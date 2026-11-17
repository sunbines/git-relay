# 工作原理

```text
本地机器                        远端 Linux 主机
   |  ssh 执行 clone 命令  ----->  git clone --no-checkout
   |                               tar.gz 打包 .git
   |  <----- stdout 流式传输 -----
   |  解压 + git checkout
```

- 远端负责访问 GitHub/GitLab，避免本地网络慢。
- 默认只回传 `.git` 目录，源文件在本地 checkout，传输量更小。
- 加 `--recurse-submodules` 时，远端会连同子模块一起 clone，再打包整个仓库回传。
- 不会修改全局 Git 配置，也不会在本地启动代理。

## 时序图

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant L as 本地 git-relay
    participant S as SSH 通道
    participant R as 远端 Linux 主机
    participant G as GitHub/GitLab

    U->>L: git-relay clone URL DEST
    L->>S: 建立 SSH 连接
    L->>S: 发送远端 clone 命令
    S->>R: 执行 shell 命令

    R->>R: 创建临时目录
    R->>G: git clone --no-checkout [--depth/--branch]
    G-->>R: 仓库对象数据

    alt 默认模式
        R->>R: tar.gz 打包 .git
    else recurse-submodules 模式
        R->>G: 拉取子模块
        G-->>R: 子模块数据
        R->>R: tar.gz 打包整个 repo
    end

    R-->>S: 将 tar.gz 写入 stdout
    S-->>L: 流式传输
    L->>L: 安全解压到临时目录
    L->>L: git checkout
    L->>L: 移动到目标目录
    L-->>U: 仓库就绪

    Note over R,L: clone 进度和统计标记经 stderr 实时转发，令牌桶限流后刷新
    Note over R: 退出时 trap 清理远端临时目录
```

## 进度与速度统计

- 远端 `git clone` 使用 `--progress`，stderr 中的进度经 SSH 实时转发到本地；本地解析 `Receiving objects` 等行，统一成 `clone: ...` 格式。
- 远端在 clone 结束后通过 stderr 发送内部统计标记，包含克隆耗时和仓库大小；该标记不会显示在日志中。
- 本地据此计算远端平均下载速度，并在回传结束后输出远端仓库大小。
- 回传阶段按已接收字节数计算实时速度；本地 checkout 完成后统计目标目录大小。
- 所有进度刷新都经过令牌桶限流（默认 4 次/秒，容量 2）：终端中原地刷新一行，非终端环境（GUI、重定向）逐行输出。
- Paramiko 后端直接读取 SSH 通道的 stderr，OpenSSH 后端使用 `read1`，避免缓冲区等待导致进度滞后。

## SSH 后端

- `paramiko`：纯 Python 实现，密码登录稳定，exe 默认内置。
- `openssh`：调用系统 `ssh`；使用密码时通过 `SSH_ASKPASS` 辅助。
- `auto`：有密码且安装了 Paramiko 时优先 Paramiko，否则使用 OpenSSH。

## 限制

- 目前是“克隆加速器”，不是 Git remote helper；已有仓库的增量 `fetch` 仍需手动处理。
- 每次执行会重新在远端 clone；后续可以扩展为远端缓存仓库 + 增量 bundle。
- Git LFS 仓库只回传指针文件；本地 `git checkout` 时仍会直连 GitHub 拉取 LFS 对象，这部分不会被加速。
- 密码只保存在进程内存或环境变量中；图形界面默认不保存密码。
