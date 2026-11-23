from __future__ import annotations

import json
import os
import queue
import sys
import threading
from dataclasses import replace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

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


class QueueWriter:
    def __init__(self, target: queue.Queue) -> None:
        self.target = target

    def write(self, text: str) -> None:
        if text:
            self.target.put(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def save_config(path: Path, remote: Remote, options: CloneOptions,
                git: GitOptions) -> None:
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
    os.replace(tmp, path)


def hide_console_if_owned() -> None:
    # 双击 exe 时隐藏附带的控制台；从已有终端启动时保留终端窗口。
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
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class RelayGUI:
    def __init__(
        self,
        root: tk.Tk,
        config_path: Path,
        backend_override: str | None = None,
        verbose: bool = False,
    ) -> None:
        self.root = root
        self.config_path = config_path
        self.verbose = verbose
        self.running = False
        self._pending_success = ""
        self.log_queue: queue.Queue = queue.Queue()

        self.startup_error = ""
        try:
            self.remote, self.options, self.git = load_config(config_path)
        except Exception as exc:
            self.remote, self.options, self.git = (
                Remote(), CloneOptions(), GitOptions())
            self.startup_error = str(exc)
        if backend_override:
            self.remote.backend = backend_override

        self._build()
        self._apply_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.startup_error:
            self.root.after(
                50,
                lambda: messagebox.showwarning(
                    "配置读取失败", self.startup_error, parent=self.root))
        hide_console_if_owned()

    def _build(self) -> None:
        self.root.title(f"git-relay {VERSION} - Git 克隆加速器")
        self.root.geometry("840x680")
        self.root.minsize(720, 560)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        server = ttk.LabelFrame(outer, text="远程服务器", padding=10)
        server.grid(row=0, column=0, sticky="ew")
        server.columnconfigure(1, weight=1)
        server.columnconfigure(3, weight=1)

        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.backend_var = tk.StringVar(value="auto")
        self.show_password_var = tk.BooleanVar(value=False)

        ttk.Label(server, text="服务器").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(server, textvariable=self.host_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(server, text="端口").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(server, textvariable=self.port_var, width=8).grid(
            row=1, column=1, sticky="w", pady=4)
        ttk.Label(server, text="用户").grid(
            row=1, column=2, sticky="e", padx=8, pady=4)
        ttk.Entry(server, textvariable=self.user_var).grid(
            row=1, column=3, sticky="ew", pady=4)

        ttk.Label(server, text="密码").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.password_entry = ttk.Entry(
            server, textvariable=self.password_var, show="*")
        self.password_entry.grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Checkbutton(
            server, text="显示密码", variable=self.show_password_var,
            command=self._toggle_password).grid(
            row=2, column=3, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(server, text="私钥").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(server, textvariable=self.key_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(server, text="浏览...", command=self._browse_key).grid(
            row=3, column=3, sticky="ew", pady=4)

        ttk.Label(server, text="SSH 后端").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            server, textvariable=self.backend_var, state="readonly",
            values=("auto", "paramiko", "openssh"), width=12).grid(
            row=4, column=1, sticky="w", pady=4)
        ttk.Label(
            server, text="密码登录建议 paramiko；密钥登录可用 openssh",
            foreground="#666666").grid(
            row=4, column=2, columnspan=2, sticky="w", padx=8, pady=4)

        repo = ttk.LabelFrame(outer, text="仓库与克隆选项", padding=10)
        repo.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        repo.columnconfigure(1, weight=1)
        repo.columnconfigure(3, weight=1)

        self.url_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.depth_var = tk.StringVar(value="0")
        self.branch_var = tk.StringVar()
        self.recurse_var = tk.BooleanVar(value=False)

        ttk.Label(repo, text="仓库 URL").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(repo, textvariable=self.url_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(repo, text="本地目录").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(repo, textvariable=self.dest_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(repo, text="浏览...", command=self._browse_dest).grid(
            row=1, column=3, sticky="ew", pady=4)

        ttk.Label(repo, text="深度").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Spinbox(
            repo, textvariable=self.depth_var, from_=0, to=10000,
            width=8).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(repo, text="分支").grid(
            row=2, column=2, sticky="e", padx=8, pady=4)
        ttk.Entry(repo, textvariable=self.branch_var).grid(
            row=2, column=3, sticky="ew", pady=4)
        ttk.Checkbutton(
            repo, text="同时拉取子模块（recurse-submodules）",
            variable=self.recurse_var).grid(
            row=3, column=1, columnspan=3, sticky="w", pady=4)

        self.mode_var = tk.StringVar(value="relay")
        ttk.Label(repo, text="同步方式").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            repo, textvariable=self.mode_var, state="readonly",
            values=("relay", "direct"), width=12).grid(
            row=4, column=1, sticky="w", pady=4)
        ttk.Label(
            repo, text="relay: 经中转主机访问外网；direct: 本机直接执行 git",
            foreground="#666666").grid(
            row=4, column=2, columnspan=2, sticky="w", padx=8, pady=4)

        sync = ttk.LabelFrame(
            outer, text="同步已有仓库（使用上面的本地目录）", padding=10)
        sync.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.fetch_button = ttk.Button(
            sync, text="抓取 fetch",
            command=lambda: self._start_sync("fetch"))
        self.fetch_button.pack(side="left")
        self.pull_button = ttk.Button(
            sync, text="拉取 pull", command=lambda: self._start_sync("pull"))
        self.pull_button.pack(side="left", padx=8)
        self.push_button = ttk.Button(
            sync, text="推送 push", command=lambda: self._start_sync("push"))
        self.push_button.pack(side="left")
        ttk.Label(
            sync, text="pull 仅做 fast-forward；推送需要中转主机具备推送凭据",
            foreground="#666666").pack(side="right")

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.test_button = ttk.Button(
            buttons, text="测试连接", command=self._test_connection)
        self.test_button.pack(side="left")
        self.clone_button = ttk.Button(
            buttons, text="开始克隆", command=self._start_clone)
        self.clone_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(
            buttons, text="保存设置（不含密码）", command=self._save_settings)
        self.save_button.pack(side="left")
        ttk.Button(buttons, text="清空日志", command=self._clear_log).pack(
            side="right")

        log_frame = ttk.LabelFrame(outer, text="日志", padding=6)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame, height=14, wrap="word", state="disabled",
            font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.status_var = tk.StringVar(value="请填写服务器、用户和密码或私钥")
        ttk.Label(outer, textvariable=self.status_var).grid(
            row=5, column=0, sticky="w", pady=(8, 0))

    def _apply_config(self) -> None:
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
        if self.remote.host and self.remote.user:
            self.status_var.set(
                f"已加载 {self.config_path}；确认后点击测试连接")

    def _toggle_password(self) -> None:
        self.password_entry.configure(
            show="" if self.show_password_var.get() else "*")

    def _browse_key(self) -> None:
        initial = Path.home() / ".ssh"
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥", parent=self.root,
            initialdir=str(initial) if initial.is_dir() else str(Path.home()))
        if path:
            self.key_var.set(path)

    def _browse_dest(self) -> None:
        initial = (Path(self.options.download_dir).expanduser()
                   if self.options.download_dir else Path.home())
        path = filedialog.askdirectory(
            title="选择本地目录", parent=self.root,
            initialdir=str(initial) if initial.is_dir() else str(Path.home()))
        if path:
            self.dest_var.set(path)

    def _remote_from_fields(self) -> Remote:
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
        return replace(self.git, mode=self.mode_var.get() or "relay")

    def _save_settings(self) -> None:
        try:
            remote = self._remote_from_fields()
            options = self._options_from_fields()
            git = self._git_from_fields()
            save_config(self.config_path, remote, options, git)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self._append(f"[gui] 已保存配置（不含密码）：{self.config_path}\n")
        self.status_var.set("设置已保存；密码不会被写入文件")

    def _test_connection(self) -> None:
        try:
            remote = self._remote_from_fields()
        except RelayError as exc:
            messagebox.showerror("配置错误", str(exc), parent=self.root)
            return

        def action() -> None:
            if check(remote, self.verbose) != 0:
                raise RelayError("连接检查失败，请查看上方日志")

        self._start_task("测试连接", action, "连接检查通过")

    def _start_clone(self) -> None:
        try:
            remote = self._remote_from_fields()
            options = self._options_from_fields()
            url = self.url_var.get().strip()
            if not url:
                raise RelayError("请填写仓库 URL")
            dest_text = self.dest_var.get().strip()
            dest = resolve_dest(url, dest_text, options, Path.home())
        except RelayError as exc:
            messagebox.showerror("配置错误", str(exc), parent=self.root)
            return
        self.dest_var.set(str(dest))

        def action() -> None:
            clone(url, dest, remote, options, self.verbose, dry_run=False)

        self._start_task("克隆仓库", action, f"克隆完成：{dest}")

    def _start_sync(self, kind: str) -> None:
        try:
            remote = self._remote_from_fields()
            options = self._options_from_fields()
            git = self._git_from_fields()
            dest_text = self.dest_var.get().strip()
            if not dest_text:
                raise RelayError("请先填写本地目录（已有仓库路径）")
            dest = Path(dest_text).expanduser()
        except RelayError as exc:
            messagebox.showerror("配置错误", str(exc), parent=self.root)
            return
        branch = self.branch_var.get().strip()
        func = {"fetch": fetch_remote, "pull": pull_remote,
                "push": push_remote}[kind]
        names = {"fetch": "抓取", "pull": "拉取", "push": "推送"}

        def action() -> None:
            func(dest, remote, options, git, branch=branch,
                 verbose=self.verbose, dry_run=False)

        self._start_task(names[kind], action, f"{names[kind]}完成：{dest}")

    def _start_task(self, name: str, action, success_status: str) -> None:
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

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_log)

    def _poll_log(self) -> None:
        finished = False
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                kind, payload = item
                if kind == "error":
                    self._append(f"\n[错误] {payload}\n")
                    self.status_var.set("失败")
                    messagebox.showerror("执行失败", payload, parent=self.root)
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
        state = "normal" if enabled else "disabled"
        for button in (self.test_button, self.clone_button, self.save_button,
                       self.fetch_button, self.pull_button, self.push_button):
            button.configure(state=state)

    def _append(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.running and not messagebox.askyesno(
                "退出", "任务仍在进行，确定退出吗？", parent=self.root):
            return
        self.root.destroy()


def run_gui(
    config_path: Path | None = None,
    backend_override: str | None = None,
    verbose: bool = False,
) -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    RelayGUI(root, config_path or DEFAULT_CONFIG, backend_override, verbose)
    root.mainloop()
    return 0
