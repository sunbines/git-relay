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
import os
import sys
from pathlib import Path

from relay_common import (
    DEFAULT_CONFIG,
    VERSION,
    RelayError,
    configure_stdio,
    finish_progress_line,
    load_config,
    render_config,
    resolve_dest,
)
from relay_ops import (
    check,
    clone,
    fetch_remote,
    fetch_submodules,
    pull_remote,
    push_remote,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clone Git repositories through a remote Linux host "
                    "and transfer them back",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python src/git_relay.py gui\n"
            "  python src/git_relay.py check\n"
            "  python src/git_relay.py config > git-relay.toml\n"
            "  python src/git_relay.py clone https://github.com/ceph/ceph.git\n"
            "  python src/git_relay.py clone --depth 1 URL ceph\n"
            "  python src/git_relay.py clone --recurse-submodules URL\n"
            "  python src/git_relay.py submodules ceph\n"
            "  python src/git_relay.py fetch ceph\n"
            "  python src/git_relay.py pull ceph --branch main\n"
            "  python src/git_relay.py push ceph --mode direct\n"
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

    p_config = sub.add_parser(
        "config", help="print the effective configuration as TOML")
    p_config.set_defaults(func="config")

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
        remote, options, git = load_config(Path(args.config).expanduser())
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
        finish_progress_line()
        print("\n[git-relay] interrupted", file=sys.stderr)
        return 130
    except RelayError as exc:
        finish_progress_line()
        print(f"[git-relay] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
