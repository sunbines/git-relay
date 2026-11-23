[CmdletBinding()]
param(
    [switch]$NoParamiko
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

if (Get-Command py -ErrorAction SilentlyContinue) {
    $launcher = "py"
    $launcherArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $launcher = "python"
    $launcherArgs = @()
} else {
    throw "未找到 Python，请先安装 Python 3.11 或更高版本"
}

$venv = Join-Path $root ".build-venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[build] 创建构建虚拟环境：$venv"
    & $launcher @launcherArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败"
    }
}

& $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "需要 Python 3.11 或更高版本"
}

Write-Host "[build] 安装 PyInstaller"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "安装 PyInstaller 失败"
}

if (-not $NoParamiko) {
    Write-Host "[build] 安装 Paramiko（密码登录需要）"
    & $venvPython -m pip install paramiko
    if ($LASTEXITCODE -ne 0) {
        throw "安装 Paramiko 失败"
    }
}

$dist = Join-Path $root "dist"
$work = Join-Path $root "build"
$pyiCommon = @("--noconfirm", "--distpath", $dist, "--workpath", $work)

Write-Host "[build] 打包图形界面（无控制台）"
& $venvPython -m PyInstaller @pyiCommon --clean `
    (Join-Path $root "packaging\git-relay-gui.spec")
if ($LASTEXITCODE -ne 0) {
    throw "打包图形界面失败"
}

Write-Host "[build] 打包命令行版本"
& $venvPython -m PyInstaller @pyiCommon `
    (Join-Path $root "packaging\git-relay-cli.spec")
if ($LASTEXITCODE -ne 0) {
    throw "打包命令行版本失败"
}

Copy-Item -Force (Join-Path $root "conf\git-relay.toml.example") `
    (Join-Path $dist "git-relay.toml.example")
Copy-Item -Force (Join-Path $root "README.md") (Join-Path $dist "README.md")
Copy-Item -Force (Join-Path $scriptDir "git-relay.cmd") `
    (Join-Path $dist "git-relay.cmd")
Copy-Item -Force (Join-Path $scriptDir "gr.cmd") `
    (Join-Path $dist "gr.cmd")

Write-Host ""
Write-Host "[build] 完成："
Write-Host "  $dist\git-relay.exe      （双击打开图形界面，不弹控制台）"
Write-Host "  $dist\git-relay-cli.exe  （命令行版本）"
Write-Host "  $dist\git-relay.cmd / gr.cmd  （命令分发，短别名 gr）"
Write-Host "  $dist\git-relay.toml.example"
