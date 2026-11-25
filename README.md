# git-relay

通过一台外网 Linux 主机执行 `git clone`，再把仓库流式回传到本地，解决本机直接 `git clone` 很慢的问题。

同一份 Python 代码可在 **Windows 11** 和 **Linux** 上运行：

- **Windows 11**：双击 GUI 版本（Git Bash 终端风格窗口），在底部提示符后输入命令——内置的 `clone` / `check` 之外，`ls`、`grep`、`git`、`ssh` 等 bash 命令直接可用；参数在右键菜单 → Options 中设置。
- **Linux**：在 `/etc/git-relay/git-relay.toml` 或环境变量中配置服务器信息，使用命令行。
- 服务器地址、用户和密码不写死在代码里。

## 特性

- 远端负责访问 GitHub/GitLab，本地只接收 `.git` 数据。
- 默认只回传 `.git` 目录，源文件在本地 `git checkout`，传输量更小。
- 支持浅克隆、指定分支、子模块。
- 支持在克隆后单独通过远端补齐子模块。
- 支持在配置文件中设置默认下载目录。
- 支持对已有仓库执行 `fetch` / `pull` / `push`，可经中转（增量 bundle）或在本机直接执行。
- 实时显示远端克隆进度、下载速度、回传速度和最终仓库大小（令牌桶限流刷新）。
- 支持 Paramiko 和 OpenSSH 两种 SSH 后端。
- 不修改全局 Git 配置，也不在本地启动代理。

## 目录结构

```text
git-relay/
├── src/                 Python 源码
├── docs/                详细文档
├── script/              启动和打包脚本
├── setup-git-relay.sh   安装和卸载命令
├── conf/                配置模板
├── packaging/           PyInstaller 打包配置
├── README.md
└── .gitignore
```

## 快速开始

### Windows 11

构建后双击 `dist\git-relay.exe`，只打开界面，不会弹出 PowerShell 或控制台。主窗口是一扇 Git Bash（mintty）风格的终端：黑底、Consolas 等宽字体、带颜色标记的输出，底部提示符后可直接输入命令（`help` 查看内置命令，上下键翻历史），右键唤出系统原生浅色菜单，服务器与克隆参数在 **Options**（标准 Windows 窗口）中设置。除内置命令外，其它输入直接交给本机 Git Bash 执行，`ls` / `cat` / `grep` / `git` / `ssh` 等命令、管道与重定向都能用。Linux 端仍使用原有命令行，未做改动。

命令行使用 `dist\git-relay-cli.exe`，或使用自动分发的 `dist\git-relay.cmd`：

```bat
dist\git-relay.exe
dist\git-relay-cli.exe check
dist\git-relay.cmd clone --depth 1 https://github.com/ceph/ceph.git ceph
```

配置文件默认位于 `/etc/git-relay/git-relay.toml`（Windows 下为当前盘符的 `\etc\git-relay\`）；可用 `-c/--config` 指定其他路径。

从源码启动 GUI：

```powershell
python src\git_relay.py gui
```

打包方法见 [docs/build.md](docs/build.md)。

### Linux

```bash
./setup-git-relay.sh
sudoedit /etc/git-relay/git-relay.toml
# 填入服务器和用户
export GIT_RELAY_PASSWORD='你的密码'

git-relay check
git-relay clone --depth 1 https://github.com/ceph/ceph.git ceph
```

安装脚本会把配置模板复制到 `/etc/git-relay/git-relay.toml`（已存在则不覆盖）；普通用户安装时，若无写权限会尝试用 `sudo` 创建目录并复制模板。

`git-relay config` 输出当前生效的配置（配置文件 + 环境变量 + 默认值），可直接作为 TOML 文件使用；密码不会写入输出，请使用 `GIT_RELAY_PASSWORD`。先输出到临时文件再替换，避免 shell 重定向提前截断正在读取的配置：

```bash
git-relay config > /tmp/git-relay.toml
sudo mv /tmp/git-relay.toml /etc/git-relay/git-relay.toml
```

`setup-git-relay.sh` 会把命令链接到 PATH：root 用户默认 `/usr/local/bin`，普通用户默认 `~/.local/bin`；可用 `--prefix DIR` 指定其他位置。运行 `./setup-git-relay.sh --uninstall` 卸载（不会删除 `/etc/git-relay/git-relay.toml`），加 `--build` 同时清理 `.build-venv`、`build`、`dist` 和 Python 缓存。不安装时也可以继续用 `./script/git-relay`。

完整用法见 [docs/usage.md](docs/usage.md)。

## 依赖

- Python 3.9+（3.11+ 使用标准库 `tomllib`；3.9/3.10 使用内置 TOML 解析，安装 `tomli` 可获得完整支持）
- Git（本地和远端）
- SSH 客户端
- 密码登录建议安装 Paramiko：openEuler 可执行 `dnf install python3-paramiko`，也可使用 `python3 -m pip install paramiko`
- Linux 图形界面需要 `tkinter`，命令行不需要；openEuler 可安装 `python3-devel`，Debian/Ubuntu 为 `python3-tk`

配置好远端后，运行 `git-relay check`（或 `./script/git-relay check`）可检查本地 Python 模块、`git`/`ssh` 以及远端所需命令是否齐全。

## 文档

- [使用说明](docs/usage.md)：Windows GUI、Linux 配置、命令行、环境变量
- [工作原理](docs/architecture.md)：传输流程、模式和限制
- [构建与打包](docs/build.md)：Windows exe 构建、产物说明
- [故障排查](docs/troubleshooting.md)：常见错误和处理方法
