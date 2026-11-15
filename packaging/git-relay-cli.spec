# -*- mode: python ; coding: utf-8 -*-
import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

hiddenimports = ["git_relay_gui"]
datas = []

# paramiko 是运行时按需导入的，PyInstaller 静态分析发现不了，需要显式收集。
if importlib.util.find_spec("paramiko") is not None:
    hiddenimports += collect_submodules("paramiko")
    datas += collect_data_files("paramiko")

a = Analysis(
    [str(SRC / "git_relay.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="git-relay-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
)
