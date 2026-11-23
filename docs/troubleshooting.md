# 故障排查

## 提示“未配置 SSH 服务器地址”

Windows 请打开图形界面填写服务器和用户；Linux 请检查 `/etc/git-relay/git-relay.toml`，或设置：

```bash
export GIT_RELAY_HOST='your-server.example.com'
export GIT_RELAY_USER='your-user'
export GIT_RELAY_PASSWORD='你的密码'
```

## SSH 连接失败 / Permission denied

```bash
git-relay check
```

确认服务器地址、用户、端口、密码或私钥正确。Windows 密码登录建议安装 Paramiko，或使用已内置 Paramiko 的 exe；Linux 可安装 `python3-paramiko`，也可以继续使用 OpenSSH 后端。

## 远端 git clone 失败

工具会把远端 `git clone` 的 stderr 原样打印出来。先手动在远端执行一次：

```bash
ssh your-user@your-server.example.com \
  'git clone --depth 1 https://github.com/example/project.git /tmp/test'
```

确认远端能访问目标仓库。

## 传输中途断开（Broken pipe / 退出码 255）

如果日志出现 `client_loop: send disconnect: Broken pipe`，说明 SSH 连接在回传途中被网络或服务端断开，tar 流被截断。工具会自动重试一次；重试会重新在远端 clone，已传输的字节不会续传。

如果仍然失败：

- 大仓库加 `--depth 1`（以及 `--recurse-submodules --shallow-submodules`）减少传输量。
- 换用更稳定的网络，或检查远端 `sshd` 日志中的断开原因（如会话超时、流量限制）。
- 确认没有中间设备按流量或时长切断长连接。

## Windows 解压时报符号链接错误

启用 Windows 开发者模式，或用管理员终端运行。也可以先不加“同时拉取子模块”，避免回传包含大量符号链接的工作区。

## 目标目录已存在

工具要求目标目录不存在或为空，避免覆盖已有文件。

## 双击 GUI 没有任何窗口

查看 exe 同目录的 `git-relay-error.log`。如果使用源码运行，错误日志在仓库根目录。

## 双击 GUI 弹出 PowerShell 或控制台

确认使用的是 `dist\git-relay.exe`，它使用 GUI 子系统，不应分配控制台。`git-relay-cli.exe` 是命令行版本，运行时会使用当前终端。

## 看不到实时进度

- 进度刷新由令牌桶限流（默认每秒 4 次），小仓库可能瞬间完成，只显示最终统计。
- 命令行终端会原地刷新一行；GUI 和重定向输出会逐行追加。
- 如果长时间没有新进度，先用 `check` 验证 SSH 连通性，再确认远端能访问目标仓库。

## Linux 图形界面无法启动

确认系统已安装 `tkinter`（openEuler 可安装 `python3-devel`，Debian/Ubuntu 为 `python3-tk`）。只使用命令行时不需要该依赖。
