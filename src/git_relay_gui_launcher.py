from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path


def _report_error(exc: BaseException) -> None:
    detail = "".join(traceback.format_exception(exc))
    try:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent.parent
        (base / "git-relay-error.log").write_text(detail, encoding="utf-8")
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, f"启动图形界面失败：\n\n{exc}", "git-relay", 0x10)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-c", "--config")
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
