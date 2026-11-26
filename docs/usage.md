# 使用说明

## Windows 11 界面（Git Bash 终端风格）

从源码启动：

```bat
script\git-relay.cmd
```

或：

```powershell
python src\git_relay.py gui
```

打包后直接双击 `dist\git-relay.exe`。这是无控制台的 GUI 版本，不会弹出 PowerShell 窗口，主窗口本身就是一扇 Git Bash（mintty）风格的终端：黑底、Consolas 等宽字体、带颜色标记的输出，底部提示符后可直接输入命令。右键菜单和 **Options** 设置窗口使用系统原生的浅色界面，与终端窗口区分开。Linux 端仍使用原有命令行，不受影响。

### 命令

在底部提示符后输入，回车执行：

| 命令 | 作用 |
| --- | --- |
| `help` | 显示内置命令 |
| `check` / `test` | 测试中转主机连接 |
| `clone <url> [dest]` | 克隆仓库，省略 `dest` 时自动推导 |
| `fetch` / `pull` / `push` | 同步 `dest` 指向的已有仓库 |
| `cd [path]` | 切换目录，支持 `~`、`..`、`-` 和 `/f/repo` 写法；进入 Git 仓库时同步 `dest` |
| `pwd` | 打印当前目录 |
| `set <key> [value]` | 查看或设置配置项 |
| `config` | 打印当前配置 |
| `save` | 写入配置文件（不含密码） |
| `options` | 打开设置窗口 |
| `clear` / `cls` | 清屏 |
| `shell <cmd>` | 显式交给 Git Bash 执行 |
| `exit` | 退出 |

**其它命令不再报“未知命令”，而是交给本机 Git Bash 执行。** 因此 `ls`、`ll`、`cat`、`less`、`grep`、`find`、`cp`、`mv`、`rm`、`mkdir`、`touch`、`chmod`、`git`、`ssh`、`scp`、`tar`、`unzip`、`curl`、`make`、`python`、`ps`、`kill`、`which` 等都能直接敲，并且支持管道、通配符和重定向：

```bash
ls -la | grep .py
cd /f/repo && git log --oneline -5
find . -name "*.toml" | head
echo hello > note.txt
```

`cd` 的实际结果会回读并同步到底部提示符。若与内置命令同名（`test`、`set`、`help` 等），在行首加 `!` 强制走 bash，例如 `!test -f README.md`。

在终端里输入 `git clone <url> [dest]` 与内置 `clone` 完全等价：由中转主机下载、打包，再回传到本地，不走本机直连。支持 `--depth N`、`--branch B`、`--recurse-submodules`、`-j N`；省略 `dest` 时克隆到当前目录下、以仓库名命名的目录。

```bash
git clone git@github.com:sunbines/3FS.git
git clone --depth 1 https://github.com/ceph/ceph.git ceph
git clone --recurse-submodules -j 8 https://github.com/x/y.git deep/y
git clone --direct https://github.com/ceph/ceph.git   # 不走中转，原生直连
```

想用原生 git 直连，加 `--direct`，或在行首加 `!`（`!git clone ...`）。出现中转覆盖不到的参数（`--filter`、`--mirror`、`--bare`、多个路径等）时，整行会自动交回原生 git 执行，语义不变。

`fetch` / `pull` / `push`（包括 `git fetch` 这类直连命令）会自动补上 `--progress`（已经写了 `--progress` 或 `-q` 时不动）。git 默认只在 stderr 是终端时输出进度，而这里的输出走管道，补参数后进度才能取到：百分比和传输速率显示在左下状态栏，日志里只保留阶段节点与最终结果，不会被成百上千次刷新刷屏。中转任务同样如此：远端 `git clone` 的百分比与 SSH 传输速率都在状态栏原地刷新。没有进度输出的长命令，状态栏会显示已运行时长（`运行中 12s`），便于判断是慢还是卡死。

需要交互输入的程序（`ssh` 密码提示、`vi`、`top`）依赖真正的 TTY，当前终端不支持，请在本机 Git Bash 窗口里执行。

`set` 支持的配置项：`host`、`port`、`user`、`password`、`key`、`backend`、`url`、`dest`、`depth`、`branch`、`recurse`、`mode`。密码不会回显，也不会写入配置文件。上下方向键翻历史命令；右键菜单提供 Copy / Paste / Clear。

### 设置

右键任意位置唤出菜单，选 **Options** 打开设置窗口，填写 `host`、`port`、`user`、`password`（或 `key`、`backend`）以及 `url`、`dest`、`depth`、`branch`、`recurse-submodules`、`mode`。**[ Apply ]** 保存，**[ Test connection ]** 测试连接，**[ Clone ]** 开始克隆，**[ Close ]** 关闭。

**[ Apply ]** 会把服务器、用户、端口、私钥和克隆选项（不含密码）写入配置文件，窗口底部会用绿色显示实际写入的路径；写入失败则在同一位置显示红色错误原因（例如没有权限），不再只写进被对话框挡住的终端日志。

Windows 下配置文件默认位于 `%APPDATA%\git-relay\git-relay.toml`（通常是 `C:\Users\<用户>\AppData\Roaming\git-relay\git-relay.toml`）。若旧的 `\etc\git-relay\git-relay.toml` 已存在，则继续沿用旧文件，不会因为升级而读不到原配置。用 `-c/--config` 可以指定其他路径。

**[ Apply ]** 同时会把修改提交到当前会话（提示符里的 `user@host` 会立即更新）。**[ Close ]**、`Esc` 和窗口关闭按钮都会放弃尚未 Apply 的修改，回到上次保存的状态。

密码不会落盘。下次启动需要重新输入，或设置 `GIT_RELAY_PASSWORD` 环境变量。

启动失败时会弹出提示，详细堆栈写入 `git-relay-error.log`（源码运行在仓库根目录，exe 运行在 exe 同目录）。

## Linux 配置文件

安装脚本会把模板复制到 `/etc/git-relay/git-relay.toml`。如果跳过了安装，手动创建：

```bash
sudo mkdir -p /etc/git-relay
sudo cp conf/git-relay.toml.example /etc/git-relay/git-relay.toml
```

编辑 `/etc/git-relay/git-relay.toml`：

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

`/etc/git-relay/git-relay.toml` 由 root 管理，卸载时不会删除。

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

脚本会把 [`script/git-relay`](../script/git-relay) 链接到 PATH，并把配置模板复制到 `/etc/git-relay/git-relay.toml`：

- root 用户默认安装到 `/usr/local/bin`。
- 普通用户默认安装到 `~/.local/bin`。
- 用 `--prefix DIR` 可以安装到 `DIR/bin`。
- 用 `--uninstall` 卸载（不会删除 `/etc/git-relay/git-relay.toml`）；配合 `--build` 同时删除 `.build-venv`、`build`、`dist` 和 `__pycache__`。
- 用 `--force` 覆盖同名文件，或删除不是本仓库安装的同名文件。

配置模板只在目标文件不存在时复制，不会覆盖已有配置。普通用户安装时，如果无权写 `/etc/git-relay`，脚本会尝试用 `sudo` 创建目录并复制模板；没有 `sudo` 时会打印手动执行的命令。

如果 `~/.local/bin` 不在 `PATH`，脚本会打印需要添加的 `export PATH=...`。安装的是指向本仓库的符号链接，仓库移动后需要重新执行安装脚本；不安装时仍可使用 `./script/git-relay`。配置默认读取 `/etc/git-relay/git-relay.toml`，与当前工作目录无关。

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
sudo mv /tmp/git-relay.toml /etc/git-relay/git-relay.toml
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

如果目标目录已经是 Git 仓库，`clone` 不会覆盖它，而是提示改用 `fetch` 或 `pull` 更新已有仓库；需要重新克隆时请指定其他目标目录。

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

## 命令历史（history）

每次运行 `git-relay` 都会把命令追加到历史文件，`history` 子命令用来回看最近执行过的命令：

```bash
git-relay history            # 最近 20 条
git-relay history -n 50      # 最近 50 条
git-relay history -n 0       # 全部
git-relay history --path     # 只打印历史文件位置
git-relay history --clear    # 清空历史
```

输出形如：

```text
2026-11-25 21:47:41  git-relay clone --depth 1 https://github.com/ceph/ceph.git ceph
2026-11-25 21:48:05  git-relay -c /etc/git-relay/git-relay.toml config
```

默认位置：`$XDG_STATE_HOME/git-relay/history`，未设置时为 `~/.local/state/git-relay/history`；Windows 下为 `%LOCALAPPDATA%\git-relay\history`。可用环境变量 `GIT_RELAY_HISTORY=/path/to/file` 指定其他位置（例如放进项目目录方便排查）。

说明：

- 记录的是命令行参数；带空格的参数以 shell 引号形式保存，可直接复制重跑。历史文件按 `0600` 创建，避免 URL 里的 token 被其他用户读到。
- `history` 和 `gui` 自身不计入历史。
- 写入失败（只读家目录、磁盘满）会静默跳过，不影响主命令。
- 文件超过 1000 条时自动裁剪为最近 500 条。

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

- Linux 命令行（源码运行和打包运行）：`/etc/git-relay/git-relay.toml`
- Windows 图形界面：`%APPDATA%\git-relay\git-relay.toml`；若旧的 `\etc\git-relay\git-relay.toml` 已存在则继续沿用

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
