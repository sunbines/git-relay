#!/usr/bin/env python3
"""git-relay: 通过远程 Linux 主机克隆 Git 仓库并回传到本地。

远端主机负责访问外网执行 git clone，仓库打包后经 SSH 流式传回本地，
本地解压并 checkout。适合本机直连 GitHub 很慢、但有一台外网主机可用的场景。

示例：
    python src/git_relay.py gui
    python src/git_relay.py clone https://github.com/ceph/ceph.git ceph --depth 1
    python src/git_relay.py clone --recurse-submodules https://github.com/ceph/ceph.git
    python src/git_relay.py check
"""

from __future__ import annotations

import argparse
import importlib.util
import os
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
from typing import BinaryIO

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

VERSION = "1.2"


FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    # PyInstaller 单文件模式下 __file__ 位于临时解压目录，配置要放在 exe 旁边。
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
    # Windows 旧控制台/重定向时默认编码可能不是 UTF-8，中文日志会崩溃。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def warn(msg: str) -> None:
    print(f"[git-relay] 警告：{msg}", file=sys.stderr, flush=True)


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


def _as_int(value: object, what: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RelayError(f"配置项 {what} 必须是整数：{value!r}")


def load_config(path: Path) -> tuple[Remote, CloneOptions]:
    data: dict = {}
    if path.exists():
        if tomllib is None:
            raise RelayError("读取 TOML 需要 Python 3.11+（或安装 tomli）")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RelayError(f"解析配置文件失败 {path}：{exc}") from exc

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
    )
    if remote.backend not in {"auto", "openssh", "paramiko"}:
        raise RelayError(f"ssh_backend 只能是 auto/openssh/paramiko：{remote.backend}")
    if options.depth < 0:
        raise RelayError("depth 不能为负数")
    if options.jobs < 1:
        raise RelayError("jobs 必须大于 0")
    return remote, options


def validate_remote(remote: Remote) -> None:
    remote.host = remote.host.strip()
    remote.user = remote.user.strip()
    if not remote.host:
        raise RelayError(
            "未配置 SSH 服务器地址：请在配置文件、图形界面或 "
            "GIT_RELAY_HOST 环境变量中设置")
    if not remote.user:
        raise RelayError(
            "未配置 SSH 用户：请在配置文件、图形界面或 "
            "GIT_RELAY_USER 环境变量中设置")
    if not 0 < remote.port < 65536:
        raise RelayError(f"SSH 端口不合法：{remote.port}")


def resolve_backend(remote: Remote) -> str:
    if remote.backend != "auto":
        return remote.backend
    # 密码登录时优先用 paramiko，避免 Windows 上 SSH_ASKPASS 的兼容性问题。
    if remote.password and importlib.util.find_spec("paramiko") is not None:
        return "paramiko"
    return "openssh"


class Runner:
    def __init__(self, remote: Remote, verbose: bool = False) -> None:
        self.remote = remote
        self.verbose = verbose

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
            raise RelayError("找不到 ssh 命令，请安装 OpenSSH 客户端")
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
        proc = subprocess.Popen(
            self._command(command), stdout=subprocess.PIPE,
            stderr=None, stdin=subprocess.DEVNULL, env=self.env)
        assert proc.stdout is not None
        try:
            stream_download(proc.stdout, path)
        finally:
            proc.stdout.close()
        return proc.wait()


class ParamikoRunner(Runner):
    def __init__(self, remote: Remote, verbose: bool = False) -> None:
        super().__init__(remote, verbose)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import paramiko  # type: ignore[import-not-found]
        self.paramiko = paramiko

    def _connect(self):
        client = self.paramiko.SSHClient()
        # 首次连接自动接受主机密钥；如需严格校验，请改回 RejectPolicy。
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
                f"SSH 连接 {self.remote.user}@{self.remote.host} 失败：{exc}") from exc
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
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=None)
            stdin.close()
            errors: list[bytes] = []

            def drain() -> None:
                while True:
                    line = stderr.readline()
                    if not line:
                        break
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    errors.append(line.encode("utf-8", "replace"))
                    sys.stderr.write(line)
                    sys.stderr.flush()

            thread = threading.Thread(target=drain, daemon=True)
            thread.start()
            try:
                stream_download(ChannelReader(stdout.channel), path)
                rc = stdout.channel.recv_exit_status()
            finally:
                thread.join(timeout=2)
            if rc != 0 and errors and self.verbose:
                warn("远端 stderr 已输出到终端")
            return rc
        finally:
            client.close()


class ChannelReader:
    def __init__(self, channel) -> None:
        self.channel = channel

    def read(self, size: int) -> bytes:
        return self.channel.recv(size)


def stream_download(stream: BinaryIO, path: Path) -> None:
    total = 0
    last = 0.0
    tty = sys.stderr.isatty()
    with open(path, "wb") as fh:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
            now = time.monotonic()
            if tty and now - last >= 0.5:
                print(f"\r[git-relay] 传输 {total / 1048576:.1f} MiB",
                      end="", file=sys.stderr, flush=True)
                last = now
    if tty:
        print(file=sys.stderr)
    log(f"传输完成：{total / 1048576:.1f} MiB")


def make_runner(remote: Remote, verbose: bool) -> Runner:
    backend = resolve_backend(remote)
    if backend == "paramiko":
        if importlib.util.find_spec("paramiko") is None:
            raise RelayError(
                "ssh_backend=paramiko 但未安装 paramiko，请执行："
                "python -m pip install paramiko")
        return ParamikoRunner(remote, verbose)
    if os.name == "nt" and remote.password:
        warn("Windows 下 OpenSSH 的密码登录可能无法弹出 askpass；"
             "建议安装 paramiko（python -m pip install paramiko）或改用 SSH 密钥")
    return OpenSSHRunner(remote, verbose)


def remote_clone_command(url: str, options: CloneOptions) -> str:
    args = [options.remote_git, "clone"]
    if options.jobs > 0:
        args += ["-c", f"submodule.fetchJobs={options.jobs}"]
    args += ["--no-checkout"]
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
        clone,
    ]
    if options.recurse_submodules:
        lines.append('cd "$tmp" && tar -czf - repo')
    else:
        lines.append('cd "$tmp/repo" && tar -czf - .git')
    return "\n".join(lines)


def validate_member(member: tarfile.TarInfo) -> None:
    name = member.name.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RelayError(f"压缩包包含不安全路径：{member.name}")
    if path.parts and ":" in path.parts[0]:
        raise RelayError(f"压缩包包含不安全路径：{member.name}")
    if member.isdev() or member.isfifo():
        raise RelayError(f"压缩包包含特殊文件：{member.name}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname.replace("\\", "/"))
        if target.is_absolute() or ".." in target.parts:
            raise RelayError(f"压缩包包含不安全链接：{member.name}")


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
            hint = "；Windows 如因符号链接失败，可启用开发者模式或管理员终端"
        raise RelayError(f"解压仓库失败：{exc}{hint}") from exc


def run_local(cmd: list[str], cwd: Path | None = None,
              check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RelayError(f"本地命令失败（exit {proc.returncode}）："
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
            log("仓库包含子模块，可下次加 --recurse-submodules 一并拉取")
            return
        log("初始化本地子模块（使用已回传的对象）")
        result = run_local(
            ["git", "-C", str(repo), "submodule", "update",
             "--init", "--recursive"], check=False)
        if result.returncode != 0:
            warn("子模块初始化失败：" +
                 (result.stderr or result.stdout).strip()[-1000:])


def prepare_stage(dest: Path) -> Path:
    if dest.exists():
        if not dest.is_dir() or any(dest.iterdir()):
            raise RelayError(f"目标目录已存在且不为空：{dest}")
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


def clone(url: str, dest: Path, remote: Remote, options: CloneOptions,
          verbose: bool, dry_run: bool) -> None:
    if shutil.which("git") is None:
        raise RelayError("找不到本地 git，请安装 Git")
    command = remote_clone_command(url, options)
    if dry_run:
        print("远端命令：")
        print(command)
        print(f"本地目标：{dest.resolve()}")
        return
    validate_remote(remote)
    runner = make_runner(remote, verbose)
    try:
        stage = prepare_stage(dest)
        try:
            with tempfile.TemporaryDirectory(prefix="git-relay-") as tmp:
                archive = Path(tmp) / "repo.tar.gz"
                log(f"通过 {remote.user}@{remote.host}:{remote.port} 克隆 {url}")
                rc = runner.download(command, archive)
                if rc != 0:
                    raise RelayError(
                        f"远端 clone 失败（exit {rc}），请检查 URL、网络和远端 git")
                log("解压仓库")
                extract_archive(archive, stage)
            if options.recurse_submodules:
                repo = stage / "repo"
                if not repo.is_dir():
                    raise RelayError("压缩包结构异常：缺少 repo 目录")
                local_checkout(repo, options)
                os.replace(repo, dest)
                shutil.rmtree(stage, ignore_errors=True)
            else:
                local_checkout(stage, options)
                os.replace(stage, dest)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    finally:
        runner.close()
    log(f"仓库已就绪：{dest.resolve()}")


def check(remote: Remote, verbose: bool) -> int:
    validate_remote(remote)
    ok = True
    local_git = shutil.which("git")
    if local_git is None:
        warn("本机未找到 git")
        ok = False
    else:
        result = run_local(["git", "--version"], check=False)
        log(f"本机：{result.stdout.strip() or result.stderr.strip()}")
    backend = resolve_backend(remote)
    log(f"SSH 后端：{backend}（{remote.user}@{remote.host}:{remote.port}）")
    runner = make_runner(remote, verbose)
    try:
        rc, out, err = runner.run_capture(
            "set -e; git --version; tar --version | head -n 1; "
            "command -v mktemp >/dev/null; command -v gzip >/dev/null")
    finally:
        runner.close()
    if rc != 0:
        warn("远端检查失败：")
        sys.stderr.write((err or out)[-2000:])
        return 1
    for line in out.splitlines():
        log(f"远端：{line}")
    if ok:
        log("检查通过")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过远程 Linux 主机克隆 Git 仓库并回传本地",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python src/git_relay.py gui\n"
            "  python src/git_relay.py check\n"
            "  python src/git_relay.py clone https://github.com/ceph/ceph.git\n"
            "  python src/git_relay.py clone --depth 1 URL ceph\n"
            "  python src/git_relay.py clone --recurse-submodules URL\n"
        ))
    parser.add_argument(
        "-c", "--config", default=str(DEFAULT_CONFIG),
        help="TOML 配置文件（源码默认 conf/git-relay.toml，exe 默认同目录 git-relay.toml）")
    parser.add_argument("--ssh-backend", choices=["auto", "openssh", "paramiko"],
                        help="SSH 实现，默认 auto")
    parser.add_argument("--verbose", action="store_true", help="输出更多信息")
    parser.add_argument("--version", action="version",
                        version=f"git-relay {VERSION}")
    sub = parser.add_subparsers(dest="command", required=False)

    p_gui = sub.add_parser("gui", help="打开图形界面（需要 tkinter）")
    p_gui.set_defaults(func="gui")

    p_check = sub.add_parser("check", help="检查本机/远端依赖和 SSH 连通性")
    p_check.set_defaults(func="check")

    p_clone = sub.add_parser("clone", help="远端克隆并回传到本地")
    p_clone.add_argument("url", help="Git 仓库 URL")
    p_clone.add_argument("dest", nargs="?", help="本地目标目录")
    p_clone.add_argument("--depth", type=int, help="浅克隆深度，0 表示完整历史")
    p_clone.add_argument("--branch", help="只克隆指定分支")
    p_clone.add_argument("--recurse-submodules", action="store_true",
                         help="同时拉取子模块")
    p_clone.add_argument("--jobs", type=int, help="子模块并发数")
    p_clone.add_argument("--dry-run", action="store_true",
                         help="只打印远端命令，不执行")
    p_clone.set_defaults(func="clone")
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
            print(f"[git-relay] 错误：无法启动图形界面：{exc}", file=sys.stderr)
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
                raise RelayError("depth 不能为负数")
            if options.jobs < 1:
                raise RelayError("jobs 必须大于 0")
            dest = Path(args.dest) if args.dest else default_dest(args.url)
            clone(args.url, dest, remote, options, args.verbose, args.dry_run)
            return 0
        return check(remote, args.verbose)
    except KeyboardInterrupt:
        print("\n[git-relay] 已中断", file=sys.stderr)
        return 130
    except RelayError as exc:
        print(f"[git-relay] 错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
