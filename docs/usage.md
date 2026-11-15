# 使用说明

## Windows 11 图形界面

从源码启动：

```bat
script\git-relay.cmd
```

或：

```powershell
python src\git_relay.py gui
```

打包后直接双击 `dist\git-relay.exe`。这是无控制台的 GUI 版本，不会弹出 PowerShell 窗口。

界面中填写：

- 服务器、端口、用户、密码（或私钥）
- 仓库 URL、本地目录
- 浅克隆深度、分支、是否拉取子模块

然后点击 **测试连接** 或 **开始克隆**，日志会实时显示在窗口中。

**保存设置（不含密码）** 会把服务器、用户、端口、私钥和克隆选项写入当前配置文件：

- 源码运行时：`conf/git-relay.toml`
- exe 运行时：exe 同目录的 `git-relay.toml`

密码不会落盘。下次启动需要重新输入，或设置 `GIT_RELAY_PASSWORD` 环境变量。

启动失败时会弹出提示，详细堆栈写入配置文件同目录的 `git-relay-error.log`。

## Linux 配置文件

在仓库根目录执行：

```bash
cp conf/git-relay.toml.example conf/git-relay.toml
```

编辑 `conf/git-relay.toml`：

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

`conf/git-relay.toml` 已被 `.gitignore` 忽略，不会提交到仓库。

Linux 下也可以使用图形界面（需要图形环境和 `tkinter`）：

```bash
./script/git-relay gui
```

## 命令行用法

先检查本机和远端环境：

```bash
./script/git-relay check
```

完整克隆：

```bash
./script/git-relay clone https://github.com/ceph/ceph.git
```

指定目录和浅克隆：

```bash
./script/git-relay clone --depth 1 \
  https://github.com/ceph/ceph.git ceph
```

克隆并包含所有子模块：

```bash
./script/git-relay clone --depth 1 --recurse-submodules \
  https://github.com/ceph/ceph.git ceph
```

只克隆某个分支：

```bash
./script/git-relay clone --branch main \
  https://github.com/example/project.git
```

只查看远端命令，不执行：

```bash
./script/git-relay clone --dry-run \
  https://github.com/example/project.git
```

Windows 命令行：

```bat
script\git-relay.cmd check
script\git-relay.cmd clone --depth 1 https://github.com/ceph/ceph.git ceph
```

打包后也可以直接使用命令行版本：

```bat
dist\git-relay-cli.exe check
dist\git-relay-cli.exe clone --depth 1 https://github.com/ceph/ceph.git ceph
```

直接调用 Python 源码：

```bash
python3 src/git_relay.py check
```

## 配置优先级

命令行模式：

```text
命令行参数 > 环境变量 > TOML 配置文件 > 内置默认值（仅端口和后端等）
```

图形界面以输入框内容为准；启动时用配置文件和环境变量预填。

默认配置文件位置：

- 源码运行：`conf/git-relay.toml`
- exe 运行：exe 同目录 `git-relay.toml`

可以用 `-c/--config` 指定其他路径。

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
