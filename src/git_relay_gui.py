from __future__ import annotations

import json
import locale
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

from relay_common import (
    DEFAULT_CONFIG,
    VERSION,
    CloneOptions,
    GitOptions,
    RelayError,
    Remote,
    load_config,
    resolve_dest,
)
from relay_ops import check, clone, fetch_remote, pull_remote, push_remote
from relay_ssh import validate_remote


# Default mintty look: solid black background, light grey text, Consolas,
# no rounded 3D edges.
BG = "#000000"
FG = "#BFBFBF"
DIM = "#5A5A5A"
LINE = "#333333"
GREEN = "#00BF00"
CYAN = "#00BFBF"
YELLOW = "#BFBF00"
RED = "#FF5555"
BLUE = "#5C5CFF"
LOG_PREFIX = "[git-relay] "
CWD_MARK = "__GR_CWD__"

# The Options dialog keeps the native light theme, so it reads as a normal
# window rather than a terminal.
LIGHT_BG = "#F3F3F3"
LIGHT_FG = "#1A1A1A"
LIGHT_DIM = "#5F5F5F"


def _mono(size: int = 11) -> tuple[str, int]:
    """Return the terminal font, falling back to DejaVu where Consolas is absent."""
    # The GUI can still run on Linux without Consolas; the CLI path is unaffected.
    if os.name == "nt":
        return ("Consolas", size)
    return ("DejaVu Sans Mono", size)


def _split_args(line: str) -> list[str]:
    """Split a line like a shell: honour quotes, not backslash escapes."""
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if quote:
        raise ValueError("引号未闭合")
    if buf:
        out.append("".join(buf))
    return out


_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def _bash_from_git() -> str | None:
    """Derive Git for Windows' bash (<root>\\bin\\bash.exe) from the git.exe on PATH."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        resolved = Path(git).resolve()
    except OSError:
        return None
    for parent in resolved.parents:
        candidate = parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _is_wsl_bash(path: str) -> bool:
    """Detect the WSL launcher at system32\\bash.exe, which is not Git Bash."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return resolved.parent == system32


def _detect_shell() -> tuple[str, str]:
    """Choose Git Bash, then cmd, else $SHELL on POSIX."""
    if os.name == "nt":
        for candidate in _BASH_CANDIDATES:
            if Path(candidate).is_file():
                return ("bash", candidate)
        from_git = _bash_from_git()
        if from_git:
            return ("bash", from_git)
        found = shutil.which("bash")
        if found and not _is_wsl_bash(found):
            return ("bash", found)
        return ("cmd", os.environ.get("COMSPEC", "cmd.exe"))
    shell = os.environ.get("SHELL") or "/bin/sh"
    return ("sh", shell if Path(shell).is_file() else "/bin/sh")


def _shell_encoding(kind: str) -> str:
    """Return how a shell encodes stdout: UTF-8 for bash, the ANSI code page for cmd."""
    # Git Bash always emits UTF-8, while cmd inherits the console code page.
    if kind == "bash":
        return "utf-8"
    try:
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


def _shell_argv(kind: str, exe: str, command: str) -> list[str]:
    """Wrap command so the shell also reports its final PWD after running."""
    # The trailing echo lets a cd inside the shell feed back into the prompt bar.
    if kind == "bash":
        # -l sources /etc/profile so PATH contains git, ls and ssh.
        return [exe, "-lc", f"{command}\nprintf '\\n{CWD_MARK}%s\\n' \"$PWD\""]
    if kind == "cmd":
        return [exe, "/c", f"{command} & echo {CWD_MARK}%CD%"]
    return [exe, "-c", f"{command}\nprintf '\\n{CWD_MARK}%s\\n' \"$PWD\""]


def _msys_to_windows(path: str) -> str:
    """Turn an MSYS-style path such as /f/repo back into F:\\repo."""
    if os.name != "nt" or not path.startswith("/") or path.startswith("//"):
        return path
    parts = path.lstrip("/").split("/", 1)
    if len(parts[0]) == 1 and parts[0].isalpha():
        rest = parts[1].replace("/", "\\") if len(parts) > 1 else ""
        return f"{parts[0].upper()}:\\{rest}"
    return path


def _windows_to_msys(path: Path) -> str:
    """Render a Windows path in MSYS form (/f/repo) for display in the prompt."""
    posix = path.as_posix()
    if os.name == "nt" and len(posix) > 1 and posix[1] == ":":
        return "/" + posix[0].lower() + posix[2:]
    return posix


class QueueWriter:
    """File-like object that forwards worker output into a queue read by the Tk loop."""

    def __init__(self, target: queue.Queue) -> None:
        """Remember the queue that receives every chunk written here."""
        self.target = target

    def write(self, text: str) -> None:
        """Enqueue non-empty text; empty writes are dropped to avoid log noise."""
        if text:
            self.target.put(text)

    def flush(self) -> None:
        """No-op flush, present because callers treat this object as a stream."""

    def isatty(self) -> bool:
        """Report a non-TTY so libraries skip colour codes and cursor tricks."""
        return False


def _toml_string(value: str) -> str:
    """Quote a value as a TOML basic string, leaning on JSON string escaping."""
    return json.dumps(value, ensure_ascii=False)


def save_config(path: Path, remote: Remote, options: CloneOptions,
                git: GitOptions) -> None:
    """Persist the config file without ever writing the password to disk."""
    lines = [
        "# git-relay 配置（由图形界面保存，不包含密码）",
        "# 密码请在图形界面输入，或设置 GIT_RELAY_PASSWORD 环境变量。",
        "",
        "[remote]",
        f"host = {_toml_string(remote.host)}",
        f"user = {_toml_string(remote.user)}",
        f"port = {remote.port}",
    ]
    if remote.key:
        lines.append(f"key = {_toml_string(remote.key)}")
    lines.append(f"ssh_backend = {_toml_string(remote.backend)}")
    lines += [
        "",
        "[clone]",
        f"depth = {options.depth}",
        f"branch = {_toml_string(options.branch)}",
        f"jobs = {options.jobs}",
        f"recurse_submodules = {str(options.recurse_submodules).lower()}",
        f"remote_git = {_toml_string(options.remote_git)}",
        f"download_dir = {_toml_string(options.download_dir)}",
        "",
        "[git]",
        f"mode = {_toml_string(git.mode)}",
        f"remote = {_toml_string(git.remote)}",
        f"cache_dir = {_toml_string(git.cache_dir)}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    # Replace atomically so a crash never leaves a half-written config behind.
    os.replace(tmp, path)


def hide_console_if_owned() -> None:
    """Hide the console of a double-clicked GUI build only."""
    # Only hide the console when this process is its sole owner.
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        kernel32.GetConsoleProcessList.argtypes = [
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
        kernel32.GetConsoleProcessList.restype = ctypes.c_ulong
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int

        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        process_ids = (ctypes.c_ulong * 2)()
        count = kernel32.GetConsoleProcessList(process_ids, 2)
        if count == 1:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    """Turn on DPI awareness so Tk text stays sharp on scaled Windows displays."""
    if os.name != "nt":
        return
    try:
        import ctypes

        # System DPI awareness via shcore is preferred; fall back to the
        # legacy system-wide call.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class RelayGUI:
    """Terminal-styled Tk front end that drives relay_ops on a worker thread."""

    def __init__(
        self,
        root: tk.Tk,
        config_path: Path,
        backend_override: str | None = None,
        verbose: bool = False,
    ) -> None:
        """Load config (tolerating failures), then build the whole widget tree."""
        self.root = root
        self.config_path = config_path
        self.verbose = verbose
        self.running = False
        self._pending_success = ""
        self._closing_confirmed = False
        self.log_queue: queue.Queue = queue.Queue()

        self.cwd = Path.home()
        self.prev_cwd = self.cwd
        self.shell_kind, self.shell_exe = _detect_shell()
        self.shell_encoding = _shell_encoding(self.shell_kind)
        self.shell_running = False
        self.shell_queue: queue.Queue = queue.Queue()
        self._shell_proc: subprocess.Popen | None = None

        self.startup_error = ""
        try:
            self.remote, self.options, self.git = load_config(config_path)
        except Exception as exc:
            self.remote, self.options, self.git = (
                Remote(), CloneOptions(), GitOptions())
            self.startup_error = str(exc)
        if backend_override:
            self.remote.backend = backend_override

        self._setup_fonts()
        self._build_vars()
        self._build()
        self._build_menu()
        self._build_options()
        self._apply_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._write_banner()
        if self.startup_error:
            self._append(f"[错误] 配置读取失败：{self.startup_error}\n")
        self.cmd_entry.focus_set()
        hide_console_if_owned()

    # ---------- Fonts and state variables ----------

    def _setup_fonts(self) -> None:
        """Cache the terminal font in three sizes."""
        self.mono = _mono(11)
        self.mono_small = _mono(10)
        self.mono_big = _mono(13)

    def _build_vars(self) -> None:
        """Create every Tk variable plus the prompt and history state."""
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.backend_var = tk.StringVar(value="auto")
        self.show_password_var = tk.BooleanVar(value=False)

        self.url_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.depth_var = tk.StringVar(value="0")
        self.branch_var = tk.StringVar()
        self.recurse_var = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value="relay")

        self.prompt_var = tk.StringVar(value="user@win11 MINGW64 ~ $")
        self.status_var = tk.StringVar(value="ready")
        self.cmd_var = tk.StringVar()
        self.history: list[str] = []
        self.history_pos = 0

    # ---------- Native light-theme widgets used by the Options dialog ----------

    def _section(self, parent: tk.Widget, row: int, title: str,
                 expand: bool = False) -> ttk.Frame:
        """Create a padded label frame laid out as a four-column form section."""
        frame = ttk.LabelFrame(parent, text=title, padding=(12, 8))
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        frame.columnconfigure(0, minsize=88)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, minsize=52)
        frame.columnconfigure(3, weight=1)
        return frame

    def _label(self, parent: tk.Widget, text: str,
               color: str | None = None) -> ttk.Label:
        """Create a left-aligned label in the light dialog palette."""
        return ttk.Label(parent, text=text, foreground=color or LIGHT_FG,
                         anchor="w")

    def _hint(self, parent: tk.Widget, text: str, row: int,
              column: int = 1, span: int = 3) -> None:
        """Place a dimmed helper line under a form row."""
        ttk.Label(parent, text=text, foreground=LIGHT_DIM).grid(
            row=row, column=column, columnspan=span, sticky="w",
            pady=(2, 2))

    def _entry(self, parent: tk.Widget, var: tk.StringVar,
               width: int | None = None, show: str | None = None) -> ttk.Entry:
        """Create an entry bound to var, optionally sized or masked."""
        entry = ttk.Entry(parent, textvariable=var)
        if width:
            entry.configure(width=width)
        if show:
            entry.configure(show=show)
        return entry

    def _field(self, parent: tk.Widget, row: int, label: str,
               var: tk.StringVar, *, span: int = 1, width: int | None = None,
               show: str | None = None, col: int = 0,
               entry_col: int = 1) -> ttk.Entry:
        """Lay out a caption plus entry on one form row and return the entry."""
        ttk.Label(parent, text=label).grid(
            row=row, column=col, sticky="w", padx=(0, 10), pady=4)
        entry = self._entry(parent, var, width=width, show=show)
        entry.grid(row=row, column=entry_col, columnspan=span,
                   sticky="ew", pady=4)
        return entry

    def _button(self, parent: tk.Widget, text: str, command,
                accent: bool = False) -> ttk.Button:
        """Create a button, optionally flagged as the dialog's default action."""
        button = ttk.Button(parent, text=text, command=command)
        if accent:
            try:
                button.configure(default="active")
            except tk.TclError:
                pass
        return button

    def _check(self, parent: tk.Widget, text: str, variable: tk.BooleanVar,
               command=None) -> ttk.Checkbutton:
        """Create a checkbutton bound to a BooleanVar."""
        return ttk.Checkbutton(parent, text=text, variable=variable,
                               command=command)

    def _choice(self, parent: tk.Widget, values: tuple[str, ...],
                variable: tk.StringVar) -> ttk.Frame:
        """Lay out radio buttons in a row and return the container."""
        frame = ttk.Frame(parent)
        for value in values:
            ttk.Radiobutton(frame, text=value, value=value,
                            variable=variable).pack(side="left",
                                                    padx=(0, 14))
        return frame

    # ---------- Main window: terminal only ----------

    def _build(self) -> None:
        """Build the black mintty-style window with log, prompt line and status bar."""
        self.root.title(f"MINGW64 ~ - git-relay {VERSION}")
        self.root.geometry("900x560")
        self.root.minsize(720, 420)
        self.root.configure(bg=BG)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = tk.Frame(self.root, bg=BG, padx=12, pady=6)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        head = tk.Frame(outer, bg=BG)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        tk.Label(head, text="git-relay", bg=BG, fg=GREEN,
                 font=self.mono_big).pack(side="left")
        tk.Label(head, text=f"  {VERSION}  -  MINGW64",
                 bg=BG, fg=DIM, font=self.mono_small).pack(side="left")
        tk.Label(head, text="-  []  X", bg=BG, fg=DIM,
                 font=self.mono_small).pack(side="right")

        log_wrap = tk.Frame(outer, bg=BG)
        log_wrap.grid(row=1, column=0, sticky="nsew")
        log_wrap.columnconfigure(0, weight=1)
        log_wrap.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_wrap, wrap="char", state="disabled",
            bg=BG, fg=FG, font=self.mono_small,
            insertbackground=GREEN, selectbackground="#264F78",
            selectforeground="#FFFFFF",
            relief="flat", bd=0, highlightthickness=0,
            padx=8, pady=2, spacing1=1, spacing3=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            log_wrap, orient="vertical", command=self.log_text.yview,
            bg="#2A2A2A", troughcolor=BG, activebackground="#404040",
            relief="flat", bd=0, highlightthickness=0, width=10)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        for name, color in (
            ("prompt", GREEN), ("info", FG), ("dim", DIM),
            ("accent", CYAN), ("ok", GREEN), ("warn", YELLOW),
            ("error", RED), ("banner", BLUE),
        ):
            self.log_text.tag_configure(name, foreground=color)
        self.log_text.bind("<ButtonRelease-1>", self._log_clicked)

        cmd_bar = tk.Frame(outer, bg=BG)
        cmd_bar.grid(row=2, column=0, sticky="ew", pady=(6, 2))
        cmd_bar.columnconfigure(1, weight=1)
        tk.Label(cmd_bar, textvariable=self.prompt_var, bg=BG, fg=GREEN,
                 font=self.mono).grid(row=0, column=0, sticky="w")
        self.cmd_entry = tk.Entry(
            cmd_bar, textvariable=self.cmd_var,
            bg=BG, fg=FG, font=self.mono,
            insertbackground=GREEN, insertwidth=6, insertborderwidth=0,
            selectbackground=CYAN, selectforeground=BG,
            relief="flat", bd=0, highlightthickness=0)
        self.cmd_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.cmd_entry.bind("<Return>", self._run_command)
        self.cmd_entry.bind("<Up>", self._history_prev)
        self.cmd_entry.bind("<Down>", self._history_next)
        self.cmd_entry.bind("<Control-l>", self._clear_log)
        self.cmd_entry.bind("<Control-c>", self._copy_or_interrupt)

        status = tk.Frame(outer, bg=BG)
        status.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        tk.Label(status, textvariable=self.status_var, bg=BG, fg=FG,
                 font=self.mono_small).pack(side="left")
        tk.Label(status, text="help  ·  右键 → Options", bg=BG, fg=DIM,
                 font=self.mono_small).pack(side="right")

    # ---------- Context menu (native light menu) ----------

    def _build_menu(self) -> None:
        """Build the right-click menu and bind it to the terminal widgets."""
        # No custom colours: the native theme paints a standard light popup menu.
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Options...", command=self._show_options)
        self.menu.add_separator()
        self.menu.add_command(label="Test connection",
                              command=self._test_connection)
        self.menu.add_command(label="Clone", command=self._start_clone)
        self.menu.add_command(label="Fetch",
                              command=lambda: self._start_sync("fetch"))
        self.menu.add_command(label="Pull",
                              command=lambda: self._start_sync("pull"))
        self.menu.add_command(label="Push",
                              command=lambda: self._start_sync("push"))
        self.menu.add_separator()
        self.menu.add_command(label="Copy", command=self._copy_selection)
        self.menu.add_command(label="Paste", command=self._paste_clipboard)
        self.menu.add_command(label="Clear", command=self._clear_log)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self._on_close)

        for widget in (self.root, self.log_text, self.cmd_entry):
            widget.bind("<Button-3>", self._popup_menu)

    def _popup_menu(self, event) -> None:
        """Show the context menu, greying out task actions while a task runs."""
        state = "disabled" if self.running else "normal"
        for label in ("Test connection", "Clone", "Fetch", "Pull", "Push"):
            try:
                self.menu.entryconfigure(label, state=state)
            except tk.TclError:
                pass
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------- Options dialog ----------

    def _build_options(self) -> None:
        """Build the hidden Options dialog using the native light ttk theme."""
        win = tk.Toplevel(self.root)
        win.title("git-relay Options")
        style = ttk.Style(win)
        # Borrow the theme background so the dialog matches native light windows.
        dialog_bg = style.lookup("TFrame", "background") or LIGHT_BG
        win.configure(bg=dialog_bg)
        win.geometry("760x600")
        win.minsize(700, 570)
        win.withdraw()
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._cancel_options)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        body = ttk.Frame(win, padding=(16, 12))
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        remote = self._section(body, 0, "Remote")
        self.host_entry = self._field(
            remote, 0, "Host", self.host_var, span=3)
        self._field(remote, 1, "Port", self.port_var, width=8)
        self._field(remote, 1, "User", self.user_var, col=2, entry_col=3)
        self.password_entry = self._field(
            remote, 2, "Password", self.password_var, span=2, show="*")
        self._check(remote, "Show password", self.show_password_var,
                    self._toggle_password).grid(
            row=2, column=3, sticky="w", padx=(10, 0), pady=4)
        self._field(remote, 3, "Private key", self.key_var, span=2)
        self._button(remote, "Browse...", self._browse_key).grid(
            row=3, column=3, sticky="w", padx=(10, 0), pady=4)
        self._label(remote, "Backend").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4)
        self._choice(remote, ("auto", "paramiko", "openssh"),
                     self.backend_var).grid(
            row=4, column=1, columnspan=3, sticky="w", pady=4)
        self._hint(remote, "密码登录建议 paramiko；密钥登录可用 openssh", 5)

        repo = self._section(body, 1, "Clone")
        self._field(repo, 0, "Repository URL", self.url_var, span=3)
        self._field(repo, 1, "Destination", self.dest_var, span=2)
        self._button(repo, "Browse...", self._browse_dest).grid(
            row=1, column=3, sticky="w", padx=(10, 0), pady=4)
        self._field(repo, 2, "Depth", self.depth_var, width=8)
        self._field(repo, 2, "Branch", self.branch_var, col=2, entry_col=3)
        self._check(repo, "Recurse submodules", self.recurse_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=4)
        self._label(repo, "Mode").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4)
        self._choice(repo, ("relay", "direct"), self.mode_var).grid(
            row=4, column=1, columnspan=3, sticky="w", pady=4)
        self._hint(repo, "relay 经中转主机访问外网；direct 在本机直接执行 git", 5)

        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.save_button = self._button(
            buttons, "Apply", self._save_settings, accent=True)
        self.save_button.pack(side="left")
        self.test_button = self._button(
            buttons, "Test connection", self._test_connection)
        self.test_button.pack(side="left", padx=(8, 0))
        self.clone_button = self._button(buttons, "Clone", self._start_clone)
        self.clone_button.pack(side="left", padx=(8, 0))
        self._button(buttons, "Close", self._cancel_options).pack(
            side="right")

        win.bind("<Escape>", lambda _event: self._cancel_options())
        self.options_win = win

    def _show_options(self) -> None:
        """Reveal and focus the hidden Options dialog."""
        self.options_win.deiconify()
        self.options_win.lift()
        self.options_win.focus_force()
        self.host_entry.focus_set()

    def _hide_options(self) -> None:
        """Withdraw the dialog and hand focus back to the command entry."""
        self.options_win.withdraw()
        self.cmd_entry.focus_set()

    def _cancel_options(self) -> None:
        """Discard unsaved edits, restore config values, and hide the dialog."""
        self._apply_config()
        self._hide_options()

    # ---------- Command line ----------

    def _run_command(self, event=None):
        """Echo, record and execute the line typed at the prompt."""
        line = self.cmd_var.get().strip()
        if not line:
            return "break"
        self.cmd_var.set("")

        echo = line
        if line.lower().startswith("set password"):
            # Mask the secret in the echoed prompt line.
            echo = "set password ***"
        self._append(f"{self.prompt_var.get()} {echo}\n")
        self.history.append(line)
        self.history_pos = len(self.history)

        if line.startswith("!"):
            # A leading bang forces the raw shell, bypassing built-ins.
            self._run_shell(line[1:].strip())
            return "break"
        if line.startswith("|"):
            # A leading pipe carries shell syntax that _split_args cannot model.
            self._run_shell(line)
            return "break"

        try:
            parts = _split_args(line)
        except ValueError as exc:
            self._append(f"parse error: {exc}\n")
            return "break"
        if not parts:
            return "break"
        self._dispatch(parts, line)
        return "break"

    def _dispatch(self, parts: list[str], line: str = "") -> None:
        """Route a parsed command to a built-in handler or pass it to the shell."""
        cmd = parts[0].lower()
        args = parts[1:]
        # Commands that touch no remote state stay usable while a task runs.
        read_only = {"help", "?", "clear", "cls", "config", "options",
                     "settings", "exit", "quit", "about", "pwd"}
        if self.running and cmd not in read_only:
            self._append("[警告] 有任务正在运行，请等待完成\n")
            return

        if cmd in ("help", "?"):
            self._help()
        elif cmd in ("clear", "cls"):
            self._clear_log()
        elif cmd in ("check", "test"):
            self._test_connection()
        elif cmd == "clone":
            if args:
                self.url_var.set(args[0])
            elif not self.url_var.get().strip():
                self._append("[错误] 用法：clone <url> [dest]\n")
                return
            if len(args) > 1:
                self.dest_var.set(args[1])
            self._start_clone()
        elif cmd in ("fetch", "pull", "push"):
            self._start_sync(cmd)
        elif cmd == "cd":
            if args and (len(args) > 1
                         or any(ch in line for ch in "&|;<>")):
                # More commands follow cd, so run the whole line in the shell
                # and read PWD back.
                self._run_shell(line)
            else:
                self._cmd_cd(args)
        elif cmd == "pwd":
            self._append(_windows_to_msys(self.cwd) + "\n")
        elif cmd == "set":
            self._cmd_set(args)
        elif cmd == "config":
            self._print_config()
        elif cmd == "save":
            self._save_settings()
        elif cmd in ("options", "settings"):
            self._show_options()
        elif cmd in ("shell", "sh", "exec"):
            target = " ".join(parts[1:]) if args else ""
            if not target:
                self._append("[错误] 用法：shell <命令>\n")
            else:
                self._run_shell(target)
        elif cmd in ("exit", "quit"):
            self._on_close()
        elif cmd == "about":
            self._append(
                f"git-relay {VERSION} - clone Git repositories through a "
                "remote Linux host\n")
        else:
            self._run_shell(line or " ".join(parts))

    def _cmd_cd(self, args: list[str]) -> None:
        """Implement cd with ~, cd - and dest syncing when a repo is entered."""
        target_text = args[0] if args else str(Path.home())
        if target_text == "-":
            destination = self.prev_cwd
        elif target_text.startswith("~"):
            destination = Path.home() / target_text[1:].lstrip("/\\")
        else:
            raw = _msys_to_windows(target_text)
            candidate = Path(raw)
            destination = candidate if candidate.is_absolute() else self.cwd / candidate
        try:
            resolved = destination.resolve()
        except OSError:
            resolved = destination
        if not resolved.is_dir():
            self._append(f"cd: {target_text}: No such directory\n")
            return
        if resolved != self.cwd:
            self.prev_cwd = self.cwd
            self.cwd = resolved
            self._refresh_prompt()
        # Entering a repo also sets dest so fetch/pull/push work straight away.
        if (resolved / ".git").exists():
            self.dest_var.set(str(resolved))

    def _short_cwd(self) -> str:
        """Render the working directory relative to home for the prompt."""
        home = Path.home()
        try:
            relative = self.cwd.relative_to(home)
        except ValueError:
            return _windows_to_msys(self.cwd)
        text = relative.as_posix()
        return "~" if text in ("", ".") else f"~/{text}"

    def _refresh_prompt(self) -> None:
        """Rebuild the prompt text from user, host and current directory."""
        user = self.user_var.get().strip()
        host = self.host_var.get().strip()
        who = f"{user}@{host}" if user and host else "user@win11"
        self.prompt_var.set(f"{who} MINGW64 {self._short_cwd()} $")

    # ---------- External shell ----------

    def _run_shell(self, command: str) -> None:
        """Run a raw shell command on a worker thread, streaming output into the log."""
        if not command:
            return
        if self.running or self.shell_running:
            self._append("[警告] 上一条命令仍在执行，请等待完成或按 Ctrl+C 中断\n")
            return

        self.shell_running = True
        self.status_var.set(f"shell: {command[:40]}")
        argv = _shell_argv(self.shell_kind, self.shell_exe, command)
        kwargs = dict(
            cwd=str(self.cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding=self.shell_encoding, errors="replace", bufsize=1,
        )
        if os.name == "nt":
            # Avoid flashing a console window for every child process.
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000)

        def worker() -> None:
            """Pump the child's stdout into the shell queue until it exits."""
            try:
                proc = subprocess.Popen(argv, **kwargs)
            except OSError as exc:
                self.shell_queue.put(
                    ("error", f"无法启动 shell: {exc}"))
                return
            self._shell_proc = proc
            try:
                assert proc.stdout is not None
                for chunk in proc.stdout:
                    self.shell_queue.put(("out", chunk))
                code = proc.wait()
            except Exception as exc:  # interruptions and broken pipes land here
                self.shell_queue.put(
                    ("error", f"{type(exc).__name__}: {exc}"))
                return
            finally:
                self._shell_proc = None
            self.shell_queue.put(("end", code))

        # The thread only enqueues; the Tk loop drains the queue in _poll_shell.
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(60, self._poll_shell)

    def _poll_shell(self) -> None:
        """Drain shell output on the Tk event loop and detect completion."""
        finished = False
        while True:
            try:
                kind, payload = self.shell_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "out":
                for piece in str(payload).splitlines(keepends=True):
                    if CWD_MARK in piece:
                        # The marker separates command output from the final PWD.
                        head, _, tail = piece.partition(CWD_MARK)
                        if head.strip():
                            self._append(head if head.endswith("\n")
                                         else head + "\n")
                        self._sync_cwd(tail.strip())
                        continue
                    self._append(piece)
            elif kind == "error":
                self._error(str(payload))
                finished = True
            else:
                finished = True
                if payload not in (0, None):
                    self._append(f"[exit {payload}]\n")
        if finished:
            self.shell_running = False
            if not self.running:
                self.status_var.set("ready")
        else:
            self.root.after(60, self._poll_shell)

    def _sync_cwd(self, value: str) -> None:
        """Adopt the directory echoed by the shell and refresh the prompt."""
        if not value:
            return
        path = Path(_msys_to_windows(value))
        try:
            if not path.is_dir():
                return
            resolved = path.resolve()
        except OSError:
            return
        if resolved != self.cwd:
            self.prev_cwd = self.cwd
            self.cwd = resolved
            self._refresh_prompt()
        if (resolved / ".git").exists():
            self.dest_var.set(str(resolved))

    def _interrupt_shell(self) -> bool:
        """Kill the running shell process tree, returning False when nothing runs."""
        proc = self._shell_proc
        if proc is None or proc.poll() is not None:
            return False
        if os.name == "nt":
            try:
                # /T kills descendants too, since bash spawns git and ssh as children.
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW",
                                           0x08000000), timeout=10)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        else:
            try:
                proc.terminate()
            except Exception:
                pass
        return True

    def _cmd_set(self, args: list[str]) -> None:
        """Implement set: print or update a single configuration field."""
        settable = {
            "host": self.host_var, "port": self.port_var,
            "user": self.user_var, "password": self.password_var,
            "key": self.key_var, "backend": self.backend_var,
            "url": self.url_var, "dest": self.dest_var,
            "depth": self.depth_var, "branch": self.branch_var,
            "mode": self.mode_var,
        }
        if not args:
            self._print_config()
            return
        key = args[0].lower()
        if key in settable:
            var = settable[key]
            if len(args) == 1:
                value = var.get()
                shown = "***" if key == "password" and value else value
                self._append(f"{key} = {shown or '(unset)'}\n")
                return
            var.set(" ".join(args[1:]))
            shown = "***" if key == "password" else var.get()
            self._append(f"{key} = {shown}\n")
            return
        if key in ("recurse", "recurse-submodules"):
            if len(args) == 1:
                self._append(f"recurse-submodules = {self.recurse_var.get()}\n")
                return
            self.recurse_var.set(
                args[1].lower() in ("1", "on", "true", "yes", "y"))
            self._append(f"recurse-submodules = {self.recurse_var.get()}\n")
            return
        self._append(f"未知配置项：{key}\n")

    def _print_config(self) -> None:
        """Print current settings with the password masked."""
        password = "***" if self.password_var.get() else "(unset)"
        lines = [
            f"host      = {self.host_var.get() or '(unset)'}",
            f"port      = {self.port_var.get()}",
            f"user      = {self.user_var.get() or '(unset)'}",
            f"password  = {password}",
            f"key       = {self.key_var.get() or '(unset)'}",
            f"backend   = {self.backend_var.get()}",
            f"url       = {self.url_var.get() or '(unset)'}",
            f"dest      = {self.dest_var.get() or '(unset)'}",
            f"depth     = {self.depth_var.get()}",
            f"branch    = {self.branch_var.get() or '(default)'}",
            f"recurse   = {self.recurse_var.get()}",
            f"mode      = {self.mode_var.get()}",
            f"config    = {self.config_path}",
        ]
        self._append("\n".join(lines) + "\n")

    def _help(self) -> None:
        """Print the built-in command reference."""
        self._append(
            "内置命令：\n"
            "  help                     显示本帮助（bash 内置帮助用 !help）\n"
            "  check | test             测试中转主机连接\n"
            "  clone <url> [dest]       克隆仓库（dest 省略时自动推导）\n"
            "  fetch | pull | push      同步 dest 指向的已有仓库\n"
            "  cd [path]                切换目录（进入仓库时同步 dest，支持 cd -）\n"
            "  pwd                      打印当前目录\n"
            "  set <key> [value]        查看或设置配置项\n"
            "  config                   打印当前配置\n"
            "  save                     写入配置文件（不含密码）\n"
            "  options                  打开设置窗口（也可右键 → Options）\n"
            "  clear | cls              清屏\n"
            "  exit | quit              退出\n"
            "  shell <命令>             交给 shell 执行\n"
            "其它命令直接交给 Git Bash 执行，例如：\n"
            "  ls  ll  cat  less  grep  find  cp  mv  rm  mkdir  touch  chmod\n"
            "  git  ssh  scp  tar  unzip  curl  make  python  ps  kill  which\n"
            "  支持管道与重定向：ls -la | grep txt > out.txt\n"
            "  与内置命令同名时用 ! 强制走 bash，如 !test -f a.txt\n"
            "可用配置项：host port user password key backend url dest "
            "depth branch recurse mode\n")

    # ---------- History and clipboard ----------

    def _history_prev(self, event=None):
        """Step one entry back through the command history."""
        if not self.history:
            return "break"
        self.history_pos = max(0, self.history_pos - 1)
        self.cmd_var.set(self.history[self.history_pos])
        self.cmd_entry.icursor("end")
        return "break"

    def _history_next(self, event=None):
        """Step forward, showing an empty line once past the newest entry."""
        if not self.history:
            return "break"
        self.history_pos = min(len(self.history), self.history_pos + 1)
        if self.history_pos >= len(self.history):
            self.cmd_var.set("")
        else:
            self.cmd_var.set(self.history[self.history_pos])
        self.cmd_entry.icursor("end")
        return "break"

    def _log_clicked(self, event=None) -> None:
        """Return focus to the entry when a click left no log selection."""
        try:
            if not self.log_text.tag_ranges("sel"):
                self.cmd_entry.focus_set()
        except tk.TclError:
            pass

    def _copy_text(self, text: str) -> None:
        """Put text on the clipboard and report its length in the status bar."""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set(f"copied {len(text)} chars")

    def _copy_selection(self) -> None:
        """Copy the log selection, else the entry selection, else the whole entry."""
        text = ""
        try:
            text = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            pass
        if not text:
            if self.cmd_entry.selection_present():
                text = self.cmd_entry.selection_get()
            else:
                text = self.cmd_var.get()
        if not text:
            self.status_var.set("nothing to copy")
            return
        self._copy_text(text)

    def _copy_or_interrupt(self, event=None):
        """Copy a selection, otherwise interrupt the shell or clear the input line."""
        try:
            text = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            text = ""
        if text:
            self._copy_text(text)
        elif self._interrupt_shell():
            self._append("^C\n")
            self.status_var.set("已中断")
        else:
            # Nothing selected and nothing running: behave like ^C on an empty prompt.
            self._append(f"{self.prompt_var.get()} {self.cmd_var.get()}^C\n")
            self.cmd_var.set("")
        return "break"

    def _paste_clipboard(self) -> None:
        """Insert clipboard text at the cursor in the command entry."""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        self.cmd_entry.insert("insert", text)
        self.cmd_entry.focus_set()

    # ---------- Log output and prompt ----------

    def _write_banner(self) -> None:
        """Print the startup banner with the shell path and usage hints."""
        self._append(
            f"git-relay {VERSION} - MINGW64 terminal UI\n"
            f"shell: {self.shell_exe}\n"
            "输入命令：内置命令见 help，其余交给 bash（ls / cat / grep / git / ssh ...）。\n"
            "服务器与克隆参数在 右键 → Options。\n"
            "\n")

    @staticmethod
    def _line_tag(body: str) -> str:
        """Pick a colour tag for a log line from keywords it contains."""
        low = body.lower()
        if not body.strip():
            return "info"
        if ((" MINGW64 " in body and " $ " in body)
                or body.startswith("$ ")
                or body.startswith("=== ")):
            return "prompt"
        if ("[错误]" in body or "error:" in low or "fatal:" in low
                or "失败" in body or "traceback" in low):
            return "error"
        if "warning" in low or "[警告]" in body or "重试" in body:
            return "warn"
        if ("完成" in body or "通过" in body or "已保存" in body
                or "already up to date" in low or "success" in low):
            return "ok"
        if body.startswith("[gui]") or body.startswith("#"):
            return "dim"
        if body.startswith("git-relay ") and "MINGW64" in body:
            return "banner"
        if "clone:" in low or "receiving objects" in low:
            return "accent"
        return "info"

    def _append(self, text: str) -> None:
        """Append text to the log, colouring each line and trimming old output."""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self.log_text.configure(state="normal")
        for line in normalized.splitlines(keepends=True):
            has_newline = line.endswith("\n")
            body = line[:-1] if has_newline else line
            newline = "\n" if has_newline else ""
            tag = self._line_tag(body)
            if body.startswith(LOG_PREFIX):
                self.log_text.insert("end", LOG_PREFIX, "accent")
                self.log_text.insert(
                    "end", body[len(LOG_PREFIX):] + newline, tag)
            else:
                self.log_text.insert("end", body + newline, tag)
        try:
            last_line = int(self.log_text.index("end-1c").split(".")[0])
            # Keep the buffer bounded so long-running commands cannot grow it
            # without end.
            if last_line > 3000:
                self.log_text.delete("1.0", "500.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self, event=None) -> str:
        """Clear the log (Ctrl+L, clear, cls) and keep typing state intact."""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")
        return "break"

    def _error(self, message: str) -> None:
        """Log an error line and flag the status bar as failed."""
        self._append(f"[错误] {message}\n")
        self.status_var.set("失败")

    # ---------- Config and task orchestration ----------

    def _apply_config(self) -> None:
        """Push the loaded config into the Tk variables and refresh the prompt."""
        self.host_var.set(self.remote.host)
        self.port_var.set(str(self.remote.port))
        self.user_var.set(self.remote.user)
        self.password_var.set(self.remote.password)
        self.key_var.set(self.remote.key)
        self.backend_var.set(self.remote.backend)
        self.depth_var.set(str(self.options.depth))
        self.branch_var.set(self.options.branch)
        self.recurse_var.set(self.options.recurse_submodules)
        self.mode_var.set(self.git.mode)
        self._refresh_prompt()
        if self.remote.host and self.remote.user:
            self.status_var.set(f"已加载 {self.config_path}")

    def _toggle_password(self) -> None:
        """Show or mask the password entry."""
        self.password_entry.configure(
            show="" if self.show_password_var.get() else "*")

    def _browse_key(self) -> None:
        """Let the user pick a private key file."""
        initial = Path.home() / ".ssh"
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥", parent=self.options_win,
            initialdir=str(initial) if initial.is_dir() else str(Path.home()))
        if path:
            self.key_var.set(path)

    def _browse_dest(self) -> None:
        """Let the user pick a local destination directory."""
        initial = (Path(self.options.download_dir).expanduser()
                   if self.options.download_dir else Path.home())
        path = filedialog.askdirectory(
            title="选择本地目录", parent=self.options_win,
            initialdir=str(initial) if initial.is_dir() else str(Path.home()))
        if path:
            self.dest_var.set(path)

    def _remote_from_fields(self) -> Remote:
        """Validate Remote fields, raising RelayError on bad input."""
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            raise RelayError("端口必须是整数")
        remote = Remote(
            host=self.host_var.get().strip(),
            user=self.user_var.get().strip(),
            port=port,
            password=self.password_var.get(),
            key=self.key_var.get().strip(),
            backend=self.backend_var.get() or "auto",
        )
        validate_remote(remote)
        return remote

    def _options_from_fields(self) -> CloneOptions:
        """Validate clone fields, raising RelayError on bad input."""
        try:
            depth = int(self.depth_var.get().strip())
        except ValueError:
            raise RelayError("克隆深度必须是整数")
        if depth < 0:
            raise RelayError("克隆深度不能为负数")
        return replace(
            self.options,
            depth=depth,
            branch=self.branch_var.get().strip(),
            recurse_submodules=bool(self.recurse_var.get()),
        )

    def _git_from_fields(self) -> GitOptions:
        """Return GitOptions carrying the mode selected in the dialog."""
        return replace(self.git, mode=self.mode_var.get() or "relay")

    def _save_settings(self) -> None:
        """Validate and persist the config, deliberately leaving the password out."""
        try:
            remote = self._remote_from_fields()
            options = self._options_from_fields()
            git = self._git_from_fields()
            # The password is never persisted: it lives in memory or in
            # GIT_RELAY_PASSWORD.
            save_config(self.config_path, remote, options, git)
        except Exception as exc:
            self._error(f"保存失败：{exc}")
            return
        self._append(f"[gui] 已保存配置（不含密码）：{self.config_path}\n")
        self.status_var.set("设置已保存；密码不会被写入文件")

    def _test_connection(self) -> None:
        """Validate the fields, then run the connection check as a background task."""
        try:
            remote = self._remote_from_fields()
        except RelayError as exc:
            self._error(f"配置错误：{exc}")
            return

        def action() -> None:
            """Run check and turn a non-zero exit into a RelayError."""
            if check(remote, self.verbose) != 0:
                raise RelayError("连接检查失败，请查看上方日志")

        self._start_task("测试连接", action, "连接检查通过")

    def _start_clone(self) -> None:
        """Validate fields, resolve the destination, then clone in the background."""
        try:
            remote = self._remote_from_fields()
            options = self._options_from_fields()
            url = self.url_var.get().strip()
            if not url:
                raise RelayError("请填写仓库 URL")
            dest_text = self.dest_var.get().strip()
            dest = resolve_dest(url, dest_text, options, Path.home())
        except RelayError as exc:
            self._error(f"配置错误：{exc}")
            return
        self.dest_var.set(str(dest))

        def action() -> None:
            """Perform the clone; progress is reported through the log queue."""
            clone(url, dest, remote, options, self.verbose, dry_run=False)

        self._start_task("克隆仓库", action, f"克隆完成：{dest}")

    def _start_sync(self, kind: str) -> None:
        """Run fetch, pull or push as a background task."""
        try:
            remote = self._remote_from_fields()
            options = self._options_from_fields()
            git = self._git_from_fields()
            dest_text = self.dest_var.get().strip()
            if not dest_text:
                raise RelayError("请先设置 dest（已有仓库路径）")
            dest = Path(dest_text).expanduser()
        except RelayError as exc:
            self._error(f"配置错误：{exc}")
            return
        branch = self.branch_var.get().strip()
        func = {"fetch": fetch_remote, "pull": pull_remote,
                "push": push_remote}[kind]
        names = {"fetch": "抓取", "pull": "拉取", "push": "推送"}

        def action() -> None:
            """Call the selected sync operation with the current settings."""
            func(dest, remote, options, git, branch=branch,
                 verbose=self.verbose, dry_run=False)

        self._start_task(names[kind], action, f"{names[kind]}完成：{dest}")

    def _start_task(self, name: str, action, success_status: str) -> None:
        """Run action on a daemon thread, funnelling its output into log_queue."""
        if self.running:
            return
        self.running = True
        self._pending_success = success_status
        self._set_buttons(False)
        self.status_var.set(f"{name}进行中...")
        self._append(f"\n=== {name} ===\n")
        self.log_queue = queue.Queue()
        writer = QueueWriter(self.log_queue)

        def worker() -> None:
            """Run the task with stdout/stderr redirected, then signal the Tk loop."""
            stdout, stderr = sys.stdout, sys.stderr
            sys.stdout = writer
            sys.stderr = writer
            try:
                action()
                self.log_queue.put(("done", None))
            except RelayError as exc:
                self.log_queue.put(("error", str(exc)))
            except Exception as exc:
                self.log_queue.put(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                sys.stdout, sys.stderr = stdout, stderr

        # Worker output reaches the widgets only through _poll_log on the Tk event loop.
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_log)

    def _poll_log(self) -> None:
        """Drain the task queue on the Tk event loop and restore the UI when done."""
        finished = False
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                kind, payload = item
                if kind == "error":
                    self._error(str(payload))
                else:
                    self.status_var.set(self._pending_success)
                finished = True
            else:
                self._append(item)
        if finished:
            self.running = False
            self._set_buttons(True)
        elif self.running:
            self.root.after(100, self._poll_log)

    def _set_buttons(self, enabled: bool) -> None:
        """Enable or disable the task buttons while a job runs."""
        state = "normal" if enabled else "disabled"
        for button in (self.test_button, self.clone_button, self.save_button):
            try:
                button.configure(state=state)
            except tk.TclError:
                pass

    def _on_close(self) -> None:
        """Stop any shell, confirm once if a task runs, then destroy the window."""
        if self.shell_running:
            self._interrupt_shell()
        if self.running and not self._closing_confirmed:
            # A second close during the same task exits without asking again.
            self._closing_confirmed = True
            self.status_var.set("任务进行中，再次关闭将强制退出")
            self._append("\n[警告] 任务仍在进行，再次点击关闭将退出。\n")
            return
        self.root.destroy()


def run_gui(
    config_path: Path | None = None,
    backend_override: str | None = None,
    verbose: bool = False,
) -> int:
    """Enable DPI awareness, build the root window, and run the Tk main loop."""
    enable_dpi_awareness()
    root = tk.Tk()
    RelayGUI(root, config_path or DEFAULT_CONFIG, backend_override, verbose)
    root.mainloop()
    return 0
