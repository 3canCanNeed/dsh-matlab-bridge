# dsh-matlab-bridge

> 把 MATLAB 引擎变成 DSH 的常驻工具：一次启动，之后每次调用 0.02 ~ 2 秒。

[English](#english) · 简体中文

---

## 这是什么

MATLAB 命令行每次冷启动要 60 ~ 190 秒，没法在对话里反复用。这个插件把 MATLAB Engine 挂在常驻的 Python 进程里，再通过 DSH 的工具接口暴露出来：

- **第一次调用**付一次引擎启动成本（约 15 ~ 20 秒，冷启动可到 100 秒以上）
- **之后每次调用** 0.02 ~ 2 秒，工作区变量跨调用保留
- 不用手动开 MATLAB，不用手动开桥接进程 —— 插件随 DSH 启动而拉起 server，随 DSH 退出而清理

## 架构

```text
DSH 主进程
└─ dsh-matlab-bridge 插件（lib/index.js）
   ├─ environment.js   探测 Python / matlab.engine / MATLAB 安装位置
   ├─ session.js       spawn 并管理 server 子进程（自带重启与清理）
   └─ MCP over HTTP ──► matlab_mcp_server.py  127.0.0.1:<port>/mcp
                          └─ 懒启动 ──► MATLAB Engine（Python 进程内常驻）
```

- **插件层**只负责“让 server 活着”和“把 4 个工具注册进 DSH”，不碰 MATLAB 本身。
- **server 层**是一个**零依赖**的 Python MCP server（只用标准库 + `matlab.engine`），同时支持 stdio 和 streamable-http 两种传输。
- **引擎层**懒启动：server 起来时不碰 MATLAB，第一次 `tools/call` 才加载 `matlab.engine`。

## 特性

- **零 Python 依赖**：server 只用标准库，不需要 `pip install mcp`
- **懒启动 + 自动重连**：引擎在首次调用时才启动；`exit`/`quit` 弄死引擎后下次调用自动重启
- **工作区贯通**：变量在多次 `matlab_eval` 之间保留，像同一个 MATLAB 会话
- **结构化结果优先**：数值矩阵返回真实数组，不是字符串截断；转不了 Python 对象的类型（char 矩阵、稀疏矩阵、`table`、`categorical`…）自动退回 MATLAB 原始回显
- **大结果防爆**：超过 200 个元素的数组折叠成摘要，文本输出超过 8000 字符截断
- **环境自动探测**：不配 `pythonPath` 也能用（PATH + `MATLAB_ROOT`/`matlabroot` 推断），探测结果写日志

## 安装

### 前置条件

1. **MATLAB**（R2021a 及以上建议）已安装，且许可证可用
2. **MATLAB Engine API for Python** 装好 —— 见 [官方文档](https://www.mathworks.com/help/matlab/matlab_external/install-the-matlab-engine-for-python.html)：
   ```powershell
   cd "<MATLAB_ROOT>\extern\engines\python"
   python -m pip install .
   ```
3. **Python 3.9+**，且能 `python -c "import matlab.engine"` 成功

### 安装插件

把本仓库放进 DSH 的插件目录（或任意位置后在配置里引用），然后在
`~/.dsh/profiles/<profile>/cordis.patch.yml` 里加一段：

```yaml
- insert:
    - id: matlab-bridge
      name: dsh-matlab-bridge
      config:
        pythonPath: ""          # 留空 = 自动探测
        port: 8765
        autoStart: true
```

完整可配置项见 [`examples/cordis.patch.yml.example`](examples/cordis.patch.yml.example)。

重启 DSH，工具列表里应出现 `matlab_eval` / `matlab_run` / `matlab_status` / `matlab_stop`。

### 验证

```text
matlab_eval("a = [10 20 30]; sum(a)")   →  60
matlab_status()                          →  {"state":"ready", ...}
```

首次调用会等引擎启动（约 15 ~ 20 秒），之后即快。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `pythonPath` | string | `""` | Python 解释器路径。空 = 自动探测（PATH → `py -3` → 常见安装目录） |
| `port` | number | `8765` | server 监听端口。端口被占用时 `session.js` 会探测并报错 |
| `autoStart` | boolean | `true` | 插件激活时自动拉起 server；关掉则只做“连接已有 server” |
| `connectUrl` | string | `""` | 连接已存在的 server（例如手动 `python matlab_mcp_server.py --http` 起的）。非空时 `autoStart` 不生效 |
| `startupTimeoutMs` | number | `300000` | 等 server 就绪的超时。慢机器可加大 |
| `cwd` | string | `""` | server 子进程的工作目录。空 = 继承 DSH 进程 cwd |
| `toolPrefix` | string | `"matlab"` | 工具名前缀，最终暴露为 `<prefix>_eval` 等 |

## 工具

### `matlab_eval`

执行单条表达式或语句，返回结构化结果。

```text
matlab_eval("magic(3) * 2")
matlab_eval("format long; pi")
```

- 内部用 `evalc` 执行，`disp` 的输出与返回值一起返回
- 大数组折叠为摘要（`[3×3 double: ...]` 风格），需要全量请自己写文件（`writematrix`/`save`）

### `matlab_run`

执行多行脚本（函数体、循环、画图前处理等）。

```text
matlab_run("x = 0:0.1:2*pi; y = sin(x); plot(x, y); saveas(gcf, 'out.png')")
```

- 适合多语句、`for`/`while`、调用自己写的函数
- 跑 `.m` 脚本文件：`matlab_run("run('D:/path/script.m')")`
- 跑函数文件：`matlab_run("addpath('D:/path'); myfunc(3)")` —— 直接 `run()` 函数文件只会加载定义、不会调用

### `matlab_status`

```json
{
  "state": "ready",
  "start_seconds": 18.4,
  "uptime_seconds": 7.2,
  "call_count": 6,
  "avg_call_seconds": 0.886,
  "last_error": null
}
```

`state` 取值：`stopped` / `starting` / `ready` / `error`。

### `matlab_stop`

关闭引擎并释放内存（MATLAB 常驻约 300MB ~ 1GB）。下次调用会自动重启。

## 性能参考

Windows 11 + MATLAB Engine for Python 实测：

| 场景 | 耗时 |
|---|---|
| 引擎启动 | 13.8 ~ 17.6 秒（磁盘冷启动 99 ~ 188 秒） |
| 单次 `1+1` | 0.01 ~ 0.06 秒 |
| `disp('hi')` | 0.02 秒 |
| `magic(3)` | 0.66 秒（首次含 JIT 预热） |
| `magic(50)`（2500 元素，折叠摘要） | 1.8 秒 |
| 运行时错误（未定义函数） | 1.0 ~ 1.4 秒 |
| 首次画图并存盘 | 47 秒（图形系统冷启动），第二次约 16 秒 |
| 20 万行 `disp` 输出 | 6.5 秒（`evalc` 要全收进内存） |
| 对照：每次重开 MATLAB 命令行 | 60 ~ 190 秒 |

## 设计说明

### 为什么用 HTTP 而不是 stdio

`matlab_mcp_server.py` 两种传输都支持，但插件默认走 HTTP：

在 DSH 0.2.0-rc.2 上实测，stdio 模式下 `dsh-mcp-client` 能完成 `initialize` 握手，但之后**不发 `tools/list`**，约 107 秒后关闭连接重连，形成循环，工具始终注册不上。用官方 MCP SDK 直连同一份 server 却能正常列出 4 个工具 —— 瓶颈在客户端的 tools capability 判断，不在 server。

HTTP 模式（`--http`）下这套组合工作正常，所以插件把 server 作为 HTTP 服务拉起并连接。server 的 stdio 模式仍保留，用于其他 MCP 客户端或本地调试。

### 为什么用 `evalc`

`eval` 会把 `disp` 的输出直接写到 Python 的 stdout，混进协议流；`evalc` 把输出收进字符串，再和返回值一起返回，顺序稳定、可截断。代价是大输出要占内存（见性能表最后一行）。

### 引擎状态机

`stopped → starting → ready`，失败进 `error` 并**不自动重试**（启动失败几乎都是配置问题，重试只会每次白等几十秒）。`error` 状态下调 `matlab_stop` 可复位。唯一例外：检测到“引擎已被 `exit` 弄死”时自动复位，下次调用重新启动。

## 已知限制

- **单线程**：MATLAB Engine 是单线程的，所有调用排队；并发工具调用会串行执行
- **超时不中断 MATLAB**：超时只是调用方放弃等待，MATLAB 那边仍在跑并占着锁
- **DSH 重启后首次调用冷启动**：引擎随 server 进程退出，重启 DSH 后第一次调用要重新等启动
- **图形窗口不可见**：无头会话里 `figure`/`plot` 能执行但看不到窗口，存图用 `saveas`/`print`
- **中文变量名不可用**：`温度 = 25.5` 会报 `SyntaxError: 文本字符无效`。这是 MATLAB 引擎调用链的限制，不是插件问题 —— 中文字符串（`disp('中文')`）完全正常
- **不支持 `function` 定义块**：`matlab_eval` 会在前面拼 `clear ans;`，函数定义没法这样拼；请写文件后用 `matlab_run` + `addpath`

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named 'matlabengineforpython_abi3'` | `matlab.engine` 的 `_arch.txt` 第 3 行不是 `matlab\engine\win64` | 改 `_arch.txt`，或重装 engine |
| 首次调用超时 | `startupTimeoutMs` 太小 | 设成 300000 以上 |
| 工具列表里没有 `matlab_*` | 插件没加载 / 配置语法错 | 看 DSH 日志；确认 `cordis.patch.yml` 缩进 |
| 引擎启动报 `File system inconsistency` | MATLAB 需要完整文件系统权限，受限沙箱下起不来 | 在完整权限下运行 |
| 引擎启动抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbe` | **假象**：MATLAB 其实是被沙箱挡死，它用 GBK 写的报错被按 UTF-8 解码 | 不要被误导，用 `matlab.exe -batch "disp(1)"` 直接跑一次看真错误 |
| 某类值只返回文本 | char 矩阵 / 稀疏矩阵等引擎转不了 Python 对象 | 正常，文本回显里有完整内容 |
| 调用报「引擎已停止…下次调用会自动重启」 | 执行了 `exit` / `quit` | 无需处理，再调一次即可 |
| 下次调用立刻报「此前启动失败」 | `error` 状态不自动重试 | 调 `matlab_stop` 复位 |

## 开发

仓库内不带 `node_modules`。本地自检（不经 DSH，直接验证 server 一条链路）：

```bash
npm install          # 或 pnpm install
node scripts/selfcheck.mjs
```

它会探测环境 → 起 server → 发 `initialize` → 调一次 `matlab_eval` → 关掉，全程打印耗时。

> 这个脚本会 spawn Python 子进程。如果跑在受限文件沙箱里（例如 DSH 的 workspace-write），
> Node 的 spawn 会被拒绝并报 `EPERM`，请换到普通终端或完整权限环境运行。
> 自动探测不到 Python 时，可用 `SELFCHECK_PYTHON` 指定解释器。

手工起 server（HTTP 模式）：

```bash
python bridge/matlab_mcp_server.py --http --port 8765
```

## 目录结构

```text
dsh-matlab-bridge/
├── lib/
│   ├── index.js         插件入口：配置校验、工具注册、生命周期
│   ├── environment.js   Python / matlab.engine / MATLAB root 探测
│   └── session.js       server 子进程管理（启动、就绪探测、停止、清理）
├── bridge/
│   └── matlab_mcp_server.py   零依赖 MCP server（stdio + streamable-http）
├── scripts/
│   └── selfcheck.mjs    端到端自检
├── examples/
│   └── cordis.patch.yml.example
├── package.json
├── LICENSE
└── README.md
```

## 许可

[MIT](LICENSE)

---

<a id="english"></a>
## English

**dsh-matlab-bridge** exposes a persistent MATLAB engine as DSH tools. It starts a zero-dependency Python MCP server (streamable HTTP) that lazily loads `matlab.engine` on first call, then exposes `matlab_eval`, `matlab_run`, `matlab_status` and `matlab_stop` to the DSH agent. First call pays a ~15–20 s engine startup; subsequent calls take 0.02–2 s and share a stateful workspace. Requires MATLAB plus the MATLAB Engine API for Python, and Python 3.9+.
