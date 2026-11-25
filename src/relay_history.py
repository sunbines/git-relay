"""Command history for the git-relay CLI.

每次运行向一个小文件追加一行，`git-relay history` 读取最近的记录。
记录失败（只读家目录、磁盘满等）一律静默，不能影响真正的命令。
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Iterable

MAX_ENTRIES = 1000      # 超过后裁剪，避免文件无限增长
KEEP_ENTRIES = 500
DEFAULT_SHOW = 20
ENV_PATH = "GIT_RELAY_HISTORY"


def history_path() -> Path:
    """历史文件位置，可用 GIT_RELAY_HISTORY 覆盖。"""
    override = os.environ.get(ENV_PATH)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "git-relay" / "history"
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return root / "git-relay" / "history"


def format_args(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def record(args: Iterable[str], path: Path | None = None) -> None:
    """追加一条命令；任何 IO 错误都忽略。"""
    command = format_args(args)
    if not command.strip():
        return
    target = path or history_path()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}\t{command}\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 历史里可能出现带 token 的 URL，文件按 0600 创建
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
        _trim(target)
    except OSError:
        pass


def _trim(target: Path) -> None:
    try:
        lines = target.read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_ENTRIES:
        return
    kept = lines[-KEEP_ENTRIES:]
    temp = target.with_name(target.name + ".tmp")
    try:
        temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        temp.replace(target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def entries(path: Path | None = None) -> list[tuple[str, str]]:
    """返回 [(时间戳, 命令)]，按时间从旧到新。"""
    target = path or history_path()
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        stamp, sep, command = line.partition("\t")
        if not sep or not command.strip():
            continue
        out.append((stamp, command))
    return out


def recent(limit: int = DEFAULT_SHOW,
           path: Path | None = None) -> list[tuple[str, str]]:
    """最近 limit 条；limit <= 0 表示全部。"""
    items = entries(path)
    if limit and limit > 0:
        return items[-limit:]
    return items


def clear(path: Path | None = None) -> int:
    """删除历史文件，返回被删除的条数。"""
    target = path or history_path()
    count = len(entries(target))
    try:
        target.unlink()
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    return count
