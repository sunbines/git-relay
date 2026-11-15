# 故障排查

## 提示“未配置 SSH 服务器地址”

Windows 请打开图形界面填写服务器和用户；Linux 请检查 `conf/git-relay.toml`，或设置：

```bash
export GIT_RELAY_HOST='your-server.example.com'
export GIT_RELAY_USER='your-user'
export GIT_RELAY_PASSWORD='你的密码'
```

## SSH 连接失败 / Permission denied

```bash
python3 src/git_relay.py check
```

确认服务器地址、用户、端口、密码或私钥正确。Windows 密码登录建议安装 Paramiko，或使用已内置 Paramiko 的 exe。

## 远端 git clone 失败

工具会把远端 `git clone` 的 stderr 原样打印出来。先手动在远端执行一次：

```bash
ssh your-user@your-server.example.com \
  'git clone --depth 1 https://github.com/example/project.git /tmp/test'
```

确认远端能访问目标仓库。

## Windows 解压时报符号链接错误

启用 Windows 开发者模式，或用管理员终端运行。也可以先不加“同时拉取子模块”，避免回传包含大量符号链接的工作区。

## 目标目录已存在

工具要求目标目录不存在或为空，避免覆盖已有文件。

## 双击 GUI 没有任何窗口

查看 exe 同目录的 `git-relay-error.log`。如果使用源码运行，错误日志在仓库根目录。

## 双击 GUI 弹出 PowerShell 或控制台

确认使用的是 `dist\git-relay.exe`，它使用 GUI 子系统，不应分配控制台。`git-relay-cli.exe` 是命令行版本，运行时会使用当前终端。

## Linux 图形界面无法启动

确认系统已安装 `tkinter`（部分发行版包名为 `python3-tk`）。只使用命令行时不需要该依赖。
