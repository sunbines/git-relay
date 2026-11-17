#!/usr/bin/env python3
"""git-relay: clone Git repositories through a remote Linux host.

The remote host accesses the internet and runs git clone; the repository is
packed and streamed back over SSH, then extracted and checked out locally.
Useful when direct access to GitHub from this machine is slow but an
internet-facing host is available.

Examples:
    python src/git_relay.py gui
    python src/git_relay.py clone https://github.com/ceph/ceph.git ceph --depth 1
    python src/git_relay.py clone --recurse-submodules https://github.com/ceph/ceph.git
    python src/git_relay.py check
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

VERSION = "1.3"
MIB = 1 << 20


FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    # In PyInstaller one-file mode __file__ lives in a temporary extraction
    # directory, so keep the config next to the executable.
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


HERE = app_dir()
DEFAULT_CONFIG = HERE / "git-relay.toml" if FROZEN else HERE / "conf" / "git-relay.toml"
PASSWORD_ENV = "GIT_RELAY_PASSWORD"


class RelayError(RuntimeError):
    pass


def log(msg: str) -> None:
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
    print(f"[git-relay] warning: {msg}", file=sys.stderr, flush=True)


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

    def update(self, message: str, force: bool = False) -> None:
        with self._lock:
            if not force and not self._bucket.take():
                return
            if self._tty:
                sys.stderr.write(f"\r[git-relay] {message}")
                sys.stderr.flush()
                self._active = True
            else:
                print(f"[git-relay] {message}", file=sys.stderr, flush=True)

    def end(self) -> None:
        with self._lock:
            if self._active:
                sys.stderr.write("\n")
                sys.stderr.flush()
                self._active = False


def _as_int(value: object, what: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RelayError(f"config option {what} must be an integer: {value!r}")


def load_config(path: Path) -> tuple[Remote, CloneOptions]:
    data: dict = {}
    if path.exists():
        if tomllib is None:
            raise RelayError(
                "reading TOML requires Python 3.11+ (or install tomli)")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RelayError(
                f"failed to parse config file {path}: {exc}") from exc

    r = data.get("remote", {})
    c = data.get("clone", {})
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
        download_dir=os.environ.get("GIT_RELAY_DOWNLOAD_DIR",
                                    str(c.get("download_dir", ""))).strip(),
    )
    if remote.backend not in {"auto", "openssh", "paramiko"}:
        raise RelayError(
            f"ssh_backend must be auto/openssh/paramiko: {remote.backend}")
    if options.depth < 0:
        raise RelayError("depth cannot be negative")
    if options.jobs < 1:
        raise RelayError("jobs must be greater than 0")
    return remote, options


def validate_remote(remote: Remote) -> None:
    remote.host = remote.host.strip()
    remote.user = remote.user.strip()
    if not remote.host:
        raise RelayError(
            "SSH host is not configured: set it in the config file, the GUI, "
            "or the GIT_RELAY_HOST environment variable")
    if not remote.user:
        raise RelayError(
            "SSH user is not configured: set it in the config file, the GUI, "
            "or the GIT_RELAY_USER environment variable")
    if not 0 < remote.port < 65536:
        raise RelayError(f"invalid SSH port: {remote.port}")


def resolve_backend(remote: Remote) -> str:
    if remote.backend != "auto":
        return remote.backend
    # Prefer paramiko for password auth to avoid SSH_ASKPASS compatibility
    # issues on Windows.
    if remote.password and importlib.util.find_spec("paramiko") is not None:
        return "paramiko"
    return "openssh"


class Runner:
    def __init__(self, remote: Remote, verbose: bool = False) -> None:
        self.remote = remote
        self.verbose = verbose
        self.stats = RemoteStats()
        self.progress = ProgressReporter()

    def close(self) -> None:
        pass

    def run_capture(self, command: str) -> tuple[int, str, str]:
        raise NotImplementedError

    def download(self, command: str, path: Path) -> int:
        raise NotImplementedError


class OpenSSHRunner(Runner):
    def __init__(self, remote: Remote, verbose: bool = False) -> None:
        super().__init__(remote, verbose)
        self._askpass: tempfile.TemporaryDirectory[str] | None = None
        self.env = os.environ.copy()
        if remote.password:
            self._askpass = tempfile.TemporaryDirectory(
                prefix="git-relay-askpass-")
            helper = Path(self._askpass.name) / (
                "askpass.cmd" if os.name == "nt" else "askpass.sh")
            if os.name == "nt":
                helper.write_text(
                    "@echo off\r\n@echo %GIT_RELAY_PASSWORD%\r\n",
                    encoding="utf-8")
            else:
                helper.write_text(
                    "#!/bin/sh\nprintf '%s\\n' \"$GIT_RELAY_PASSWORD\"\n",
                    encoding="utf-8")
                helper.chmod(0o700)
            self.env.update({
                PASSWORD_ENV: remote.password,
                "SSH_ASKPASS": str(helper),
                "SSH_ASKPASS_REQUIRE": "force",
            })
            if os.name != "nt":
                self.env.setdefault("DISPLAY", ":0")

    def close(self) -> None:
        if self._askpass is not None:
            self._askpass.cleanup()
            self._askpass = None

    def _command(self, remote_command: str) -> list[str]:
        ssh = shutil.which("ssh")
        if ssh is None:
            raise RelayError(
                "ssh command not found; please install an OpenSSH client")
        cmd = [
            ssh,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "NumberOfPasswordPrompts=1",
        ]
        if self.remote.key:
            cmd += ["-i", str(Path(self.remote.key).expanduser())]
        if not self.remote.password:
            cmd += ["-o", "BatchMode=yes"]
        cmd += [
            "-p", str(self.remote.port),
            f"{self.remote.user}@{self.remote.host}",
            remote_command,
        ]
        return cmd

    def run_capture(self, command: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            self._command(command), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, env=self.env)
        return proc.returncode, proc.stdout, proc.stderr

    def download(self, command: str, path: Path) -> int:
        self.stats = RemoteStats()
        proc = subprocess.Popen(
            self._command(command), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=self.env)
        assert proc.stdout is not None and proc.stderr is not None
        forwarder = StderrForwarder(self.stats, self.progress)
        reader = getattr(proc.stderr, "read1", proc.stderr.read)
        thread = threading.Thread(
            target=pump_stderr, args=(reader, forwarder), daemon=True)
        thread.start()
        try:
            stream_download(proc.stdout, path, self.progress)
        finally:
            proc.stdout.close()
        rc = proc.wait()
        thread.join(timeout=5)
        forwarder.flush()
        if not thread.is_alive():
            proc.stderr.close()
        return rc


class ParamikoRunner(Runner):
    def __init__(self, remote: Remote, verbose: bool = False) -> None:
        super().__init__(remote, verbose)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import paramiko  # type: ignore[import-not-found]
        self.paramiko = paramiko

    def _connect(self):
        client = self.paramiko.SSHClient()
        # Accept the host key on first connect; switch back to RejectPolicy
        # for strict verification.
        client.set_missing_host_key_policy(self.paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self.remote.host,
            "port": self.remote.port,
            "username": self.remote.user,
            "password": self.remote.password or None,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
            "allow_agent": not self.remote.password,
            "look_for_keys": not self.remote.password,
        }
        if self.remote.key:
            kwargs["key_filename"] = str(Path(self.remote.key).expanduser())
        try:
            client.connect(**kwargs)
        except Exception as exc:
            client.close()
            raise RelayError(
                f"failed to connect to "
                f"{self.remote.user}@{self.remote.host}: {exc}") from exc
        return client

    def run_capture(self, command: str) -> tuple[int, str, str]:
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(command)
            stdin.close()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
            return rc, out, err
        finally:
            client.close()

    def download(self, command: str, path: Path) -> int:
        self.stats = RemoteStats()
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=None)
            stdin.close()
            forwarder = StderrForwarder(self.stats, self.progress)
            thread = threading.Thread(
                target=pump_stderr,
                args=(stderr.channel.recv_stderr, forwarder), daemon=True)
            thread.start()
            try:
                stream_download(ChannelReader(stdout.channel), path,
                                self.progress)
                rc = stdout.channel.recv_exit_status()
            finally:
                thread.join(timeout=5)
                forwarder.flush()
            if rc != 0 and self.verbose:
                warn("remote command failed; see the output above")
            return rc
        finally:
            client.close()


class ChannelReader:
    def __init__(self, channel) -> None:
        self.channel = channel

    def read(self, size: int) -> bytes:
        return self.channel.recv(size)


class StderrForwarder:
    MARKER = "@@git-relay@@"
    PROGRESS_RE = re.compile(
        r"^(?:remote:\s*)?"
        r"(?P<phase>[A-Za-z][A-Za-z ]*?):\s+"
        r"(?P<percent>\d+)%"
        r"(?:\s+\((?P<done>\d+)/(?P<total>\d+)\))?"
        r"(?:,\s*(?P<detail>.+))?$")

    def __init__(self, stats: RemoteStats,
                 progress: ProgressReporter) -> None:
        self.stats = stats
        self.progress = progress
        self._raw = ""
        self._in_marker = False
        self._visible = ""

    def feed(self, text: str) -> None:
        self._raw += text
        while True:
            if self._in_marker:
                newline = self._raw.find("\n")
                if newline < 0:
                    return
                self._parse(self._raw[:newline])
                self._raw = self._raw[newline + 1:]
                self._in_marker = False
                continue
            index = self._raw.find(self.MARKER)
            if index < 0:
                keep = 0
                limit = min(len(self._raw), len(self.MARKER) - 1)
                for size in range(limit, 0, -1):
                    if self.MARKER.startswith(self._raw[-size:]):
                        keep = size
                        break
                self._feed_visible(self._raw[:-keep] if keep else self._raw)
                self._raw = self._raw[-keep:] if keep else ""
                return
            if index:
                self._feed_visible(self._raw[:index])
            self._raw = self._raw[index + len(self.MARKER):]
            self._in_marker = True

    def flush(self) -> None:
        if self._in_marker:
            self._parse(self._raw)
            self._raw = ""
            self._in_marker = False
        if self._raw:
            self._feed_visible(self._raw)
            self._raw = ""
        if self._visible:
            self._handle_segment(self._visible)
            self._visible = ""
        self.progress.end()

    def _feed_visible(self, text: str) -> None:
        self._visible += text
        while True:
            match = re.search(r"[\r\n]", self._visible)
            if match is None:
                return
            segment = self._visible[:match.start()]
            self._visible = self._visible[match.end():]
            self._handle_segment(segment)

    def _handle_segment(self, segment: str) -> None:
        text = segment.strip()
        if not text:
            return
        match = self.PROGRESS_RE.match(text)
        if match is not None:
            phase = match.group("phase").strip()
            percent = int(match.group("percent"))
            message = f"{phase} {percent}%"
            done, total = match.group("done"), match.group("total")
            if done and total:
                message += f" ({done}/{total})"
            detail = (match.group("detail") or "").strip()
            if detail:
                message += f" {detail}"
            self.progress.update(
                f"clone: {message}",
                force=percent >= 100 or "done" in detail)
            return
        self.progress.end()
        sys.stderr.write(f"{text}\n")
        sys.stderr.flush()

    def _parse(self, line: str) -> None:
        for token in line.split():
            key, sep, value = token.partition("=")
            if not sep:
                continue
            try:
                if key == "repo_bytes":
                    self.stats.repo_bytes = int(value)
                elif key == "elapsed_s":
                    self.stats.clone_seconds = float(value)
            except ValueError:
                continue


def read_chunk(stream: BinaryIO, size: int) -> bytes:
    reader = getattr(stream, "read1", None)
    return reader(size) if reader is not None else stream.read(size)


def pump_stderr(read: Callable[[int], bytes],
                forwarder: StderrForwarder) -> None:
    while True:
        chunk = read(4096)
        if not chunk:
            break
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        forwarder.feed(chunk)


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


def stream_download(stream: BinaryIO, path: Path,
                    progress: ProgressReporter) -> None:
    total = 0
    started = time.monotonic()
    with open(path, "wb") as fh:
        while True:
            chunk = read_chunk(stream, MIB)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
            elapsed = time.monotonic() - started
            speed = total / elapsed if elapsed > 0 else 0.0
            progress.update(f"transfer {total / MIB:.1f} MiB "
                            f"({speed / MIB:.2f} MiB/s)")
    elapsed = time.monotonic() - started
    progress.end()
    speed = total / elapsed if elapsed > 0 else 0.0
    log(f"transfer complete: {total / MIB:.1f} MiB in {elapsed:.1f}s "
        f"({speed / MIB:.2f} MiB/s)")


def make_runner(remote: Remote, verbose: bool) -> Runner:
    backend = resolve_backend(remote)
    if backend == "paramiko":
        if importlib.util.find_spec("paramiko") is None:
            raise RelayError(
                "ssh_backend=paramiko but paramiko is not installed; run: "
                "python -m pip install paramiko")
        return ParamikoRunner(remote, verbose)
    if os.name == "nt" and remote.password:
        warn("OpenSSH password login may fail to show the askpass prompt "
             "on Windows; install paramiko "
             "(python -m pip install paramiko) or use an SSH key")
    return OpenSSHRunner(remote, verbose)


def remote_clone_command(url: str, options: CloneOptions) -> str:
    args = [options.remote_git, "clone"]
    if options.jobs > 0:
        args += ["-c", f"submodule.fetchJobs={options.jobs}"]
    args += ["--no-checkout", "--progress"]
    if options.depth > 0:
        args += ["--depth", str(options.depth)]
    if options.branch:
        args += ["--branch", options.branch]
    if options.recurse_submodules:
        args += ["--recurse-submodules"]
        if options.depth > 0:
            args += ["--shallow-submodules"]
    args += ["--", url, '"$tmp/repo"']
    clone = " ".join(shlex.quote(a) if a != '"$tmp/repo"' else a for a in args)
    lines = [
        "set -eu",
        'tmp=$(mktemp -d /tmp/git-relay.XXXXXX)',
        "trap 'rm -rf \"$tmp\"' EXIT",
        "export GIT_TERMINAL_PROMPT=0",
        "export LC_ALL=C",
        'start=$(date +%s)',
        clone,
        'end=$(date +%s)',
        'repo_bytes=$(du -sb "$tmp/repo" 2>/dev/null | cut -f1 || true)',
        '[ -n "$repo_bytes" ] || '
        'repo_bytes=$(( $(du -sk "$tmp/repo" | cut -f1) * 1024 ))',
        "printf '\\n@@git-relay@@ repo_bytes=%s elapsed_s=%s\\n' "
        '"$repo_bytes" "$((end - start))" >&2',
    ]
    if options.recurse_submodules:
        lines.append('cd "$tmp" && tar -czf - repo')
    else:
        lines.append('cd "$tmp/repo" && tar -czf - .git')
    return "\n".join(lines)


def remote_submodule_command(url: str, commit: str,
                             options: CloneOptions) -> str:
    clone = " ".join(
        shlex.quote(a) if a != '"$tmp/repo"' else a
        for a in [options.remote_git, "clone", "--no-checkout", "--progress",
                  "--", url, '"$tmp/repo"'])
    commit_arg = shlex.quote(commit)
    git = shlex.quote(options.remote_git)
    lines = [
        "set -eu",
        'tmp=$(mktemp -d /tmp/git-relay.XXXXXX)',
        "trap 'rm -rf \"$tmp\"' EXIT",
        "export GIT_TERMINAL_PROMPT=0",
        "export LC_ALL=C",
        f"{clone} >&2",
        f'if ! {git} -C "$tmp/repo" cat-file -e '
        f'{shlex.quote(commit + "^{commit}")} 2>/dev/null; then',
        f'  echo "git-relay: local commit {commit} is not on the remote" >&2',
        "  exit 4",
        "fi",
        f'{git} -C "$tmp/repo" checkout -q --detach {commit_arg} >&2',
        f'{git} -C "$tmp/repo" -c submodule.fetchJobs={options.jobs} '
        'submodule update --init --recursive >&2',
        f'{git} -C "$tmp/repo" submodule foreach --quiet '
        '\'echo "$displaypath"\' > "$tmp/paths"',
        'if [ -d "$tmp/repo/.git/modules" ]; then '
        'printf \'.git/modules\\n\' > "$tmp/list"; '
        'else : > "$tmp/list"; fi',
        'if [ -s "$tmp/paths" ]; then cat "$tmp/paths" >> "$tmp/list"; fi',
        'cd "$tmp/repo" && tar -czf - -T "$tmp/list"',
    ]
    return "\n".join(lines)


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


def fetch_submodules(dest: Path, remote: Remote, options: CloneOptions,
                     verbose: bool, dry_run: bool) -> None:
    if shutil.which("git") is None:
        raise RelayError("local git not found; please install Git")
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
    repo = Path(top.stdout.strip())
    if not (repo / ".gitmodules").exists():
        log(f"no submodules in {repo}")
        return
    status = run_local(["git", "-C", str(repo), "submodule", "status"],
                       check=False)
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        raise RelayError(f"failed to inspect submodules: {detail[-3000:]}")
    entries = [line for line in status.stdout.splitlines() if line.strip()]
    uninitialized = [line for line in entries if line.startswith("-")]
    if not entries:
        log(f"no submodules in {repo}")
        return
    if not uninitialized:
        log(f"submodules already initialized in {repo}")
        return
    if len(uninitialized) != len(entries):
        raise RelayError(
            "only some submodules are initialized; run "
            "'git submodule update --init --recursive --no-fetch' locally "
            "or remove .git/modules before retrying")
    url = run_local(["git", "-C", str(repo), "config", "--get",
                     "remote.origin.url"], check=False).stdout.strip()
    if not url:
        raise RelayError(
            "cannot read remote.origin.url; the repository was not cloned "
            "from a remote")
    commit = run_local(
        ["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    command = remote_submodule_command(url, commit, options)
    if dry_run:
        print("Remote command:")
        print(command)
        print(f"Local repository: {repo}")
        return
    validate_remote(remote)
    runner = make_runner(remote, verbose)
    try:
        with tempfile.TemporaryDirectory(prefix="git-relay-") as tmp:
            archive = Path(tmp) / "submodules.tar.gz"
            log(f"fetching submodules for {repo} via "
                f"{remote.user}@{remote.host}:{remote.port}")
            rc = runner.download(command, archive)
            if rc == 4:
                raise RelayError(
                    "the local HEAD commit is not present on the remote; "
                    "push your commits first")
            if rc != 0:
                raise RelayError(
                    f"remote submodule fetch failed (exit {rc}); check the "
                    f"repository URL and remote network")
            log("extracting submodules")
            extract_archive(archive, repo)
        log("initializing submodules locally")
        result = run_local(
            ["git", "-C", str(repo), "-c",
             f"submodule.fetchJobs={options.jobs}",
             "submodule", "update", "--init", "--recursive", "--no-fetch"],
            check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RelayError(
                f"local submodule initialization failed "
                f"(exit {result.returncode}): {detail[-3000:]}")
    finally:
        runner.close()
    log(f"submodules ready: {repo}")


def clone(url: str, dest: Path, remote: Remote, options: CloneOptions,
          verbose: bool, dry_run: bool) -> None:
    if shutil.which("git") is None:
        raise RelayError("local git not found; please install Git")
    command = remote_clone_command(url, options)
    if dry_run:
        print("Remote command:")
        print(command)
        print(f"Local destination: {dest.resolve()}")
        return
    validate_remote(remote)
    runner = make_runner(remote, verbose)
    try:
        stage = prepare_stage(dest)
        try:
            with tempfile.TemporaryDirectory(prefix="git-relay-") as tmp:
                archive = Path(tmp) / "repo.tar.gz"
                log(f"cloning {url} via "
                    f"{remote.user}@{remote.host}:{remote.port}")
                rc = runner.download(command, archive)
                if rc != 0:
                    raise RelayError(
                        f"remote clone failed (exit {rc}); check the URL, "
                        f"network, and remote git")
                log_remote_stats(runner.stats)
                log("extracting repository")
                extract_archive(archive, stage)
            if options.recurse_submodules:
                repo = stage / "repo"
                if not repo.is_dir():
                    raise RelayError(
                        "unexpected archive layout: missing repo directory")
                local_checkout(repo, options)
                os.replace(repo, dest)
                shutil.rmtree(stage, ignore_errors=True)
            else:
                local_checkout(stage, options)
                os.replace(stage, dest)
            log(f"local repository size: {format_size(dir_size(dest))}")
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    finally:
        runner.close()
    log(f"repository ready: {dest.resolve()}")


def check(remote: Remote, verbose: bool) -> int:
    validate_remote(remote)
    ok = True
    local_git = shutil.which("git")
    if local_git is None:
        warn("git not found on this machine")
        ok = False
    else:
        result = run_local(["git", "--version"], check=False)
        log(f"local: {result.stdout.strip() or result.stderr.strip()}")
    backend = resolve_backend(remote)
    log(f"SSH backend: {backend} "
        f"({remote.user}@{remote.host}:{remote.port})")
    runner = make_runner(remote, verbose)
    try:
        rc, out, err = runner.run_capture(
            "set -e; git --version; tar --version | head -n 1; "
            "command -v mktemp >/dev/null; command -v gzip >/dev/null")
    finally:
        runner.close()
    if rc != 0:
        warn("remote check failed:")
        sys.stderr.write((err or out)[-2000:])
        return 1
    for line in out.splitlines():
        log(f"remote: {line}")
    if ok:
        log("check passed")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clone Git repositories through a remote Linux host "
                    "and transfer them back",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python src/git_relay.py gui\n"
            "  python src/git_relay.py check\n"
            "  python src/git_relay.py clone https://github.com/ceph/ceph.git\n"
            "  python src/git_relay.py clone --depth 1 URL ceph\n"
            "  python src/git_relay.py clone --recurse-submodules URL\n"
            "  python src/git_relay.py submodules ceph\n"
        ))
    parser.add_argument(
        "-c", "--config", default=str(DEFAULT_CONFIG),
        help="TOML config file (source default: conf/git-relay.toml; "
             "exe default: git-relay.toml next to the executable)")
    parser.add_argument("--ssh-backend", choices=["auto", "openssh", "paramiko"],
                        help="SSH implementation (default: auto)")
    parser.add_argument("--verbose", action="store_true",
                        help="show more output")
    parser.add_argument("--version", action="version",
                        version=f"git-relay {VERSION}")
    sub = parser.add_subparsers(dest="command", required=False)

    p_gui = sub.add_parser("gui", help="open the GUI (requires tkinter)")
    p_gui.set_defaults(func="gui")

    p_check = sub.add_parser(
        "check", help="check local/remote dependencies and SSH connectivity")
    p_check.set_defaults(func="check")

    p_clone = sub.add_parser(
        "clone", help="clone on the remote host and transfer to local")
    p_clone.add_argument("url", help="Git repository URL")
    p_clone.add_argument("dest", nargs="?", help="local destination directory")
    p_clone.add_argument("--depth", type=int,
                         help="shallow clone depth; 0 means full history")
    p_clone.add_argument("--branch", help="clone only the specified branch")
    p_clone.add_argument("--recurse-submodules", action="store_true",
                         help="also fetch submodules")
    p_clone.add_argument("--jobs", type=int, help="submodule concurrency")
    p_clone.add_argument("--dry-run", action="store_true",
                         help="print the remote command without executing it")
    p_clone.set_defaults(func="clone")

    p_sub = sub.add_parser(
        "submodules",
        help="fetch submodules for an existing local clone via the remote host")
    p_sub.add_argument(
        "dest", nargs="?", default=".",
        help="existing local repository (default: current directory; a "
             "relative name is looked up under clone.download_dir)")
    p_sub.add_argument("--jobs", type=int, help="submodule concurrency")
    p_sub.add_argument("--dry-run", action="store_true",
                       help="print the remote command without executing it")
    p_sub.set_defaults(func="submodules")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        if args.command is None and os.name != "nt":
            parser.print_help()
            return 2
        try:
            from git_relay_gui import run_gui
            return run_gui(Path(args.config).expanduser(),
                           backend_override=args.ssh_backend,
                           verbose=args.verbose)
        except Exception as exc:
            print(f"[git-relay] error: failed to start the GUI: {exc}",
                  file=sys.stderr)
            return 1
    try:
        remote, options = load_config(Path(args.config).expanduser())
        if args.ssh_backend:
            remote.backend = args.ssh_backend
        if args.command == "clone":
            if args.depth is not None:
                options.depth = args.depth
            if args.branch:
                options.branch = args.branch
            if args.recurse_submodules:
                options.recurse_submodules = True
            if args.jobs is not None:
                options.jobs = args.jobs
            if options.depth < 0:
                raise RelayError("depth cannot be negative")
            if options.jobs < 1:
                raise RelayError("jobs must be greater than 0")
            dest = resolve_dest(args.url, args.dest, options)
            clone(args.url, dest, remote, options, args.verbose, args.dry_run)
            return 0
        if args.command == "submodules":
            if args.jobs is not None:
                options.jobs = args.jobs
            if options.jobs < 1:
                raise RelayError("jobs must be greater than 0")
            fetch_submodules(Path(args.dest).expanduser(), remote, options,
                             args.verbose, args.dry_run)
            return 0
        return check(remote, args.verbose)
    except KeyboardInterrupt:
        print("\n[git-relay] interrupted", file=sys.stderr)
        return 130
    except RelayError as exc:
        print(f"[git-relay] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
