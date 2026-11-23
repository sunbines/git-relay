"""High-level operations: clone, submodules, fetch/pull/push and checks."""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from toml_parse import toml_backend

from relay_common import (
    CloneOptions,
    GitOptions,
    RelayError,
    Remote,
    dir_size,
    extract_archive,
    format_size,
    local_checkout,
    log,
    log_remote_stats,
    prepare_stage,
    resolve_local_repo,
    run_local,
    run_local_stream,
    warn,
)
from relay_ssh import make_runner, resolve_backend, validate_remote


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


def mirror_prelude(url: str, git: GitOptions) -> list[str]:
    lines = [
        "set -eu",
        "export GIT_TERMINAL_PROMPT=0",
        "export LC_ALL=C",
    ]
    if git.cache_dir:
        lines.append(f"cache={shlex.quote(git.cache_dir)}")
    else:
        lines.append('cache="${GIT_RELAY_CACHE_DIR:-$HOME/.cache/git-relay}"')
    lines += [
        'mkdir -p "$cache"',
        f"url={shlex.quote(url)}",
        "key=$(printf '%s' \"$url\" | git hash-object --stdin)",
        'dir="$cache/$key.git"',
        'if [ ! -d "$dir" ]; then',
        '  git clone --bare -- "$url" "$dir" >&2',
        "fi",
    ]
    return lines


def remote_fetch_command(url: str, git: GitOptions,
                         prereqs: list[str]) -> str:
    lines = mirror_prelude(url, git)
    lines += [
        'git -C "$dir" fetch --prune origin '
        "'+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*' >&2",
        'args=""',
    ]
    if prereqs:
        shas = " ".join(shlex.quote(s) for s in prereqs)
        lines += [
            f"for s in {shas}; do",
            '  if git -C "$dir" cat-file -e "$s^{commit}" 2>/dev/null; then',
            '    args="$args ^$s"',
            "  fi",
            "done",
        ]
    lines += [
        'bundle=$(mktemp /tmp/git-relay-fetch.XXXXXX)',
        'err=$(mktemp /tmp/git-relay-fetch.XXXXXX)',
        "trap 'rm -f \"$bundle\" \"$err\"' EXIT",
        'if ! git -C "$dir" bundle create "$bundle" '
        '--branches --tags $args 2>"$err"; then',
        "  if grep -qi 'empty bundle' \"$err\"; then exit 3; fi",
        '  cat "$err" >&2',
        "  exit 1",
        "fi",
        'cat "$bundle"',
    ]
    return "\n".join(lines)


def remote_push_command(url: str, git: GitOptions, branch: str) -> str:
    lines = mirror_prelude(url, git)
    refspec = shlex.quote(f"refs/git-relay/push/{branch}:refs/heads/{branch}")
    temp_ref = shlex.quote(f"refs/git-relay/push/{branch}")
    lines += [
        'bundle=$(mktemp /tmp/git-relay-push.XXXXXX)',
        "trap 'rm -f \"$bundle\"; "
        f"git -C \"$dir\" update-ref -d {temp_ref} >/dev/null 2>&1 || true' "
        "EXIT",
        'cat > "$bundle"',
        'git -C "$dir" fetch "$bundle" '
        "'+refs/heads/*:refs/git-relay/push/*' >&2",
        f'git -C "$dir" push -- "$url" {refspec} >&2',
    ]
    return "\n".join(lines)


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


def fetch_submodules(dest: Path, remote: Remote, options: CloneOptions,
                     verbose: bool, dry_run: bool) -> None:
    if shutil.which("git") is None:
        raise RelayError("local git not found; please install Git")
    repo = resolve_local_repo(dest, options)
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


def sync_mode(git: GitOptions, override: str = "") -> str:
    mode = (override or git.mode).strip().lower()
    if mode not in {"relay", "direct"}:
        raise RelayError(f"sync mode must be relay/direct: {mode}")
    return mode


def remote_name(git: GitOptions, override: str = "") -> str:
    name = (override or git.remote).strip() or "origin"
    return name


def remote_url(repo: Path, name: str) -> str:
    result = run_local(["git", "-C", str(repo), "remote", "get-url", name],
                       check=False)
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        raise RelayError(f"git remote '{name}' not found in {repo}")
    return url


def current_branch(repo: Path) -> str:
    result = run_local(["git", "-C", str(repo), "symbolic-ref", "--quiet",
                        "--short", "HEAD"], check=False)
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise RelayError("HEAD is detached; pass --branch to pick a branch")
    return branch


def validate_branch(branch: str) -> None:
    if not branch:
        raise RelayError("branch name must not be empty")
    run_local(["git", "check-ref-format", "--branch", branch])


def tracking_prereqs(repo: Path, name: str) -> list[str]:
    # SHAs already known locally keep the relayed bundle small. Refs that no
    # longer exist on the remote mirror are filtered out by the remote script.
    result = run_local(["git", "-C", str(repo), "for-each-ref",
                        "--format=%(objectname)",
                        f"refs/remotes/{name}", "refs/tags"], check=False)
    if result.returncode != 0:
        return []
    shas = []
    for line in result.stdout.splitlines():
        sha = line.strip()
        if sha and sha not in shas:
            shas.append(sha)
    return shas


def relay_fetch(repo: Path, remote: Remote, git: GitOptions, name: str,
                verbose: bool, dry_run: bool) -> bool:
    url = remote_url(repo, name)
    command = remote_fetch_command(url, git, tracking_prereqs(repo, name))
    if dry_run:
        print("Remote command:")
        print(command)
        print(f"Local repository: {repo}")
        return False
    validate_remote(remote)
    runner = make_runner(remote, verbose)
    try:
        log(f"fetching {name} via {remote.user}@{remote.host}:{remote.port}")
        with tempfile.TemporaryDirectory(prefix="git-relay-") as tmp:
            bundle = Path(tmp) / "fetch.bundle"
            rc = runner.download(command, bundle)
            if rc == 3:
                log(f"already up to date: {repo}")
                return False
            if rc != 0:
                raise RelayError(
                    f"remote fetch failed (exit {rc}); check the remote URL, "
                    f"network, and remote git")
            if bundle.stat().st_size == 0:
                log(f"already up to date: {repo}")
                return False
            log("applying fetched objects")
            run_local(["git", "-C", str(repo), "fetch", str(bundle),
                       f"+refs/heads/*:refs/remotes/{name}/*",
                       "+refs/tags/*:refs/tags/*"])
    finally:
        runner.close()
    return True


def fetch_remote(dest: Path, remote: Remote, options: CloneOptions,
                 git: GitOptions, *, branch: str = "", name: str = "",
                 mode: str = "", verbose: bool = False,
                 dry_run: bool = False) -> None:
    if shutil.which("git") is None:
        raise RelayError("local git not found; please install Git")
    repo = resolve_local_repo(dest, options)
    name = remote_name(git, name)
    mode = sync_mode(git, mode)
    if mode == "direct":
        cmd = ["git", "-C", str(repo), "fetch", name]
        if dry_run:
            print("Local command:")
            print(" ".join(shlex.quote(c) for c in cmd))
            return
        rc = run_local_stream(cmd)
        if rc != 0:
            raise RelayError(f"git fetch failed (exit {rc})")
        log(f"fetched {name} in {repo}")
        return
    relay_fetch(repo, remote, git, name, verbose, dry_run)
    log(f"fetched {name} in {repo}")


def upstream_ref(repo: Path, name: str, branch: str) -> str:
    result = run_local(["git", "-C", str(repo), "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{upstream}"], check=False)
    ref = result.stdout.strip()
    if result.returncode == 0 and ref.startswith(f"{name}/"):
        return ref
    candidate = f"refs/remotes/{name}/{branch}"
    if run_local(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                  candidate], check=False).returncode == 0:
        return candidate
    raise RelayError(f"no upstream found for branch {branch} on {name}; "
                     f"fetch it first or check git.remote")


def pull_remote(dest: Path, remote: Remote, options: CloneOptions,
                git: GitOptions, *, branch: str = "", name: str = "",
                mode: str = "", verbose: bool = False,
                dry_run: bool = False) -> None:
    if shutil.which("git") is None:
        raise RelayError("local git not found; please install Git")
    repo = resolve_local_repo(dest, options)
    name = remote_name(git, name)
    mode = sync_mode(git, mode)
    if mode == "direct":
        cmd = ["git", "-C", str(repo), "pull", "--ff-only"]
        if branch:
            cmd += [name, branch]
        if dry_run:
            print("Local command:")
            print(" ".join(shlex.quote(c) for c in cmd))
            return
        rc = run_local_stream(cmd)
        if rc != 0:
            raise RelayError(f"git pull failed (exit {rc})")
        log(f"pulled {repo}")
        return
    relay_fetch(repo, remote, git, name, verbose, dry_run)
    if dry_run:
        return
    branch = branch or current_branch(repo)
    target = upstream_ref(repo, name, branch)
    log(f"merging {target} into {branch}")
    result = run_local(["git", "-C", str(repo), "merge", "--ff-only", target],
                       check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RelayError(
            "fast-forward merge failed; the branch has diverged, merge or "
            f"rebase manually:\n{detail[-3000:]}")
    log(f"pulled {branch} from {name} in {repo}")


def push_remote(dest: Path, remote: Remote, options: CloneOptions,
                git: GitOptions, *, branch: str = "", name: str = "",
                mode: str = "", verbose: bool = False,
                dry_run: bool = False) -> None:
    if shutil.which("git") is None:
        raise RelayError("local git not found; please install Git")
    repo = resolve_local_repo(dest, options)
    name = remote_name(git, name)
    mode = sync_mode(git, mode)
    branch = branch or current_branch(repo)
    validate_branch(branch)
    if mode == "direct":
        cmd = ["git", "-C", str(repo), "push", name, branch]
        if dry_run:
            print("Local command:")
            print(" ".join(shlex.quote(c) for c in cmd))
            return
        rc = run_local_stream(cmd)
        if rc != 0:
            raise RelayError(f"git push failed (exit {rc})")
        log(f"pushed {branch} to {name} in {repo}")
        return
    url = remote_url(repo, name)
    base = f"refs/remotes/{name}/{branch}"
    has_base = run_local(["git", "-C", str(repo), "rev-parse", "--verify",
                          "--quiet", base],
                         check=False).returncode == 0
    with tempfile.TemporaryDirectory(prefix="git-relay-") as tmp:
        bundle = Path(tmp) / "push.bundle"
        create = ["git", "-C", str(repo), "bundle", "create", str(bundle),
                  branch]
        if has_base:
            create.append(f"^{base}")
        result = run_local(create, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "empty bundle" in detail.lower():
                log(f"nothing to push: {branch} is up to date")
                return
            raise RelayError(f"failed to create push bundle:\n"
                             f"{detail[-3000:]}")
        command = remote_push_command(url, git, branch)
        if dry_run:
            print("Remote command:")
            print(command)
            print(f"Local repository: {repo}")
            print(f"Bundle size: {format_size(bundle.stat().st_size)}")
            return
        validate_remote(remote)
        runner = make_runner(remote, verbose)
        try:
            log(f"pushing {branch} via {remote.user}@{remote.host}:"
                f"{remote.port}")
            rc = runner.upload(command, bundle)
            if rc != 0:
                raise RelayError(
                    f"remote push failed (exit {rc}); check push credentials "
                    f"and whether the branch is fast-forward")
        finally:
            runner.close()
    sha = run_local(["git", "-C", str(repo), "rev-parse", branch]).stdout.strip()
    if sha:
        run_local(["git", "-C", str(repo), "update-ref", base, sha],
                  check=False)
    log(f"pushed {branch} to {name} in {repo}")


def check(remote: Remote, verbose: bool) -> int:
    validate_remote(remote)
    ok = True
    log(f"python: {sys.version.split()[0]} (TOML: {toml_backend()})")
    local_git = shutil.which("git")
    if local_git is None:
        warn("git not found on this machine")
        ok = False
    else:
        result = run_local(["git", "--version"], check=False)
        log(f"local: {result.stdout.strip() or result.stderr.strip()}")
    if importlib.util.find_spec("tkinter") is None:
        log("tkinter: not installed (GUI unavailable; CLI works)")
    else:
        log("tkinter: available")
    backend = resolve_backend(remote)
    has_paramiko = importlib.util.find_spec("paramiko") is not None
    if has_paramiko:
        log("paramiko: available")
    elif backend == "paramiko":
        warn("paramiko is not installed; install python3-paramiko "
             "or use ssh_backend=openssh")
        ok = False
    else:
        log("paramiko: not installed (optional)")
    log(f"SSH backend: {backend} "
        f"({remote.user}@{remote.host}:{remote.port})")
    if backend == "openssh" and shutil.which("ssh") is None:
        warn("ssh not found on this machine; install an OpenSSH client")
        return 1
    if backend == "paramiko" and not has_paramiko:
        return 1
    runner = make_runner(remote, verbose)
    try:
        rc, out, err = runner.run_capture(
            "set -e; git --version; tar --version | head -n 1; "
            "command -v mktemp >/dev/null; command -v gzip >/dev/null; "
            "command -v du >/dev/null; command -v cut >/dev/null; "
            "command -v date >/dev/null; command -v cat >/dev/null; "
            "command -v head >/dev/null")
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
