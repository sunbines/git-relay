#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./setup-git-relay.sh [--prefix DIR] [--force] [--uninstall [--build]]

Install the git-relay command into PATH. Root installs to /usr/local/bin by
default; other users install to ~/.local/bin. The configuration file is not
removed on uninstall.

Options:
  --prefix DIR   install to DIR/bin
  --force        overwrite or remove files not created by this installer
  --uninstall    remove the installed command
  --build        with --uninstall, also remove .build-venv, build, dist,
                 and Python caches
  -h, --help     show this help
EOF
}

prefix=
force=0
uninstall=0
build=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            [ $# -ge 2 ] || { echo "[setup] --prefix requires an argument" >&2; exit 2; }
            prefix=$2
            shift 2
            ;;
        --prefix=*)
            prefix=${1#*=}
            shift
            ;;
        --force)
            force=1
            shift
            ;;
        --uninstall)
            uninstall=1
            shift
            ;;
        --build)
            build=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[setup] unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$build" -eq 1 ] && [ "$uninstall" -ne 1 ]; then
    echo "[setup] --build requires --uninstall" >&2
    exit 2
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$here
source=$root/script/git-relay

if [ -z "$prefix" ]; then
    if [ "$(id -u)" -eq 0 ] && [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
        prefix=/usr/local
    else
        prefix=$HOME/.local
    fi
fi

mkdir -p "$prefix/bin"
bin_dir=$(CDPATH= cd -- "$prefix/bin" && pwd)
target=$bin_dir/git-relay

is_our_link() {
    [ -L "$target" ] && [ "$(readlink "$target")" = "$source" ]
}

if [ "$uninstall" -eq 1 ]; then
    if [ ! -e "$target" ] && [ ! -L "$target" ]; then
        echo "[setup] no installed command at $target"
    else
        if ! is_our_link && [ "$force" -ne 1 ]; then
            echo "[setup] $target was not installed by this repository; use --force to remove it anyway" >&2
            exit 1
        fi
        rm -f "$target"
        echo "[setup] removed $target"
    fi
    if [ "$build" -eq 1 ]; then
        rm -rf "$root/.build-venv" "$root/build" "$root/dist"
        find "$root/src" -type d -name __pycache__ -prune -exec rm -rf {} +
        echo "[setup] removed build artifacts and Python caches"
    fi
    exit 0
fi

[ -f "$root/src/git_relay.py" ] || {
    echo "[setup] missing $root/src/git_relay.py" >&2
    exit 1
}
[ -f "$source" ] || {
    echo "[setup] missing $source" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    echo "[setup] python3 not found" >&2
    exit 1
}
chmod +x "$source"

if { [ -e "$target" ] || [ -L "$target" ]; } && ! is_our_link; then
    if [ "$force" -ne 1 ]; then
        echo "[setup] $target already exists; use --force to overwrite" >&2
        exit 1
    fi
fi

rm -f "$target"
ln -s "$source" "$target"
echo "[setup] installed: $target -> $source"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        echo "[setup] note: $bin_dir is not in PATH; add it to your shell profile, for example:"
        echo "  export PATH=\"$bin_dir:\$PATH\""
        ;;
esac

echo "[setup] verify with: git-relay --version"
echo "[setup] uninstall with: $root/setup-git-relay.sh --uninstall"
