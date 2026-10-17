#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MATLAB MCP Bridge —— 把常驻 MATLAB 引擎包装成 MCP 工具
=======================================================

问题：每次用 MATLAB 都要重新启动，开销 15~190 秒。
方案：MCP server 常驻内存（由 DSH 拉起），MATLAB 引擎懒启动，之后每次调用 0.01~0.5 秒。

传输：stdio（每行一个 JSON-RPC 2.0 消息）
工具：matlab_eval / matlab_run / matlab_status / matlab_stop

注意：stdout 是协议通道，任何调试输出必须走 stderr。
"""

import atexit
import json
import os
import re
import signal
import sys
import threading
import time
import traceback

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "matlab-bridge"
SERVER_VERSION = "1.1.0"

# Windows 上 stdout 默认是 GBK，中文工具描述会乱码；MCP 规定 UTF-8。
for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MAX_ARRAY_ELEMENTS = 200     # 超过就折叠成摘要，避免几十万数字灌进上下文
MAX_TEXT_CHARS = 8000        # 单次返回文本上限
START_TIMEOUT_SECONDS = 300  # 等待引擎启动的上限（与 toolCallTimeoutMs 对齐）

# MATLAB 回显 `ans = ...` 的段落；有结构化结果时从输出里剥掉，避免重复
_ANS_BLOCK = re.compile(r"\n*ans\s*=\s*\n+[\s\S]*\Z")

# MATLAB 终端会把类型名包成 <a href="matlab:helpPopup('struct')">struct</a>，
# 灌进对话上下文纯属噪音，剥成裸文本。
_MATLAB_LINK = re.compile(r'<a href="matlab:[^"]*"[^>]*>(.*?)</a>', re.S)

# 引擎已经废掉的征兆：执行 exit/quit 会抛 InterruptedError，
# 之后所有调用一律变成 "Unknown exception"。
_ENGINE_DEAD_HINTS = (
    "Unknown exception",
    "已中断执行 MATLAB 命令",
    "engine has been shut down",
    "MATLABProcessID",
)


# ---------------------------------------------------------------- 日志

# 由 DSH 拉起时 stderr 被 MCP SDK 接管，看不到输出；同时落一份盘便于排查"到底有没有被 spawn"。
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
_LOG_READY = True


def log(msg):
    """调试信息走 stderr，绝不污染 stdout 协议通道。"""
    try:
        sys.stderr.write("[matlab-bridge] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass
    global _LOG_READY
    if not _LOG_READY:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s [pid %s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:
        _LOG_READY = False


# ---------------------------------------------------------------- 序列化

def to_jsonable(value, limit=MAX_ARRAY_ELEMENTS):
    """把 MATLAB 返回值转成可 JSON 序列化的 Python 对象。

    超大数组折叠成摘要：2500 个元素的 magic(50) 全量 JSON 是几百 KB，
    足以把对话上下文烧穿，而它几乎从不是想要的东西。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    # matlab.double / matlab.logical / 普通数组 -> 嵌套 list 或摘要
    try:
        import numpy as np
        arr = np.asarray(value)
        if arr.size > limit:
            flat = arr.ravel()
            info = {"shape": list(arr.shape), "dtype": str(arr.dtype), "size": int(arr.size)}
            # 复数矩阵的 min/max 会抛异常，缺失时就不报值域
            try:
                info["min"] = flat.min().item()
                info["max"] = flat.max().item()
            except Exception:
                pass
            try:
                info["first"] = flat[:16].tolist()
            except Exception:
                info["first"] = [str(x) for x in flat[:16]]
            return {"__summary__": info}
        return arr.tolist()
    except Exception:
        pass

    try:
        return [to_jsonable(x, limit) for x in value]
    except Exception:
        pass

    return repr(value)


def stringify_result(value):
    """给模型看的紧凑文本：标量原样，数组用紧凑 JSON，超大数组给摘要。"""
    v = to_jsonable(value)

    if isinstance(v, dict) and "__summary__" in v:
        s = v["__summary__"]
        shape = "x".join(str(x) for x in s["shape"])
        head = json.dumps(s["first"], ensure_ascii=False, separators=(",", ":"))
        if "min" in s:
            return ("[矩阵 %s %s，共 %d 个元素，未全量返回] 值域 [%s, %s]，开头 16 个: %s"
                    % (shape, s["dtype"], s["size"], s["min"], s["max"], head))
        return ("[矩阵 %s %s，共 %d 个元素，未全量返回] 开头 16 个: %s"
                % (shape, s["dtype"], s["size"], head))

    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)) or v is None:
        return json.dumps(v)
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(v)


def truncate(text, limit=MAX_TEXT_CHARS):
    """输出截断 —— for i=1:100000, disp(i), end 能产出几 MB 文本。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（输出过长已截断，原长 %d 字符）" % len(text)


def is_empty_serialization(text):
    """判断序列化结果是不是「一对括号」这种没有信息量的东西。

    引擎把 function handle、categorical、containers.Map 之类转成 Python 对象时
    只会给出空壳，stringify_result 就得到 `[[[]]]`。这种结果不但没用，还会把
    MATLAB 自己的文本回显挤掉（见 eval 里的处理）。

    `[]`（0×0 空矩阵）也会被判为真空 —— 那正是想要的：改用 MATLAB 的回显
    「0×0 empty double matrix」，比一对括号清楚。
    """
    if not text:
        return False
    return set(text) <= set("[], ")


def strip_ans_block(output):
    """剥掉 evalc 回显的 `ans = ...` 段落（已经有结构化结果时不需要它）。"""
    if not output:
        return ""
    return _ANS_BLOCK.sub("", output).strip()


def clean_output(text):
    """剥掉 MATLAB 终端塞进来的 helpPopup 超链接，留下纯文本。"""
    if not text:
        return ""
    return _MATLAB_LINK.sub(lambda m: m.group(1), text)


# ---------------------------------------------------------------- 引擎

class MatlabBridge(object):
    """持有 MATLAB 引擎，并把调用串行化（引擎本身不支持并发）。"""

    def __init__(self):
        self.engine = None
        self.lock = threading.RLock()       # 保护状态字段
        self.call_lock = threading.Lock()   # 串行化 MATLAB 调用
        self.state = "stopped"              # stopped | starting | ready | error
        self.start_seconds = None
        self.started_at = None
        self.last_error = None
        self.call_count = 0
        self.total_seconds = 0.0

    # ---------------------------------------------------------- 启动

    def ensure_started(self):
        """懒启动。并发调用时只有一个线程真正去启动，其余等待。"""
        with self.lock:
            if self.state == "ready":
                return
            if self.state == "error":
                # 不自动重试：启动失败几乎都是配置问题，重试只会每次再白等几十秒。
                # 用户可以先调 matlab_stop 把状态复位，再重新触发一次启动。
                raise RuntimeError(
                    "MATLAB 引擎此前启动失败：%s（可调用 matlab_stop 复位后重试）" % self.last_error
                )
            if self.state == "starting":
                starter = False
            else:
                self.state = "starting"
                self.last_error = None
                starter = True

        if not starter:
            deadline = time.time() + START_TIMEOUT_SECONDS
            while time.time() < deadline:
                with self.lock:
                    if self.state == "ready":
                        return
                    if self.state == "error":
                        raise RuntimeError("MATLAB 引擎启动失败: %s" % self.last_error)
                time.sleep(0.5)
            raise RuntimeError("等待 MATLAB 引擎启动超时（%d 秒）" % START_TIMEOUT_SECONDS)

        # 真正启动 —— 期间把 stdout 的 C 层输出改道到 stderr，防止污染协议
        t0 = time.time()
        saved_stdout_fd = None
        try:
            try:
                saved_stdout_fd = os.dup(1)
                os.dup2(2, 1)          # fd 1 -> fd 2（stderr）
            except Exception:
                saved_stdout_fd = None

            log("正在启动 MATLAB 引擎（约 15~190 秒）…")
            import matlab.engine
            self.engine = matlab.engine.start_matlab()

            elapsed = time.time() - t0
            with self.lock:
                self.state = "ready"
                self.start_seconds = elapsed
                self.started_at = time.time()
            log("引擎就绪，启动耗时 %.1f 秒" % elapsed)
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
            log("启动失败: %s" % traceback.format_exc())
            raise
        finally:
            if saved_stdout_fd is not None:
                try:
                    os.dup2(saved_stdout_fd, 1)
                    os.close(saved_stdout_fd)
                except Exception:
                    pass

    # ---------------------------------------------------------- 执行

    def _record(self, elapsed):
        with self.lock:
            self.call_count += 1
            self.total_seconds += elapsed

    @staticmethod
    def _engine_unusable(exc):
        """判断这次异常是不是「引擎已经死了」。

        用户在会话里执行 exit / quit 会让 MATLAB 进程直接退出，evalc 抛
        InterruptedError，此后每次调用都报 Unknown exception。不处理的话
        会话就永久废掉，而 status 还骗人地显示 ready。
        """
        if isinstance(exc, InterruptedError):
            return True
        msg = str(exc)
        return any(h in msg for h in _ENGINE_DEAD_HINTS)

    def _discard_engine(self):
        """丢掉已经不可用的引擎引用并复位状态，让下次调用能重新启动。"""
        self.engine = None
        with self.lock:
            self.state = "stopped"
            self.started_at = None
            self.start_seconds = None

    def _execute(self, code):
        """统一执行入口：一次 evalc 搞定表达式与语句。

        为什么不用 engine.eval()：它是「表达式」求值器，`a = [1 2 3]` 会被当成
        相等比较而报 SyntaxError。而「先 eval、失败再 evalc」会让有副作用的语句
        执行两次（disp/plot/写文件）—— 那是真 bug。evalc 一次执行，副作用只发生
        一次，还能同时拿到 ans 和打印输出。
        """
        self.ensure_started()
        with self.call_lock:
            t0 = time.time()
            try:
                # 先 clear ans：否则纯赋值语句会返回上一次残留的 ans。
                # 注意若代码有语法错误，clear 也不会执行，但那时我们抛异常、
                # 不会去读 ans，所以不会被陈旧值骗到。
                output = clean_output(self.engine.evalc("clear ans; " + code))

                # 用回显文本判断有没有 ans，而不是「直接读、失败再捕获」——
                # 后者在不产生 ans 的语句上要花 2.7 秒构造异常。
                result = None
                if "ans =" in output:
                    try:
                        result = self.engine.workspace["ans"]
                    except Exception:
                        # 2D char 数组、稀疏矩阵等类型引擎没法转成 Python 对象，
                        # 这不是错误：output 里已经有可读的文本回显了。
                        result = None

                elapsed = time.time() - t0
                self._record(elapsed)
                return {"ok": True, "output": output, "result": result, "elapsed": elapsed}
            except Exception as exc:
                elapsed = time.time() - t0
                self._record(elapsed)
                if self._engine_unusable(exc):
                    log("引擎已失效（可能执行了 exit/quit），复位状态以便下次重启")
                    self._discard_engine()
                    return {
                        "ok": False,
                        "error": ("引擎已停止（检测到 exit/quit 或执行被中断）。"
                                  "状态已复位，下次调用会自动重启。"),
                        "elapsed": elapsed,
                    }
                return {
                    "ok": False,
                    "error": "%s: %s" % (type(exc).__name__, str(exc).strip()),
                    "elapsed": elapsed,
                }

    def eval(self, code):
        """求值：优先返回结构化结果，没有则返回打印输出。"""
        r = self._execute(code)
        elapsed = round(r["elapsed"], 3)
        if not r["ok"]:
            return {"ok": False, "error": r["error"], "elapsed": elapsed}

        raw = r["output"] or ""

        # 先算出结构化结果的文本形态，再决定要不要用它。
        result_text = None
        if r["result"] is not None:
            result_text = stringify_result(r["result"])
            # `[[[]]]` / `[[]]` / `[]` 这种纯括号结果没有信息量 —— 引擎把
            # function handle、categorical、containers.Map 之类转成 Python
            # 对象时只给空壳。这种情况下退回 MATLAB 的文本回显，否则用户
            # 只会看到一对括号，完全不知道算出了什么。
            if is_empty_serialization(result_text):
                result_text = None

        # 关键：只有真的要返回结构化结果时才剥掉 ans 回显块。否则（char 矩阵、
        # 稀疏矩阵、上面那类空壳类型）整段剥掉就等于把结果扔了。
        extra = strip_ans_block(raw) if result_text is not None else raw.strip()

        parts = []
        # 先打印输出、后结果，保持与代码执行顺序一致的阅读顺序
        if extra:
            parts.append(extra)
        if result_text is not None:
            parts.append(result_text)
        text = "\n".join(parts).strip() or "(执行成功，无返回值)"
        return {"ok": True, "text": truncate(text), "elapsed": elapsed}

    def run(self, code):
        """执行脚本片段：返回捕获到的打印输出。"""
        r = self._execute(code)
        elapsed = round(r["elapsed"], 3)
        if not r["ok"]:
            return {"ok": False, "error": r["error"], "elapsed": elapsed}

        text = truncate((r["output"] or "").strip())
        if not text and r["result"] is not None:
            text = truncate(stringify_result(r["result"]))
        if not text:
            text = "(执行成功，无输出)"
        return {"ok": True, "output": text, "elapsed": elapsed}

    # ---------------------------------------------------------- 生命周期

    def status(self):
        with self.lock:
            return {
                "state": self.state,
                "start_seconds": None if self.start_seconds is None else round(self.start_seconds, 1),
                "uptime_seconds": None if self.started_at is None else round(time.time() - self.started_at, 1),
                "call_count": self.call_count,
                "avg_call_seconds": round(self.total_seconds / self.call_count, 3) if self.call_count else None,
                "last_error": self.last_error,
            }

    def stop(self):
        """关闭引擎并把状态完整复位（否则 start_seconds / last_error 会残留）。"""
        with self.call_lock:
            if self.engine is not None:
                try:
                    self.engine.quit()
                except Exception:
                    log("关闭引擎时出错: %s" % traceback.format_exc())
                self.engine = None
            with self.lock:
                self.state = "stopped"
                self.started_at = None
                self.start_seconds = None
                self.last_error = None
        return {"ok": True}


BRIDGE = MatlabBridge()


def _shutdown_engine():
    """进程退出时关掉 MATLAB，否则引擎会变成没人管的孤儿进程（占几百 MB）。"""
    eng = getattr(BRIDGE, "engine", None)
    if eng is not None:
        try:
            eng.quit()
        except Exception:
            pass
        BRIDGE.engine = None


def _on_signal(signum, frame):
    _shutdown_engine()
    os._exit(0)


atexit.register(_shutdown_engine)
try:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
except Exception:
    pass


# ---------------------------------------------------------------- 工具定义

TOOLS = [
    {
        "name": "matlab_eval",
        "description": (
            "在常驻 MATLAB 会话中执行代码并返回结果。工作区跨调用保持（先 A=[1 2;3 4]，"
            "再 det(A) 可以直接用）。表达式给值、赋值语句给状态、disp/fprintf 的打印也会一并返回。"
            "首次调用会启动引擎（15~100 秒），之后每次 0.01~0.5 秒。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "MATLAB 代码，可以是表达式（1+1）、赋值（A=pascal(4)）或多条语句（x=1; y=2; x+y）",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "matlab_run",
        "description": (
            "在常驻 MATLAB 会话中执行脚本片段，返回 disp/fprintf 捕获到的文本输出。"
            "适合多行脚本、循环、绘图前的数据准备、调用自己写的函数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "MATLAB 语句（可多行）"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "matlab_status",
        "description": "查看 MATLAB 引擎状态：是否就绪、启动耗时、已调用次数、平均每次耗时。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "matlab_stop",
        "description": "关闭 MATLAB 引擎并释放内存（约 300MB~1GB），同时复位状态。下次调用会重新启动。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------- 协议

def make_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def make_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def text_content(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def error_content(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _code_arg(args):
    """取出并校验 code 参数；返回 (code, 错误消息)。"""
    code = args.get("code", "")
    if not isinstance(code, str):
        return None, "code 必须是字符串（收到 %s）" % type(code).__name__
    if not code.strip():
        return None, "code 不能为空"
    return code, None


def handle_tool_call(msg_id, params):
    name = params.get("name")
    args = params.get("arguments") or {}

    try:
        if name == "matlab_eval":
            code, err = _code_arg(args)
            if err:
                return make_result(msg_id, error_content(err))
            r = BRIDGE.eval(code)
            if r["ok"]:
                return make_result(msg_id, text_content(r["text"]))
            return make_result(msg_id, error_content("MATLAB 报错: " + r["error"]))

        if name == "matlab_run":
            code, err = _code_arg(args)
            if err:
                return make_result(msg_id, error_content(err))
            r = BRIDGE.run(code)
            if r["ok"]:
                return make_result(msg_id, text_content(r["output"]))
            return make_result(msg_id, error_content("MATLAB 报错: " + r["error"]))

        if name == "matlab_status":
            return make_result(msg_id, text_content(json.dumps(BRIDGE.status(), ensure_ascii=False, indent=2)))

        if name == "matlab_stop":
            BRIDGE.stop()
            return make_result(msg_id, text_content("MATLAB 引擎已关闭。"))

        return make_result(msg_id, error_content("未知工具: %s" % name))

    except Exception as exc:
        log("工具调用异常: %s" % traceback.format_exc())
        return make_result(msg_id, error_content("%s: %s" % (type(exc).__name__, exc)))


def handle(msg):
    """处理一条 JSON-RPC 消息；通知类消息返回 None。"""
    method = msg.get("method")
    # 判断「是不是通知」必须看有没有 id 这个键，不能看 id 的值：
    # JSON-RPC 里 "id": null 是一个合法请求（响应也回 null），
    # 只有完全不带 id 键的才是通知，才不需要回复。
    has_id = "id" in msg
    msg_id = msg.get("id")

    if not has_id and method is not None:
        log("通知: %s" % method)
        return None

    if method == "initialize":
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        client = params.get("clientInfo") or {}
        log("客户端: %s %s" % (client.get("name"), client.get("version")))
        return make_result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "tools/list":
        log("← tools/list 已响应（%d 个工具）" % len(TOOLS))
        return make_result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        return handle_tool_call(msg_id, params)

    if method == "ping":
        return make_result(msg_id, {})

    return make_error(msg_id, -32601, "Method not found: %s" % method)


def emit(response):
    try:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        log("写响应失败: %s" % traceback.format_exc())


def run_http(host, port):
    """streamable-http 传输：单端点 POST 收 JSON-RPC，回 JSON 或 SSE。

    为什么要有这个模式：stdio 模式在 dsh-mcp-client 下握手成功后
    拿不到 tools/list（见 README 的已知问题），HTTP 是绕过那条路径的备选。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静音默认访问日志

        def _write_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            log("HTTP POST %s len=%s accept=%r session=%r" % (
                self.path, n, self.headers.get("Accept"), self.headers.get("Mcp-Session-Id")))
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            raw = raw.lstrip("\ufeff").strip()

            if not raw:
                self._write_json(make_error(None, -32700, "Parse error"))
                return

            try:
                msg = json.loads(raw)
            except Exception:
                # 兼容批量请求（JSON 数组）
                self._write_json(make_error(None, -32700, "Parse error"))
                return

            if isinstance(msg, list):
                responses = []
                for item in msg:
                    if not isinstance(item, dict):
                        responses.append(make_error(None, -32600, "Invalid Request"))
                        continue
                    r = handle(item)
                    if r is not None:
                        responses.append(r)
                if not responses:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._write_json(responses)
                return

            if not isinstance(msg, dict):
                self._write_json(make_error(None, -32600, "Invalid Request"))
                return

            try:
                response = handle(msg)
            except Exception:
                log("处理异常: %s" % traceback.format_exc())
                if "id" in msg:
                    self._write_json(make_error(msg.get("id"), -32603, "Internal error"))
                return

            if response is None:
                # 通知：按规范回 202 且无 body
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            accept = (self.headers.get("Accept") or "").lower()
            if "text/event-stream" in accept:
                # 客户端优先 SSE：回一条 message 事件
                data = "event: message\ndata: " + json.dumps(response, ensure_ascii=False) + "\n\n"
                body = data.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._write_json(response)

        def do_GET(self):
            log("HTTP GET %s accept=%r" % (self.path, self.headers.get("Accept")))
            # 不主动推送消息，所以没有可订阅的 SSE 流
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self):
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log("HTTP 模式已监听 http://%s:%d/mcp （POST JSON-RPC）" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log("HTTP 服务已停止")


def main():
    log("MCP server 启动 v%s，pid=%d，cwd=%s" % (SERVER_VERSION, os.getpid(), os.getcwd()))
    log("argv=%r" % (sys.argv,))

    if "--http" in sys.argv:
        host = "127.0.0.1"
        port = 8765
        args = sys.argv[1:]
        for i, a in enumerate(args):
            if a in ("--host", "--port") and i + 1 < len(args):
                if a == "--host":
                    host = args[i + 1]
                else:
                    port = int(args[i + 1])
            elif a.startswith("--port="):
                port = int(a.split("=", 1)[1])
            elif a.startswith("--host="):
                host = a.split("=", 1)[1]
        run_http(host, port)
        return

    for line in sys.stdin:
        # 去掉可能的 UTF-8 BOM：PowerShell / 记事本另存的 JSONL 会带，
        # 而 json.loads 见到 "\ufeff" 就报 Parse error。
        line = line.lstrip("\ufeff").strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except Exception:
            # JSON-RPC 规定解析失败也要回 -32700，否则客户端只能干等到超时
            emit(make_error(None, -32700, "Parse error"))
            continue

        if not isinstance(msg, dict):
            emit(make_error(None, -32600, "Invalid Request"))
            continue

        try:
            response = handle(msg)
        except Exception:
            # 内部错误同样必须回响应，不能静默 continue
            log("处理异常: %s" % traceback.format_exc())
            if "id" in msg:
                emit(make_error(msg.get("id"), -32603, "Internal error"))
            continue

        if response is not None:
            emit(response)

    log("stdin 关闭，退出")


if __name__ == "__main__":
    main()
