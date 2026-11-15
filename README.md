# git-relay

通过一台外网 Linux 主机执行 `git clone`，再把仓库打包流式回传到本地，解决本机直接 `git clone` 很慢的问题。

同一份 Python 代码可在 **Windows 11** 和 **Linux** 上运行：

- **Windows 11**：双击 exe 打开图形界面，填写服务器、端口、用户和密码。
- **Linux**：在 `git-relay.toml` 或环境变量中配置服务器信息，使用命令行。
- 服务器地址、用户和密码都没有写死在代码里。

## 工作原理

```text
本地机器                        远端 Linux 主机
   |  ssh 执行 clone 命令  ----->  git clone --no-checkout
   |                               tar.gz 打包 .git
   |  <----- stdout 流式传输 -----
   |  解压 + git checkout
```

- 远端负责访问 GitHub/GitLab，避免本地网络慢。
- 只回传 `.git` 目录（默认模式），源文件在本地 checkout，传输量更小。
- 加 `--recurse-submodules` 时，远端会连同子模块一起 clone，再打包整个仓库回传。
- 不会修改全局 git 配置，也不会在本地启动代理。

## 依赖

- Python 3.11+（使用标准库 `tomllib`）
- Git（本地和远端）
- SSH 客户端
  - Linux：通常已安装 OpenSSH
  - Windows 11：系统自带 OpenSSH Client，另需安装 Git for Windows
- 密码登录建议安装 Paramiko：

```bash
python -m pip install paramiko
```

没有 Paramiko 时会退回 OpenSSH + `SSH_ASKPASS`；使用 SSH 密钥时两种后端都可以。Windows 自带 OpenSSH 对 `SSH_ASKPASS` 的支持并不稳定，**密码登录请优先使用 Paramiko，或直接使用打包好的 exe（已内置 Paramiko）**。

Linux 图形界面需要 `tkinter`（部分发行版包名为 `python3-tk`），命令行不需要。

## Windows 11 图形界面

用 Python 直接启动：

```bat
tools\git-relay\git-relay.cmd
```

或：

```powershell
python tools\git-relay\git_relay.py gui
```

界面中填写：

- 服务器、端口、用户、密码（或私钥）
- 仓库 URL、本地目录
- 浅克隆深度、分支、是否拉取子模块

然后点击 **测试连接** 或 **开始克隆**。日志会实时显示在窗口中。

**保存设置（不含密码）** 会把服务器、用户、端口、私钥和克隆选项写入 exe/脚本旁边的 `git-relay.toml`；密码不会落盘，下次启动需要重新输入，或使用 `GIT_RELAY_PASSWORD` 环境变量。

打包后有两个 exe：

- `git-relay.exe`：图形界面版本，双击直接打开窗口，不会弹出控制台或 PowerShell。
- `git-relay-cli.exe`：命令行版本，输出显示在当前终端中。

`git-relay.cmd` 会自动选择：不带参数时打开图形界面，带参数时调用命令行版本。

## Linux 配置文件

```bash
cd tools/git-relay
cp git-relay.toml.example git-relay.toml
```

编辑 `git-relay.toml`：

```toml
[remote]
host = "your-server.example.com"
user = "your-user"
port = 22
# password = "your-password"
# key = "/home/you/.ssh/id_ed25519"
# ssh_backend = "auto"
```

推荐不要把密码写进文件，改用环境变量：

```bash
export GIT_RELAY_PASSWORD='你的密码'
```

`git-relay.toml` 已被 `.gitignore` 忽略，不会提交到仓库。

Linux 下也可以使用图形界面（需要图形环境和 tkinter）：

```bash
./tools/git-relay/git-relay gui
```

## 命令行用法

先检查本机和远端环境：

```bash
./tools/git-relay/git-relay check
```

完整克隆：

```bash
./tools/git-relay/git-relay clone https://github.com/ceph/ceph.git
```

指定目录和浅克隆（推荐，速度最快）：

```bash
./tools/git-relay/git-relay clone --depth 1 \
  https://github.com/ceph/ceph.git ceph
```

克隆并包含所有子模块：

```bash
./tools/git-relay/git-relay clone --depth 1 --recurse-submodules \
  https://github.com/ceph/ceph.git ceph
```

只克隆某个分支：

```bash
./tools/git-relay/git-relay clone --branch main \
  https://github.com/example/project.git
```

只查看远端命令，不执行：

```bash
./tools/git-relay/git-relay clone --dry-run \
  https://github.com/example/project.git
```

Windows 命令行：

```bat
tools\git-relay\git-relay.cmd check
tools\git-relay\git-relay.cmd clone --depth 1 https://github.com/ceph/ceph.git ceph
```

也可以直接调用 `python tools\git-relay\git_relay.py ...`。

## 打包成 Windows 11 exe

PyInstaller 不能跨平台打包，请在 **Windows 11** 上执行。构建脚本会创建独立的 `.build-venv`，不会污染系统 Python。

PowerShell：

```powershell
cd tools\git-relay
.\build-exe.ps1
```

cmd 中也可以双击或执行：

```bat
build-exe.cmd
```

默认会把 Paramiko 一起打进去，密码登录无需另外安装 SSH 客户端。只用 SSH 密钥时可以减小体积：

```powershell
.\build-exe.ps1 -NoParamiko
```

产物：

```text
tools\git-relay\dist\
  git-relay.exe          图形界面（无控制台）
  git-relay-cli.exe      命令行版本
  git-relay.cmd          自动选择 GUI/CLI 的入口
  git-relay.toml.example
  README.md
```

使用：

```powershell
# 双击或直接运行，打开图形界面，不会弹出控制台
.\dist\git-relay.exe

# 命令行使用 CLI 版本
.\dist\git-relay-cli.exe check
.\dist\git-relay-cli.exe clone --depth 1 https://github.com/ceph/ceph.git ceph
```

注意：

- 图形界面保存的 `git-relay.toml` 在 exe 旁边，且不包含密码。
- 本机仍需安装 Git for Windows，用于本地 `git checkout`。
- 单文件 exe 首次启动会解压到临时目录，启动稍慢属正常现象。
- 图形界面启动失败时会弹出提示，并把详细错误写入 exe 旁边的 `git-relay-error.log`。
- 未签名的 PyInstaller exe 可能触发 Windows Defender 误报，发布时建议做代码签名。

## 配置优先级

命令行模式：

```text
命令行参数 > 环境变量 > git-relay.toml > 内置默认值（仅端口和后端等）
```

图形界面以输入框内容为准；启动时会用 `git-relay.toml` 和环境变量预填。

常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `GIT_RELAY_HOST` | 远端主机 |
| `GIT_RELAY_USER` | SSH 用户 |
| `GIT_RELAY_PORT` | SSH 端口 |
| `GIT_RELAY_PASSWORD` | SSH 密码 |
| `GIT_RELAY_KEY` | SSH 私钥路径 |
| `GIT_RELAY_SSH_BACKEND` | `auto` / `openssh` / `paramiko` |
| `GIT_RELAY_DEPTH` | 默认浅克隆深度 |

## 退出码

- `0`：成功
- `1`：配置、SSH、clone、解压或 checkout 失败
- `2`：Linux 下未提供子命令（会打印帮助）
- `130`：用户按 Ctrl+C 中断

## 故障排查

**提示“未配置 SSH 服务器地址”**

Windows 请打开图形界面填写服务器和用户；Linux 请检查 `git-relay.toml`，或设置：

```bash
export GIT_RELAY_HOST='your-server.example.com'
export GIT_RELAY_USER='your-user'
export GIT_RELAY_PASSWORD='你的密码'
```

**SSH 连接失败 / Permission denied**

```bash
python tools/git-relay/git_relay.py check
```

确认服务器地址、用户、端口、密码或私钥正确。Windows 密码登录建议安装 Paramiko，或使用已内置 Paramiko 的 exe。

**远端 git clone 失败**

工具会把远端 `git clone` 的 stderr 原样打印出来。先手动在远端执行一次：

```bash
ssh your-user@your-server.example.com \
  'git clone --depth 1 https://github.com/example/project.git /tmp/test'
```

确认远端能访问目标仓库。

**Windows 解压时报符号链接错误**

启用 Windows 开发者模式，或用管理员终端运行。也可以先不加“同时拉取子模块”，避免回传包含大量符号链接的工作区。

**目标目录已存在**

工具要求目标目录不存在或为空，避免覆盖已有文件。

## 限制

- 目前是“克隆加速器”，不是 Git remote helper；已有仓库的增量 `fetch` 仍需手动处理。
- 每次执行会重新在远端 clone；后续可以扩展为远端缓存仓库 + 增量 bundle。
- Git LFS 仓库只回传指针文件；本地 `git checkout` 时仍会直连 GitHub 拉取 LFS 对象，这部分不会被加速。
- 密码只保存在进程内存或环境变量中；图形界面默认不保存密码。
