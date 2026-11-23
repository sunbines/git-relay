#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./install.sh [--prefix DIR] [--force] [--uninstall]

Install the git-relay command into PATH. Root installs to /usr/local/bin by
default; other users install to ~/.local/bin.

Options:
  --prefix DIR   install to DIR/bin
  --force        overwrite files not created by this installer
  --uninstall    remove the installed command
  -h, --help     show this help
EOF
}

prefix=
force=0
uninstall=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            [ $# -ge 2 ] || { echo "[install] --prefix requires an argument" >&2; exit 2; }
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
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[install] unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

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
        echo "[install] no installed command at $target"
        exit 0
    fi
    if ! is_our_link && [ "$force" -ne 1 ]; then
        echo "[install] $target was not installed by this repository; use --force to remove it anyway" >&2
        exit 1
    fi
    rm -f "$target"
    echo "[install] removed $target"
    exit 0
fi

[ -f "$root/src/git_relay.py" ] || {
    echo "[install] missing $root/src/git_relay.py" >&2
    exit 1
}
[ -f "$source" ] || {
    echo "[install] missing $source" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    echo "[install] python3 not found" >&2
    exit 1
}
chmod +x "$source"

if { [ -e "$target" ] || [ -L "$target" ]; } && ! is_our_link; then
    if [ "$force" -ne 1 ]; then
        echo "[install] $target already exists; use --force to overwrite" >&2
        exit 1
    fi
fi

rm -f "$target"
ln -s "$source" "$target"
echo "[install] installed: $target -> $source"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        echo "[install] note: $bin_dir is not in PATH; add it to your shell profile, for example:"
        echo "  export PATH=\"$bin_dir:\$PATH\""
        ;;
esac

echo "[install] verify with: git-relay --version"
