# 构建与打包

## 依赖

构建 Windows exe 需要：

- Windows 11
- Python 3.11+
- PowerShell
- 网络（首次安装 PyInstaller 和 Paramiko）

PyInstaller 不能跨平台打包，请在 Windows 上执行。构建脚本会在仓库根目录创建独立的 `.build-venv`，不会污染系统 Python。

## 构建

在仓库根目录执行：

```powershell
.\script\build-exe.ps1
```

也可以在 `script` 目录中双击或执行：

```bat
build-exe.cmd
```

默认会把 Paramiko 一起打包，密码登录无需另外安装 SSH 客户端。只用 SSH 密钥时可以减小体积：

```powershell
.\script\build-exe.ps1 -NoParamiko
```

构建脚本会自动定位仓库根目录，因此从其他目录调用也可以。

## 产物

```text
dist\
  git-relay.exe          图形界面（无控制台）
  git-relay-cli.exe      命令行版本
  git-relay.cmd          自动选择 GUI/CLI 的入口
  gr.cmd                 同上，短别名
  git-relay.toml.example
  README.md
```

`git-relay.exe` 使用 GUI 子系统构建，双击不会弹出 PowerShell 或控制台。命令行请使用 `git-relay-cli.exe`。

## 打包配置

PyInstaller 配置放在 `packaging/`：

- `packaging/git-relay-gui.spec`：GUI 版本，`console=False`
- `packaging/git-relay-cli.spec`：命令行版本，`console=True`

两个 spec 都通过 `SPECPATH` 定位仓库根目录，不依赖当前工作目录。

## 注意

- 图形界面保存的 `git-relay.toml` 在 exe 旁边，且不包含密码。
- 本机仍需安装 Git for Windows，用于本地 `git checkout`。
- 单文件 exe 首次启动会解压到临时目录，启动稍慢属正常现象。
- 未签名的 PyInstaller exe 可能触发 Windows Defender 误报，发布时建议做代码签名。
- `dist/`、`build/` 和 `.build-venv/` 已被 `.gitignore` 忽略，不提交到仓库。
