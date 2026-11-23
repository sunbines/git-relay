"""SSH backends and streaming transfer."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import warnings
from pathlib import Path
from typing import BinaryIO, Callable

from relay_common import (
    MIB,
    PASSWORD_ENV,
    ProgressReporter,
    RelayError,
    Remote,
    RemoteStats,
    log,
    warn,
)


def read_chunk(stream: BinaryIO, size: int) -> bytes:
    reader = getattr(stream, "read1", None)
    return reader(size) if reader is not None else stream.read(size)


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
    if total:
        speed = total / elapsed if elapsed > 0 else 0.0
        log(f"transfer complete: {total / MIB:.1f} MiB in {elapsed:.1f}s "
            f"({speed / MIB:.2f} MiB/s)")


def stream_upload(stream: BinaryIO, path: Path,
                  progress: ProgressReporter) -> None:
    total = 0
    started = time.monotonic()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(MIB)
            if not chunk:
                break
            stream.write(chunk)
            stream.flush()
            total += len(chunk)
            elapsed = time.monotonic() - started
            speed = total / elapsed if elapsed > 0 else 0.0
            progress.update(f"upload {total / MIB:.1f} MiB "
                            f"({speed / MIB:.2f} MiB/s)")
    elapsed = time.monotonic() - started
    progress.end()
    speed = total / elapsed if elapsed > 0 else 0.0
    log(f"upload complete: {total / MIB:.1f} MiB in {elapsed:.1f}s "
        f"({speed / MIB:.2f} MiB/s)")


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

    def upload(self, command: str, path: Path) -> int:
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

    def upload(self, command: str, path: Path) -> int:
        self.stats = RemoteStats()
        proc = subprocess.Popen(
            self._command(command), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        assert (proc.stdin is not None and proc.stdout is not None
                and proc.stderr is not None)
        forwarder = StderrForwarder(self.stats, self.progress)
        reader = getattr(proc.stderr, "read1", proc.stderr.read)
        thread = threading.Thread(
            target=pump_stderr, args=(reader, forwarder), daemon=True)
        thread.start()
        drain = threading.Thread(
            target=drain_stream, args=(proc.stdout,), daemon=True)
        drain.start()
        try:
            stream_upload(proc.stdin, path, self.progress)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        rc = proc.wait()
        thread.join(timeout=5)
        drain.join(timeout=5)
        forwarder.flush()
        if not thread.is_alive():
            proc.stderr.close()
        if not drain.is_alive():
            proc.stdout.close()
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

    def upload(self, command: str, path: Path) -> int:
        self.stats = RemoteStats()
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=None)
            forwarder = StderrForwarder(self.stats, self.progress)
            thread = threading.Thread(
                target=pump_stderr,
                args=(stderr.channel.recv_stderr, forwarder), daemon=True)
            thread.start()
            drain = threading.Thread(
                target=drain_channel, args=(stdout.channel,), daemon=True)
            drain.start()
            try:
                stream_upload(stdin, path, self.progress)
            finally:
                try:
                    stdin.close()
                except OSError:
                    pass
                thread.join(timeout=5)
                drain.join(timeout=5)
                forwarder.flush()
            rc = stdout.channel.recv_exit_status()
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
        self.progress.write_line(text)

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


def pump_stderr(read: Callable[[int], bytes],
                forwarder: StderrForwarder) -> None:
    while True:
        chunk = read(4096)
        if not chunk:
            break
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        forwarder.feed(chunk)


def drain_stream(stream: BinaryIO) -> None:
    try:
        while stream.read(65536):
            pass
    except (OSError, ValueError):
        pass


def drain_channel(channel) -> None:
    try:
        while channel.recv(65536):
            pass
    except (OSError, EOFError):
        pass


def make_runner(remote: Remote, verbose: bool) -> Runner:
    backend = resolve_backend(remote)
    if backend == "paramiko":
        if importlib.util.find_spec("paramiko") is None:
            raise RelayError(
                "ssh_backend=paramiko but paramiko is not installed; install "
                "python3-paramiko (openEuler) or run: "
                "python3 -m pip install paramiko")
        return ParamikoRunner(remote, verbose)
    if os.name == "nt" and remote.password:
        warn("OpenSSH password login may fail to show the askpass prompt "
             "on Windows; install paramiko "
             "(python -m pip install paramiko) or use an SSH key")
    return OpenSSHRunner(remote, verbose)
