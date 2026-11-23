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

然后点击 **测试连接** 或 **开始克隆**，日志会实时显示在窗口中（进度格式见下文“进度输出”）。

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

[clone]
download_dir = "/root"   # 默认下载目录
```

`download_dir` 是默认下载目录：命令行未指定 `DEST` 时，仓库会克隆到 `<download_dir>/<仓库名>`；如果指定了 `DEST`，则 `DEST` 优先。相对路径按当前工作目录解析。未配置时默认为 `/root`。

推荐不要把密码写进文件，改用环境变量：

```bash
export GIT_RELAY_PASSWORD='你的密码'
```

`conf/git-relay.toml` 已被 `.gitignore` 忽略，不会提交到仓库。

Linux 下也可以使用图形界面（需要图形环境和 `tkinter`）：

```bash
./script/git-relay gui
```

## 安装 git-relay 命令

在仓库根目录执行：

```bash
./setup-git-relay.sh
git-relay --version
```

脚本会把 [`script/git-relay`](../script/git-relay) 链接到 PATH：

- root 用户默认安装到 `/usr/local/bin`。
- 普通用户默认安装到 `~/.local/bin`。
- 用 `--prefix DIR` 可以安装到 `DIR/bin`。
- 用 `--uninstall` 卸载（不会删除 `conf/git-relay.toml`）；配合 `--build` 同时删除 `.build-venv`、`build`、`dist` 和 `__pycache__`。
- 用 `--force` 覆盖同名文件，或删除不是本仓库安装的同名文件。

如果 `~/.local/bin` 不在 `PATH`，脚本会打印需要添加的 `export PATH=...`。安装的是指向本仓库的符号链接，仓库移动后需要重新执行安装脚本；不安装时仍可使用 `./script/git-relay`。配置默认读取仓库中的 `conf/git-relay.toml`，与当前工作目录无关。

Windows 使用打包后的 `git-relay.cmd` 或 exe，不需要这个脚本。

## 生成配置文件

```bash
git-relay config > /tmp/git-relay.toml
```

命令把当前生效的配置输出为 TOML（配置文件 + 环境变量 + 默认值），可直接作为配置文件使用。密码不会写入输出，请使用 `GIT_RELAY_PASSWORD` 环境变量；没有配置文件时，`host` 和 `user` 使用占位符。

如果目标配置文件已存在，先输出到临时文件再替换，避免 shell 重定向提前截断正在读取的配置：

```bash
git-relay config > /tmp/git-relay.toml
# 检查后替换
mv /tmp/git-relay.toml conf/git-relay.toml
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

子模块在远端一并下载并检出，本地不会再访问 GitHub。

如果最初没有加 `--recurse-submodules`，可以在已有仓库中单独补齐子模块：

```bash
./script/git-relay submodules ceph
```

工具会读取仓库的 `origin` URL 和当前 `HEAD`，在远端按同一提交拉取子模块，只回传子模块工作区和 `.git/modules`，最后在本地离线检出。相对名称在当前目录不存在时，会按配置中的 `download_dir` 查找，所以配置了 `download_dir = "/home"` 时可以直接写 `submodules ceph`。要求该提交已存在于远端；如果本地有新提交，请先 push。若子模块已全部初始化，命令会直接提示后退出；只有部分初始化时会提示先执行本地 `git submodule update --init --recursive --no-fetch`。

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

## 同步已有仓库（fetch / pull / push）

对本地已有仓库执行网络操作，默认经中转主机访问外网：

```bash
./script/git-relay fetch ceph            # 只更新远端跟踪分支和标签
./script/git-relay pull ceph             # fetch 后对当前分支做 fast-forward
./script/git-relay pull ceph --branch main
./script/git-relay push ceph             # 推送当前分支
./script/git-relay push ceph --branch main --remote origin
```

- `--mode relay`：远端主机维护裸仓库镜像（默认缓存 `~/.cache/git-relay`），fetch 时把新对象打包回传；push 时把本机对象上传给远端，再由远端推送到 origin。
- `--mode direct`：直接用本机 `git` 执行，中转只用于 clone。
- 不传 `--mode` 时使用配置 `[git].mode`（默认 `relay`），`GIT_RELAY_GIT_MODE` 可覆盖。
- `pull` 只做 fast-forward，分支已分叉时会报错，请手动 merge/rebase。
- relay 模式 push 要求远端主机对该仓库具备推送凭据（SSH key、HTTPS token 等）；凭据错误会原样打印远端 git 的输出。

配置文件中的默认值：

```toml
[git]
mode = "relay"            # relay / direct
remote = "origin"         # 本地 git 远程名
# cache_dir = ""          # 远端镜像缓存目录（默认 ~/.cache/git-relay）
```

Windows 命令行：

```bat
script\git-relay.cmd check
script\git-relay.cmd clone --depth 1 https://github.com/ceph/ceph.git ceph
script\git-relay.cmd submodules ceph
```

打包后也可以直接使用命令行版本：

```bat
dist\git-relay-cli.exe check
dist\git-relay-cli.exe clone --depth 1 https://github.com/ceph/ceph.git ceph
dist\git-relay-cli.exe submodules ceph
```

直接调用 Python 源码：

```bash
python3 src/git_relay.py check
```

## 进度输出

命令行和 GUI 使用相同的进度统计，示例：

```text
[git-relay] clone: Receiving objects 45% (1234/2741) 12.3 MiB | 5.6 MiB/s
[git-relay] transfer 18.4 MiB (9.2 MiB/s)
[git-relay] transfer complete: 45.2 MiB in 8.3s (5.45 MiB/s)
[git-relay] remote repository size: 117.7 MiB (cloned in 12s, 9.81 MiB/s)
[git-relay] local repository size: 210.4 MiB
```

- 远端 clone 阶段显示对象接收进度、已接收大小和远端下载速度。
- 回传阶段显示已传输大小和实时速度，结束时输出总量、耗时和平均速度。
- 完成后输出远端仓库大小和本地项目大小。
- 刷新频率由令牌桶算法控制（默认每秒 4 次，容量 2），终端中原地刷新一行，GUI 和重定向输出中逐行追加，避免刷屏。
- 小仓库可能在 1 秒内完成，此时只会看到最终统计结果。

## 配置优先级

命令行模式：

```text
命令行参数 > 环境变量 > TOML 配置文件 > 内置默认值（端口、后端、下载目录等）
```

图形界面以输入框内容为准；启动时用配置文件和环境变量预填。本地目录留空时，使用配置中的 `download_dir`；未配置则默认为 `/root`。

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
| `GIT_RELAY_BRANCH` | 默认克隆分支 |
| `GIT_RELAY_JOBS` | 子模块并发数 |
| `GIT_RELAY_REMOTE_GIT` | 远端 `git` 可执行文件 |
| `GIT_RELAY_DOWNLOAD_DIR` | 默认下载目录 |
| `GIT_RELAY_GIT_MODE` | 同步方式 `relay` / `direct` |
| `GIT_RELAY_GIT_REMOTE` | 本地 git 远程名 |
| `GIT_RELAY_GIT_CACHE_DIR` | 远端镜像缓存目录 |

## 退出码

- `0`：成功
- `1`：配置、SSH、clone、解压或 checkout 失败
- `2`：Linux 下未提供子命令（会打印帮助）
- `130`：用户按 Ctrl+C 中断
