"""Command history for the git-relay CLI.

Each run appends one line to a small file and `git-relay history` reads back
the most recent records. Recording failures (read-only home directory, full
disk, ...) are swallowed so that the real command is never affected.
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Iterable

MAX_ENTRIES = 1000      # trimmed once exceeded, to bound file growth
KEEP_ENTRIES = 500
DEFAULT_SHOW = 20
ENV_PATH = "GIT_RELAY_HISTORY"


def history_path() -> Path:
    """Return the history file path, overridable via GIT_RELAY_HISTORY."""
    # Precedence: GIT_RELAY_HISTORY, then per-platform defaults
    # (LOCALAPPDATA on Windows, XDG_STATE_HOME elsewhere).
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
    """Shell-quote arguments so a recorded command can be re-run as-is."""
    return " ".join(shlex.quote(str(arg)) for arg in args)


def record(args: Iterable[str], path: Path | None = None) -> None:
    """Append one command to the history file, ignoring any IO error."""
    command = format_args(args)
    if not command.strip():
        return
    target = path or history_path()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}\t{command}\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Recorded URLs may embed access tokens, so create the file 0600.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
        _trim(target)
    except OSError:
        # History is best effort; a failed write must not fail the command.
        pass


def _trim(target: Path) -> None:
    """Keep the history file bounded by retaining only the newest entries."""
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
        # Replace atomically so a concurrent reader never sees a partial trim.
        temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        temp.replace(target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def entries(path: Path | None = None) -> list[tuple[str, str]]:
    """Return (timestamp, command) pairs ordered oldest to newest."""
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
    """Return the newest limit entries; limit <= 0 means all of them."""
    items = entries(path)
    if limit and limit > 0:
        return items[-limit:]
    return items


def clear(path: Path | None = None) -> int:
    """Delete the history file and return how many entries it held."""
    target = path or history_path()
    count = len(entries(target))
    try:
        target.unlink()
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    return count
