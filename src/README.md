# src

按平台拆成三个包。依赖方向只有一个：**客户端 → 共用**，`common` 不会反向引用 `linux` / `win11`，所以 Linux 端永远不会碰到 tkinter。

```text
src/
├── common/     共用引擎（平台无关）
├── linux/      Linux 命令行客户端
└── win11/      Windows 11 图形界面
```

## common/ — 共用引擎

真正干活的部分：连远端主机、下载仓库、回传解包。两个客户端都只是它的外壳。

| 文件 | 职责 |
| --- | --- |
| `relay_common.py` | 常量、配置加载与渲染、本地 git/解压辅助、日志与进度条 |
| `relay_ssh.py` | SSH 后端（系统 ssh / paramiko）、stderr 进度转发、上下行速度统计 |
| `relay_ops.py` | 远端命令构建，clone / submodule / fetch / pull / push / check |
| `toml_parse.py` | TOML 读取，标准库 `tomllib`，缺失时退回 `tomli` |

## linux/ — 命令行客户端

| 文件 | 职责 |
| --- | --- |
| `git_relay.py` | 入口：参数解析、子命令分发（`clone` / `submodules` / `fetch` / `pull` / `push` / `check` / `history` / `config` / `gui`） |
| `relay_history.py` | `history` 子命令的落盘记录 |

## win11/ — 图形界面

| 文件 | 职责 |
| --- | --- |
| `git_relay_gui.py` | Tk 终端窗口：右键 Options、内置命令、其余输入交本机 Git Bash |
| `git_relay_gui_launcher.py` | 无控制台入口；启动失败写 `git-relay-error.log` 并弹框 |

## 怎么跑

三个包都没有安装步骤，入口脚本会把 `src/` 自己插进 `sys.path`：

```bash
# Linux / 任意有 Python 3.9+ 的地方
python3 src/linux/git_relay.py --version
./script/git-relay check

# Windows（源码运行图形界面）
python src\win11\git_relay_gui_launcher.py
```

打包同理，PyInstaller 的 `pathex` 指向本目录即可（见 `packaging/*.spec`）。

## 改动时注意

- 新功能优先放 `common/`，客户端只做参数翻译和界面，避免两端各写一份逻辑。
- `common/` 里不要 import `linux` / `win11`，也不要 import tkinter —— 否则 Linux 端会多出图形库依赖。
- 模块之间用绝对包名导入（`from common.relay_ops import clone`），不要用相对路径或裸模块名，打包时才能正常解析。
- 面向用户的字符串是中文；注释和 docstring 用英文，和现有代码保持一致。
