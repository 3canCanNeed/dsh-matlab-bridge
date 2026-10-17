/**
 * MATLAB bridge server 的进程生命周期管理。
 *
 * 这里的每一条都来自实践：Python 子进程必须能自己起来、能自己死掉、
 * 出问题时要留下足够的日志让人判断是「Python 没装」还是「MATLAB 没装」。
 */
import { spawn } from "node:child_process"
import { createServer } from "node:net"
import { existsSync } from "node:fs"

/** 拿一个空闲端口。传 0 时由系统分配。 */
export function findFreePort(preferred) {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.unref()
    server.on("error", reject)
    server.listen(preferred === 0 ? 0 : preferred, "127.0.0.1", () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

/** 端口是否被占用 */
export function isPortBusy(port) {
  return new Promise((resolve) => {
    const server = createServer()
    server.once("error", () => resolve(true))
    server.once("listening", () => server.close(() => resolve(false)))
    server.listen(port, "127.0.0.1")
  })
}

/**
 * 一个 MATLAB bridge server 实例。
 *
 * 状态机：idle → starting → ready → stopping → stopped
 */
export class MatlabServer {
  #proc = null
  #port = 0
  #state = "idle"
  #log = []
  #exitInfo = null
  #logLimit

  constructor({ pythonPath, scriptPath, port = 8765, logger, logLimit = 400, cwd = "" }) {
    this.pythonPath = pythonPath
    this.scriptPath = scriptPath
    this.requestedPort = port
    this.logger = logger
    this.#logLimit = logLimit
    // 显式工作目录：不设时子进程继承 DSH 的 cwd，MATLAB 的 pwd 会跟着漂。
    // 显式指定后 server 日志仍按 __file__ 定位，不受影响。
    this.cwd = cwd
  }

  get state() {
    return this.#state
  }

  get port() {
    return this.#port
  }

  get url() {
    return `http://127.0.0.1:${this.#port}/mcp`
  }

  get running() {
    return this.#proc !== null && this.#state === "ready"
  }

  /** 最近的子进程输出，用于报错时附带 */
  get recentLog() {
    return this.#log.slice(-40).join("\n")
  }

  #record(line) {
    const text = String(line).replace(/\s+$/, "")
    if (!text) return
    this.#log.push(text)
    if (this.#log.length > this.#logLimit) this.#log.shift()
  }

  /**
   * 启动并等到 HTTP 端点可用。
   * 首次启动会顺带启动 MATLAB 引擎（约 15~190 秒），所以超时要给够。
   */
  async start({ timeoutMs = 300000 } = {}) {
    if (this.#state === "ready") return this

    if (!existsSync(this.scriptPath)) {
      throw new Error(`找不到 server 脚本：${this.scriptPath}`)
    }
    if (this.cwd && !existsSync(this.cwd)) {
      throw new Error(`配置的 cwd 不存在：${this.cwd}（检查 matlab-bridge 的 cwd 配置）`)
    }

    // 选端口：优先级 > 自动
    let port = this.requestedPort
    if (!port || (await isPortBusy(port))) {
      port = await findFreePort(0)
      if (port !== this.requestedPort) {
        this.logger?.info?.(`matlab-bridge: 端口 ${this.requestedPort} 不可用，改用 ${port}`)
      }
    }
    this.#port = port
    this.#state = "starting"
    this.#exitInfo = null

    const args = [this.scriptPath, "--http", "--host", "127.0.0.1", "--port", String(port)]
    this.logger?.info?.(`matlab-bridge: 启动 ${this.pythonPath} ${args.join(" ")}`)

    const proc = spawn(this.pythonPath, args, {
      cwd: this.cwd || undefined,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
      },
    })
    this.#proc = proc

    proc.stdout?.setEncoding("utf8")
    proc.stderr?.setEncoding("utf8")
    proc.stdout?.on("data", (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) this.#record(line)
    })
    proc.stderr?.on("data", (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) this.#record(line)
    })

    await new Promise((resolve, reject) => {
      let settled = false
      const done = (fn, value) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        fn(value)
      }

      proc.once("error", (error) => {
        this.#state = "stopped"
        done(reject, new Error(`无法启动 Python 子进程：${error.message}`))
      })

      proc.once("exit", (code, signal) => {
        this.#exitInfo = { code, signal }
        if (this.#state !== "stopping") {
          this.#state = "stopped"
          done(
            reject,
            new Error(
              [
                `MATLAB bridge server 意外退出（code=${code} signal=${signal}）。`,
                this.recentLog ? `最近输出：\n${this.recentLog}` : "",
              ]
                .filter(Boolean)
                .join("\n"),
            ),
          )
        }
      })

      const timer = setTimeout(() => {
        this.#state = "stopped"
        done(
          reject,
          new Error(
            [
              `等待 MATLAB bridge server 就绪超时（${Math.round(timeoutMs / 1000)} 秒）。`,
              "首次启动需要启动 MATLAB 引擎，可能比较慢；如果反复超时，请查看下面的输出：",
              this.recentLog,
            ]
              .filter(Boolean)
              .join("\n"),
          ),
        )
      }, timeoutMs)

      this.#waitUntilHealthy(port, timeoutMs).then(
        () => {
          if (settled) return
          this.#state = "ready"
          this.logger?.info?.(`matlab-bridge: 就绪 ${this.url}`)
          done(resolve)
        },
        (error) => {
          if (settled) return
          done(reject, error)
        },
      )
    })

    return this
  }

  /** 轮询 MCP 端点直到它能正常应答 initialize */
  async #waitUntilHealthy(port, timeoutMs) {
    const deadline = Date.now() + timeoutMs
    const payload = JSON.stringify({
      jsonrpc: "2.0",
      id: 0,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "dsh-matlab-bridge-probe", version: "0" },
      },
    })
    let lastError = ""
    while (Date.now() < deadline) {
      if (this.#state === "stopped") throw new Error("server 进程已退出")
      try {
        const response = await fetch(`http://127.0.0.1:${port}/mcp`, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: payload,
          signal: AbortSignal.timeout(5000),
        })
        if (response.ok) {
          const body = await response.text()
          if (body.includes('"result"')) return
          lastError = `响应异常：${body.slice(0, 200)}`
        } else {
          lastError = `HTTP ${response.status}`
        }
      } catch (error) {
        lastError = error?.message ?? String(error)
      }
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
    throw new Error(
      [
        "MATLAB bridge server 未能通过健康检查。",
        lastError ? `最后一次错误：${lastError}` : "",
        this.recentLog,
      ]
        .filter(Boolean)
        .join("\n"),
    )
  }

  /** 优雅关闭：SIGTERM → 等 5 秒 → SIGKILL */
  async stop({ graceMs = 5000 } = {}) {
    const proc = this.#proc
    if (!proc) {
      this.#state = "stopped"
      return
    }
    this.#state = "stopping"

    await new Promise((resolve) => {
      let finished = false
      const finish = () => {
        if (finished) return
        finished = true
        clearTimeout(killTimer)
        resolve()
      }
      proc.once("exit", finish)

      const killTimer = setTimeout(() => {
        try {
          proc.kill("SIGKILL")
        } catch {
          /* 已经死了 */
        }
        finish()
      }, graceMs)

      try {
        proc.kill("SIGTERM")
      } catch {
        finish()
      }
    })

    this.#proc = null
    this.#state = "stopped"
    this.logger?.info?.("matlab-bridge: server 已停止")
  }
}
