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

from toml_parse import parse_toml

VERSION = "1.3"
MIB = 1 << 20
DEFAULT_DOWNLOAD_DIR = "/root"
PASSWORD_ENV = "GIT_RELAY_PASSWORD"

FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    # In PyInstaller one-file mode __file__ lives in a temporary extraction
    # directory, so keep the config next to the executable.
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


HERE = app_dir()
DEFAULT_CONFIG = HERE / "git-relay.toml" if FROZEN else HERE / "conf" / "git-relay.toml"


class RelayError(RuntimeError):
    pass


_active_reporter: "ProgressReporter | None" = None


def finish_progress_line() -> None:
    # Status lines are written with a bare "\r" and no newline, so any other
    # output must close the line first or it would be appended to it.
    reporter = _active_reporter
    if reporter is not None:
        reporter.end()


def log(msg: str) -> None:
    finish_progress_line()
    print(f"[git-relay] {msg}", flush=True)


def configure_stdio() -> None:
    # Legacy Windows consoles and redirected streams may default to a
    # non-UTF-8 encoding, which can break non-ASCII output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def warn(msg: str) -> None:
    finish_progress_line()
    print(f"[git-relay] warning: {msg}", file=sys.stderr, flush=True)


@dataclass
class RemoteStats:
    repo_bytes: int = 0
    clone_seconds: float = 0.0


class TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self._tokens = min(
            self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class ProgressReporter:
    def __init__(self, rate: float = 4.0, capacity: float = 2.0) -> None:
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
        with self._lock:
            if not force and not self._bucket.take():
                return
            if self._tty:
                self._write_status(f"[git-relay] {message}")
            else:
                print(f"[git-relay] {message}", file=sys.stderr, flush=True)

    def write_line(self, text: str) -> None:
        with self._lock:
            self._finish_locked()
            sys.stderr.write(f"{text}\n")
            sys.stderr.flush()

    def end(self) -> None:
        with self._lock:
            self._finish_locked()

    def _write_status(self, line: str) -> None:
        if len(line) > self._width:
            line = line[:self._width]
        padding = " " * max(0, self._last_width - len(line))
        sys.stderr.write(f"\r{line}{padding}")
        sys.stderr.flush()
        self._last_width = len(line)
        self._active = True

    def _finish_locked(self) -> None:
        if self._active:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._active = False
            self._last_width = 0


def dir_size(path: Path) -> int:
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
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def log_remote_stats(stats: RemoteStats) -> None:
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
    host: str = ""
    user: str = ""
    port: int = 22
    password: str = ""
    key: str = ""
    backend: str = "auto"


@dataclass
class CloneOptions:
    depth: int = 0
    branch: str = ""
    recurse_submodules: bool = False
    jobs: int = 4
    remote_git: str = "git"
    download_dir: str = ""


@dataclass
class GitOptions:
    mode: str = "relay"      # relay: via the remote host; direct: local git
    remote: str = "origin"   # local git remote name
    cache_dir: str = ""      # mirror cache on the remote host


def _as_int(value: object, what: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RelayError(f"config option {what} must be an integer: {value!r}")


def load_config(path: Path) -> tuple[Remote, CloneOptions, GitOptions]:
    data: dict = {}
    if path.exists():
        try:
            data = parse_toml(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RelayError(
                f"failed to parse config file {path}: {exc}") from exc

    r = data.get("remote", {})
    c = data.get("clone", {})
    g = data.get("git", {})
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
    escaped = (value.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t"))
    return f'"{escaped}"'


def render_config(remote: Remote, options: CloneOptions,
                  git: GitOptions) -> str:
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
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RelayError(f"local command failed (exit {proc.returncode}): "
                         f"{' '.join(cmd)}\n{detail[-3000:]}")
    return proc


def run_local_stream(cmd: list[str], cwd: Path | None = None) -> int:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, env=env)
    return proc.returncode


def resolve_local_repo(dest: Path, options: CloneOptions) -> Path:
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
        base = PurePosixPath(name).parent if member.issym() else PurePosixPath()
        resolved = PurePosixPath(posixpath.normpath(str(base / target)))
        if (target.is_absolute()
                or (target.parts and ":" in target.parts[0])
                or resolved.is_absolute()
                or (resolved.parts and resolved.parts[0] == "..")):
            raise RelayError(f"archive contains an unsafe link: {member.name}")


def extract_archive(archive: Path, dest: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tf:
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
    run_local(["git", "-C", str(repo), "checkout"])
    if not (repo / ".gitmodules").exists():
        return
    status = run_local(["git", "-C", str(repo), "submodule", "status"],
                       check=False)
    if status.returncode != 0:
        return
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
    name = url.rstrip("/").split("/")[-1]
    if ":" in name and "/" not in name:
        name = name.rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return Path(name or "repo")


def resolve_dest(url: str, explicit: str | Path | None,
                 options: CloneOptions, default_base: Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    name = default_dest(url)
    base = (Path(options.download_dir).expanduser()
            if options.download_dir else default_base)
    return base / name if base else name
