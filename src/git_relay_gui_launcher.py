from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path


def _report_error(exc: BaseException) -> None:
    """Write the traceback to a log file and try to show a Windows message box."""
    detail = "".join(traceback.format_exception(exc))
    try:
        # A frozen exe has no source tree, so log next to the executable;
        # otherwise use the project root above src/.
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent.parent
        (base / "git-relay-error.log").write_text(detail, encoding="utf-8")
    except Exception:
        # Logging is best effort; never mask the original startup failure.
        pass
    try:
        import ctypes

        # GUI apps have no console, so surface the error in a dialog instead.
        ctypes.windll.user32.MessageBoxW(
            None, f"启动图形界面失败：\n\n{exc}", "git-relay", 0x10)
    except Exception:
        # Not running on Windows; the log file remains the only report.
        pass


def main() -> int:
    """Start the GUI, reporting startup failures instead of crashing silently."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-c", "--config")
    # Unknown arguments are ignored so launcher-only flags cannot break startup.
    args, _ = parser.parse_known_args()
    try:
        from git_relay_gui import run_gui

        config = Path(args.config).expanduser() if args.config else None
        return run_gui(config)
    except Exception as exc:
        _report_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
