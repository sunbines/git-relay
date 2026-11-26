#!/usr/bin/env python3
"""git-relay: clone Git repositories through a remote Linux host.

Linux/terminal client: parses the command line and dispatches subcommands. The
``gui`` subcommand hands off to the Windows interface in ``src/win11``; the
shared relay engine lives in ``src/common``.

The remote host accesses the internet and runs git clone; the repository is
packed and streamed back over SSH, then extracted and checked out locally.
Useful when direct access to GitHub from this machine is slow but an
internet-facing host is available.

Examples:
    git-relay gui
    git-relay clone https://github.com/ceph/ceph.git ceph --depth 1
    git-relay clone --recurse-submodules https://github.com/ceph/ceph.git
    git-relay check
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Running this file as a script puts src/linux on sys.path; add src/ itself so
# the package imports below also resolve from a plain source checkout.
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common.relay_common import (
    DEFAULT_CONFIG,
    VERSION,
    RelayError,
    configure_stdio,
    finish_progress_line,
    load_config,
    render_config,
    resolve_dest,
)
from common.relay_ops import (
    check,
    clone,
    fetch_remote,
    fetch_submodules,
    pull_remote,
    push_remote,
)
from linux import relay_history


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every subcommand wired to ``args.func``."""
    parser = argparse.ArgumentParser(
        prog="git-relay",
        description="Clone Git repositories through a remote Linux host "
                    "and transfer them back",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  git-relay gui\n"
            "  git-relay check\n"
            "  git-relay config > git-relay.toml\n"
            "  git-relay clone https://github.com/ceph/ceph.git\n"
            "  git-relay clone --depth 1 URL ceph\n"
            "  git-relay clone --recurse-submodules URL\n"
            "  git-relay submodules ceph\n"
            "  git-relay fetch ceph\n"
            "  git-relay pull ceph --branch main\n"
            "  git-relay push ceph --mode direct\n"
            "  git-relay history -n 20\n"
        ))
    parser.add_argument(
        "-c", "--config", default=None,
        help="TOML config file (default: /etc/git-relay/git-relay.toml, "
             "GUI uses %%APPDATA%%\\git-relay\\git-relay.toml on Windows)")
    parser.add_argument("--ssh-backend", choices=["auto", "openssh", "paramiko"],
                        help="SSH implementation (default: auto)")
    parser.add_argument("--verbose", action="store_true",
                        help="show more output")
    parser.add_argument("--version", action="version",
                        version=f"git-relay {VERSION}")
    sub = parser.add_subparsers(dest="command", required=False)

    # Each subparser stores its command name in ``func``; main() dispatches on
    # that value, so no handler callables need to be registered here.
    p_gui = sub.add_parser("gui", help="open the GUI (requires tkinter)")
    p_gui.set_defaults(func="gui")

    p_check = sub.add_parser(
        "check", help="check local/remote dependencies and SSH connectivity")
    p_check.set_defaults(func="check")

    p_config = sub.add_parser(
        "config", help="print the effective configuration as TOML")
    p_config.set_defaults(func="config")

    p_history = sub.add_parser(
        "history", help="show recently executed git-relay commands")
    p_history.add_argument(
        "-n", "--limit", type=int, default=relay_history.DEFAULT_SHOW,
        help=f"entries to show, 0 for all "
             f"(default: {relay_history.DEFAULT_SHOW})")
    p_history.add_argument(
        "--clear", action="store_true", help="delete the recorded history")
    p_history.add_argument(
        "--path", action="store_true",
        help="print the history file path and exit")
    p_history.set_defaults(func="history")

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

    for action, help_text in (
            ("fetch", "fetch updates for an existing local repository"),
            ("pull", "fetch then fast-forward the current branch"),
            ("push", "push a local branch through the remote host")):
        p_sync = sub.add_parser(action, help=help_text)
        p_sync.add_argument(
            "dest", nargs="?", default=".",
            help="existing local repository (default: current directory; a "
                 "relative name is looked up under clone.download_dir)")
        p_sync.add_argument(
            "--branch", help="branch to pull/push (default: current branch)")
        p_sync.add_argument(
            "--remote", help="git remote name (default: git.remote, "
                             "usually origin)")
        p_sync.add_argument("--mode", choices=["relay", "direct"],
                            help="override git.mode for this run")
        p_sync.add_argument("--dry-run", action="store_true",
                            help="print the command without executing it")
        p_sync.set_defaults(func=action)
    return parser


def show_history(args) -> int:
    """Handle the history subcommand: print, clear, or locate the log."""
    path = relay_history.history_path()
    if args.path:
        print(path, flush=True)
        return 0
    if args.clear:
        removed = relay_history.clear(path)
        if removed:
            print(f"[git-relay] removed {removed} history entries: {path}",
                  flush=True)
        else:
            print("[git-relay] history is already empty", flush=True)
        return 0
    limit = args.limit if args.limit and args.limit > 0 else 0
    items = relay_history.recent(limit, path)
    if not items:
        print("[git-relay] no history yet; run a command first", flush=True)
        return 0
    for stamp, command in items:
        print(f"{stamp}  git-relay {command}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dispatch the requested command, and return an exit code."""
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        if args.command is None and os.name != "nt":
            parser.print_help()
            return 2
        try:
            from win11.git_relay_gui import run_gui
            # Without an explicit -c the GUI picks the per-platform default
            # (%APPDATA% on Windows, /etc/git-relay elsewhere).
            explicit = Path(args.config).expanduser() if args.config else None
            return run_gui(explicit,
                           backend_override=args.ssh_backend,
                           verbose=args.verbose)
        except Exception as exc:
            print(f"[git-relay] error: failed to start the GUI: {exc}",
                  file=sys.stderr)
            return 1

    if args.command == "history":
        return show_history(args)

    # History is recorded for every command except history itself, and before
    # the config is loaded so a broken config still leaves a trace.
    if args.command:
        relay_history.record(argv if argv is not None else sys.argv[1:])

    try:
        config_path = (Path(args.config).expanduser()
                       if args.config else DEFAULT_CONFIG)
        remote, options, git = load_config(config_path)
        if args.ssh_backend:
            remote.backend = args.ssh_backend
        if args.command == "clone":
            # CLI flags override the config file only when explicitly given.
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
        if args.command == "check":
            return check(remote, args.verbose)
        if args.command == "config":
            print(render_config(remote, options, git), end="")
            return 0
        sync = {"fetch": fetch_remote, "pull": pull_remote,
                "push": push_remote}[args.command]
        sync(Path(args.dest).expanduser(), remote, options, git,
             branch=args.branch or "", name=args.remote or "",
             mode=args.mode or "", verbose=args.verbose,
             dry_run=args.dry_run)
        return 0
    except KeyboardInterrupt:
        # 130 is the conventional exit status for SIGINT.
        finish_progress_line()
        print("\n[git-relay] interrupted", file=sys.stderr)
        return 130
    except RelayError as exc:
        # Expected operational failures exit 1; unexpected ones propagate.
        finish_progress_line()
        print(f"[git-relay] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
