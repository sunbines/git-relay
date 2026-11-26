"""Shared constants, configuration, local git helpers and progress output."""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from common.toml_parse import parse_toml

VERSION = "1.3"
MIB = 1 << 20
DEFAULT_DOWNLOAD_DIR = "/root"
PASSWORD_ENV = "GIT_RELAY_PASSWORD"

DEFAULT_CONFIG = Path("/etc/git-relay/git-relay.toml")


class RelayError(RuntimeError):
    """Fatal, user-facing error reported without a traceback."""
    pass


_active_reporter: "ProgressReporter | None" = None


def finish_progress_line() -> None:
    """Close any in-place status line before other output is written."""
    # Status lines are written with a bare "\r" and no newline, so any other
    # output must close the line first or it would be appended to it.
    reporter = _active_reporter
    if reporter is not None:
        reporter.end()


def log(msg: str) -> None:
    """Print an informational message tagged with the tool name."""
    finish_progress_line()
    print(f"[git-relay] {msg}", flush=True)


def configure_stdio() -> None:
    """Force UTF-8 stdio so non-ASCII output survives legacy consoles."""
    # Legacy Windows consoles and redirected streams may default to a
    # non-UTF-8 encoding, which can break non-ASCII output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def warn(msg: str) -> None:
    """Print a warning to stderr without aborting the command."""
    finish_progress_line()
    print(f"[git-relay] warning: {msg}", file=sys.stderr, flush=True)


@dataclass
class RemoteStats:
    """Remote repository size and clone time, when the host reported them."""

    repo_bytes: int = 0
    clone_seconds: float = 0.0


class TokenBucket:
    """Token bucket used to throttle status updates to a steady rate."""
    def __init__(self, rate: float, capacity: float) -> None:
        """Start with a full bucket and the refill clock at now."""
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()

    def take(self) -> bool:
        """Consume one token if available, refilling lazily by elapsed time."""
        now = time.monotonic()
        # Refill from the elapsed interval instead of a background timer, so
        # an idle caller accumulates at most `capacity` tokens.
        self._tokens = min(
            self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class ProgressReporter:
    """Throttled, single-line progress output on stderr."""
    def __init__(self, rate: float = 4.0, capacity: float = 2.0) -> None:
        """Set up throttling, cache TTY state and register as active."""
        self._bucket = TokenBucket(rate, capacity)
        self._tty = sys.stderr.isatty()
        self._active = False
        self._lock = threading.Lock()
        self._last_width = 0
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns or 80
        self._width = max(20, columns - 1)
        global _active_reporter
        _active_reporter = self

    def update(self, message: str, force: bool = False) -> None:
        """Emit a status message, rate limited unless force is set."""
        # The lock is held across the write: in-place status lines share one
        # terminal line, so interleaved writes would garble it.
        with self._lock:
            if not force and not self._bucket.take():
                return
            if self._tty:
                self._write_status(f"[git-relay] {message}")
            else:
                print(f"[git-relay] {message}", file=sys.stderr, flush=True)

    def write_line(self, text: str) -> None:
        """Write a complete line, closing any active status line first."""
        with self._lock:
            self._finish_locked()
            sys.stderr.write(f"{text}\n")
            sys.stderr.flush()

    def end(self) -> None:
        """Finish the current status line so later output starts fresh."""
        with self._lock:
            self._finish_locked()

    def _write_status(self, line: str) -> None:
        """Redraw the status line in place, truncating and padding it."""
        if len(line) > self._width:
            line = line[:self._width]
        # Pad over the previous text; otherwise remnants of a longer message
        # would stay visible after the carriage return.
        padding = " " * max(0, self._last_width - len(line))
        sys.stderr.write(f"\r{line}{padding}")
        sys.stderr.flush()
        self._last_width = len(line)
        self._active = True

    def _finish_locked(self) -> None:
        """Terminate an active status line; caller must hold the lock."""
        if self._active:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._active = False
            self._last_width = 0


def dir_size(path: Path) -> int:
    """Sum regular file sizes under path, skipping unreadable entries."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(
                                follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def format_size(size: int) -> str:
    """Format a byte count with a binary unit suffix."""
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def log_remote_stats(stats: RemoteStats) -> None:
    """Log remote repository size and effective transfer speed."""
    if stats.repo_bytes <= 0:
        return
    size = format_size(stats.repo_bytes)
    if stats.clone_seconds > 0:
        speed = stats.repo_bytes / stats.clone_seconds / MIB
        log(f"remote repository size: {size} "
            f"(cloned in {stats.clone_seconds:.0f}s, "
            f"{speed:.2f} MiB/s)")
    else:
        log(f"remote repository size: {size}")


@dataclass
class Remote:
    """SSH connection settings for the relay host."""

    host: str = ""
    user: str = ""
    port: int = 22
    password: str = ""
    key: str = ""
    backend: str = "auto"


@dataclass
class CloneOptions:
    """Clone behaviour shared by remote and local operations."""

    depth: int = 0
    branch: str = ""
    recurse_submodules: bool = False
    jobs: int = 4
    remote_git: str = "git"
    download_dir: str = ""


@dataclass
class GitOptions:
    """How local Git commands reach the remote repository."""

    mode: str = "relay"      # relay: via the remote host; direct: local git
    remote: str = "origin"   # local git remote name
    cache_dir: str = ""      # mirror cache on the remote host


def _as_int(value: object, what: str) -> int:
    """Coerce a config value to int, naming the option on failure."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RelayError(f"config option {what} must be an integer: {value!r}")


def load_config(path: Path) -> tuple[Remote, CloneOptions, GitOptions]:
    """Load settings, letting GIT_RELAY_* environment variables win."""
    data: dict = {}
    # A missing file is not an error: defaults and environment variables still
    # produce a usable configuration.
    if path.exists():
        try:
            data = parse_toml(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RelayError(
                f"failed to parse config file {path}: {exc}") from exc

    r = data.get("remote", {})
    c = data.get("clone", {})
    g = data.get("git", {})
    # Environment variables are read first so a single run can override the
    # file (or supply values when no file exists at all).
    remote = Remote(
        host=os.environ.get("GIT_RELAY_HOST", str(r.get("host", ""))),
        user=os.environ.get("GIT_RELAY_USER", str(r.get("user", ""))),
        port=_as_int(os.environ.get("GIT_RELAY_PORT", r.get("port", 22)),
                     "remote.port"),
        password=os.environ.get(PASSWORD_ENV, str(r.get("password", ""))),
        key=os.environ.get("GIT_RELAY_KEY", str(r.get("key", ""))),
        backend=os.environ.get("GIT_RELAY_SSH_BACKEND",
                               str(r.get("ssh_backend", "auto"))),
    )
    options = CloneOptions(
        depth=_as_int(os.environ.get("GIT_RELAY_DEPTH", c.get("depth", 0)),
                      "clone.depth"),
        branch=os.environ.get("GIT_RELAY_BRANCH", str(c.get("branch", ""))),
        recurse_submodules=bool(c.get("recurse_submodules", False)),
        jobs=_as_int(os.environ.get("GIT_RELAY_JOBS", c.get("jobs", 4)),
                     "clone.jobs"),
        remote_git=os.environ.get("GIT_RELAY_REMOTE_GIT",
                                  str(c.get("remote_git", "git"))),
        download_dir=os.environ.get(
            "GIT_RELAY_DOWNLOAD_DIR",
            str(c.get("download_dir", "") or DEFAULT_DOWNLOAD_DIR)).strip(),
    )
    if remote.backend not in {"auto", "openssh", "paramiko"}:
        raise RelayError(
            f"ssh_backend must be auto/openssh/paramiko: {remote.backend}")
    if options.depth < 0:
        raise RelayError("depth cannot be negative")
    if options.jobs < 1:
        raise RelayError("jobs must be greater than 0")
    git = GitOptions(
        mode=os.environ.get("GIT_RELAY_GIT_MODE",
                            str(g.get("mode", "relay"))).strip().lower(),
        remote=os.environ.get("GIT_RELAY_GIT_REMOTE",
                              str(g.get("remote", "origin"))).strip(),
        cache_dir=os.environ.get("GIT_RELAY_GIT_CACHE_DIR",
                                 str(g.get("cache_dir", ""))).strip(),
    )
    if git.mode not in {"relay", "direct"}:
        raise RelayError(f"git.mode must be relay/direct: {git.mode}")
    if not git.remote:
        raise RelayError("git.remote must not be empty")
    return remote, options, git


def _toml_string(value: str) -> str:
    """Quote a value as a TOML basic string, escaping what TOML rejects."""
    # Backslashes go first; otherwise the escapes added below get doubled.
    escaped = (value.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t"))
    return f'"{escaped}"'


def render_config(remote: Remote, options: CloneOptions,
                  git: GitOptions) -> str:
    """Render the effective settings as a TOML document."""
    host = remote.host or "your-server.example.com"
    user = remote.user or "your-user"
    recurse = "true" if options.recurse_submodules else "false"
    lines = [
        "# git-relay configuration",
        "# The password is never written here; set GIT_RELAY_PASSWORD instead.",
        "",
        "[remote]",
        f"host = {_toml_string(host)}",
        f"user = {_toml_string(user)}",
        f"port = {remote.port}",
    ]
    if remote.key:
        lines.append(f"key = {_toml_string(remote.key)}")
    else:
        lines.append('# key = "~/.ssh/id_ed25519"')
    lines += [
        f"ssh_backend = {_toml_string(remote.backend)}",
        "",
        "[clone]",
        f"depth = {options.depth}",
        f"branch = {_toml_string(options.branch)}",
        f"recurse_submodules = {recurse}",
        f"jobs = {options.jobs}",
        f"remote_git = {_toml_string(options.remote_git)}",
        f"download_dir = {_toml_string(options.download_dir)}",
        "",
        "[git]",
        f"mode = {_toml_string(git.mode)}",
        f"remote = {_toml_string(git.remote)}",
        f"cache_dir = {_toml_string(git.cache_dir)}",
        "",
    ]
    return "\n".join(lines)


def run_local(cmd: list[str], cwd: Path | None = None,
              check: bool = True) -> subprocess.CompletedProcess:
    """Run a local command with captured output and no interactive prompts."""
    env = os.environ.copy()
    # Without this, git would block waiting for credentials on a TTY-less run.
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RelayError(f"local command failed (exit {proc.returncode}): "
                         f"{' '.join(cmd)}\n{detail[-3000:]}")
    return proc


def run_local_stream(cmd: list[str], cwd: Path | None = None) -> int:
    """Run a local command with inherited stdio so its output streams live."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, env=env)
    return proc.returncode


def resolve_local_repo(dest: Path, options: CloneOptions) -> Path:
    """Resolve dest to a work-tree root, falling back to download_dir."""
    dest = dest.expanduser()
    if not dest.is_absolute() and not dest.exists() and options.download_dir:
        dest = Path(options.download_dir).expanduser() / dest
    if not dest.exists():
        raise RelayError(f"local repository path does not exist: {dest}")
    top = run_local(["git", "-C", str(dest), "rev-parse", "--show-toplevel"],
                    check=False)
    if top.returncode != 0:
        detail = (top.stderr or top.stdout).strip()
        hint = f": {detail[-1000:]}" if detail else ""
        raise RelayError(f"not a Git work tree: {dest}{hint}")
    return Path(top.stdout.strip())


def validate_member(member: tarfile.TarInfo) -> None:
    """Reject archive members that could escape the destination directory."""
    # Windows-built archives may use backslashes; normalize before splitting.
    name = member.name.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RelayError(f"archive contains an unsafe path: {member.name}")
    if path.parts and ":" in path.parts[0]:
        raise RelayError(f"archive contains an unsafe path: {member.name}")
    if member.isdev() or member.isfifo():
        raise RelayError(f"archive contains a special file: {member.name}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname.replace("\\", "/"))
        # Symlink targets are relative to the link's own directory, so resolve
        # them there to catch escapes like ../../etc.
        base = PurePosixPath(name).parent if member.issym() else PurePosixPath()
        resolved = PurePosixPath(posixpath.normpath(str(base / target)))
        if (target.is_absolute()
                or (target.parts and ":" in target.parts[0])
                or resolved.is_absolute()
                or (resolved.parts and resolved.parts[0] == "..")):
            raise RelayError(f"archive contains an unsafe link: {member.name}")


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract a gzipped tarball after validating every member."""
    try:
        with tarfile.open(archive, "r:gz") as tf:
            # Validate the whole archive up front so a hostile member aborts
            # the run before anything is written to disk.
            for member in tf.getmembers():
                validate_member(member)
            tf.extractall(dest)
    except RelayError:
        raise
    except (OSError, tarfile.TarError) as exc:
        hint = ""
        if os.name == "nt":
            hint = ("; on Windows, if this fails on symlinks, enable "
                    "Developer Mode or use an administrator terminal")
        raise RelayError(f"failed to extract repository: {exc}{hint}") from exc


def local_checkout(repo: Path, options: CloneOptions) -> None:
    """Check out the work tree and optionally initialize local submodules."""
    run_local(["git", "-C", str(repo), "checkout"])
    if not (repo / ".gitmodules").exists():
        return
    status = run_local(["git", "-C", str(repo), "submodule", "status"],
                       check=False)
    if status.returncode != 0:
        return
    # A leading "-" in `submodule status` marks a submodule that is not yet
    # initialized; we only pay the update cost when that is the case.
    missing = any(line.startswith("-") for line in status.stdout.splitlines())
    if missing:
        if not options.recurse_submodules:
            log("repository contains submodules; add --recurse-submodules "
                "next time to fetch them")
            return
        log("initializing local submodules (using transferred objects)")
        result = run_local(
            ["git", "-C", str(repo), "submodule", "update",
             "--init", "--recursive"], check=False)
        if result.returncode != 0:
            warn("submodule initialization failed: " +
                 (result.stderr or result.stdout).strip()[-1000:])


def prepare_stage(dest: Path) -> Path:
    """Reject bad destinations and create a staging directory beside it."""
    if dest.exists():
        if dest.is_dir() and (dest / ".git").exists():
            raise RelayError(
                f"destination already contains a Git repository: {dest}; "
                f"update it with 'git-relay fetch {dest}' or fast-forward it "
                f"with 'git-relay pull {dest}', or choose another destination")
        if not dest.is_dir() or any(dest.iterdir()):
            raise RelayError(f"destination exists and is not empty: {dest}")
        dest.rmdir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{dest.name}.relay-",
                                 dir=dest.parent))


def default_dest(url: str) -> Path:
    """Derive a directory name from the last component of the URL."""
    name = url.rstrip("/").split("/")[-1]
    if ":" in name and "/" not in name:
        name = name.rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return Path(name or "repo")


def resolve_dest(url: str, explicit: str | Path | None,
                 options: CloneOptions, default_base: Path | None = None) -> Path:
    """Choose the destination from an explicit path, download_dir or base."""
    if explicit:
        return Path(explicit).expanduser()
    name = default_dest(url)
    # download_dir wins over the caller's base so a configured download
    # location applies to every clone unless a path is given explicitly.
    base = (Path(options.download_dir).expanduser()
            if options.download_dir else default_base)
    return base / name if base else name
