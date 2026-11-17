# git-relay

通过一台外网 Linux 主机执行 `git clone`，再把仓库流式回传到本地，解决本机直接 `git clone` 很慢的问题。

同一份 Python 代码可在 **Windows 11** 和 **Linux** 上运行：

- **Windows 11**：双击 GUI 版本，填写服务器、端口、用户和密码。
- **Linux**：在 `conf/git-relay.toml` 或环境变量中配置服务器信息，使用命令行。
- 服务器地址、用户和密码不写死在代码里。

## 特性

- 远端负责访问 GitHub/GitLab，本地只接收 `.git` 数据。
- 默认只回传 `.git` 目录，源文件在本地 `git checkout`，传输量更小。
- 支持浅克隆、指定分支、子模块。
- 支持在克隆后单独通过远端补齐子模块。
- 支持在配置文件中设置默认下载目录。
- 实时显示远端克隆进度、下载速度、回传速度和最终仓库大小（令牌桶限流刷新）。
- 支持 Paramiko 和 OpenSSH 两种 SSH 后端。
- 不修改全局 Git 配置，也不在本地启动代理。

## 目录结构

```text
git-relay/
├── src/                 Python 源码
├── docs/                详细文档
├── script/              启动和打包脚本
├── conf/                配置模板
├── packaging/           PyInstaller 打包配置
├── README.md
└── .gitignore
```

## 快速开始

### Windows 11

构建后双击 `dist\git-relay.exe`，只打开图形界面，不会弹出 PowerShell 或控制台。

命令行使用 `dist\git-relay-cli.exe`，或使用自动分发的 `dist\git-relay.cmd`：

```bat
dist\git-relay.exe
dist\git-relay-cli.exe check
dist\git-relay.cmd clone --depth 1 https://github.com/ceph/ceph.git ceph
```

从源码启动 GUI：

```powershell
python src\git_relay.py gui
```

打包方法见 [docs/build.md](docs/build.md)。

### Linux

```bash
cp conf/git-relay.toml.example conf/git-relay.toml
# 编辑 conf/git-relay.toml，填入服务器和用户
export GIT_RELAY_PASSWORD='你的密码'

./script/git-relay check
./script/git-relay clone --depth 1 https://github.com/ceph/ceph.git ceph
```

完整用法见 [docs/usage.md](docs/usage.md)。

## 依赖

- Python 3.11+（使用标准库 `tomllib`）
- Git（本地和远端）
- SSH 客户端
- 密码登录建议安装 Paramiko：`python -m pip install paramiko`
- Linux 图形界面需要 `tkinter`，命令行不需要

## 文档

- [使用说明](docs/usage.md)：Windows GUI、Linux 配置、命令行、环境变量
- [工作原理](docs/architecture.md)：传输流程、模式和限制
- [构建与打包](docs/build.md)：Windows exe 构建、产物说明
- [故障排查](docs/troubleshooting.md)：常见错误和处理方法
