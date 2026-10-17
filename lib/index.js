/**
 * dsh-matlab-bridge —— 把 MATLAB 变成一个可以反复调用的工具。
 *
 * 解决的问题：MATLAB 每次冷启动要 15~190 秒。做仿真、算矩阵、画图时，
 * 一次会话里反复启动是纯浪费。这个插件在后台常驻一个 MATLAB 引擎，
 * 通过 MCP 暴露 matlab_eval / matlab_run / matlab_status / matlab_stop 四个工具。
 *
 * 架构：
 *   DSH  ←MCP(HTTP)→ Python server ←matlab.engine→ MATLAB
 *
 * 为什么用 HTTP 而不是 stdio：stdio 模式下 dsh-mcp-client 与 Python 子进程
 * 存在握手后不发 tools/list 的兼容问题（详见 README「已知问题」）。
 */
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client"
import z from "@deepseek-ai/schemastery"
import { createMcpToolDefinition } from "@deepseek-ai/dsh-mcp-client"

import { EnvironmentError, resolveEnvironment } from "./environment.js"
import { MatlabServer } from "./session.js"

const HERE = dirname(fileURLToPath(import.meta.url))
const SERVER_SCRIPT = join(HERE, "..", "bridge", "matlab_mcp_server.py")

export const name = "matlab-bridge"
export const inject = ["tools"]

export const Config = z.object({
  /** 显式指定 Python 解释器；留空则自动探测 */
  pythonPath: z.string().default(""),
  /** server 端口；0 表示自动挑选空闲端口 */
  port: z.number().step(1).min(0).max(65535).default(8765),
  /** 是否由插件自己拉起 server；false 时只连接已有 server */
  autoStart: z.boolean().default(true),
  /** 直接连接一个已在运行的 server（设置后忽略 autoStart） */
  connectUrl: z.string().default(""),
  /** 首次启动等待上限（毫秒）。MATLAB 引擎冷启动很慢，默认 5 分钟 */
  startupTimeoutMs: z.number().min(1000).default(300000),
  /** server 进程（以及它拉起的 MATLAB 引擎）的工作目录；留空则用插件根目录 */
  cwd: z.string().default(""),
  /** 工具名前缀 */
  toolPrefix: z.string().default("matlab"),
})

/** 四个工具的静态定义（与 bridge/matlab_mcp_server.py 保持一致） */
const TOOL_SPECS = [
  {
    name: "matlab_eval",
    description:
      "在常驻 MATLAB 会话里执行一段代码并返回结果。工作区变量在多次调用之间保留。" +
      "多语句可用分号或换行分隔；有返回值时返回 ans，无返回值时返回执行输出。",
    inputSchema: {
      type: "object",
      properties: {
        code: { type: "string", description: "要执行的 MATLAB 代码" },
      },
      required: ["code"],
      additionalProperties: false,
    },
  },
  {
    name: "matlab_run",
    description:
      "执行一段 MATLAB 脚本（多行），返回完整屏幕输出而不是 ans。" +
      "适合跑 .m 文件内容或需要看到 fprintf 输出的场景。",
    inputSchema: {
      type: "object",
      properties: {
        code: { type: "string", description: "要执行的 MATLAB 脚本" },
      },
      required: ["code"],
      additionalProperties: false,
    },
  },
  {
    name: "matlab_status",
    description: "查询 MATLAB 会话状态：是否已启动、启动耗时、调用次数、最近错误。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "matlab_stop",
    description: "关闭 MATLAB 引擎并释放内存。下次调用会自动重新启动。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
]

/**
 * 插件主体。
 *
 * 启动策略：不等 server 就绪就返回，工具先注册上；真正连不上时，
 * 调用工具会得到一条可读的错误，而不是让 DSH 卡在启动阶段。
 */
export async function apply(ctx, config) {
  const log = ctx.logger
  const state = {
    server: null,
    client: null,
    ready: false,
    starting: null,
    lastError: null,
  }

  async function connectClient(url) {
    const transport = new StreamableHTTPClientTransport(new URL(url))
    const client = new Client(
      { name: "dsh-matlab-bridge", version: "0.1.0" },
      { capabilities: {} },
    )
    await client.connect(transport)
    return client
  }

  async function bootServer() {
    // 1. 显式 connectUrl：只连不启
    if (config.connectUrl) {
      log.info?.(`matlab-bridge: 连接已有 server ${config.connectUrl}`)
      const client = await connectClient(config.connectUrl)
      return { client, server: null }
    }

    // 2. 探测环境
    const env = await resolveEnvironment({ pythonPath: config.pythonPath })
    log.info?.(
      `matlab-bridge: Python ${env.python.version} (${env.python.path})` +
        (env.matlabRoot ? `，MATLAB ${env.matlabRoot}` : ""),
    )

    // 3. 拉起 server
    const server = new MatlabServer({
      pythonPath: env.python.path,
      scriptPath: SERVER_SCRIPT,
      port: config.port,
      cwd: config.cwd || join(HERE, ".."),
      logger: log,
    })
    await server.start({ timeoutMs: config.startupTimeoutMs })
    const client = await connectClient(server.url)
    return { client, server }
  }

  function ensureReady() {
    if (state.ready) return Promise.resolve()
    if (state.starting) return state.starting

    state.starting = (async () => {
      try {
        const { client, server } = await bootServer()
        state.client = client
        state.server = server
        state.ready = true
        state.lastError = null
      } catch (error) {
        state.lastError = error
        state.starting = null
        throw error
      }
    })()

    // 后台失败时不要让 unhandled rejection 冒出来
    state.starting.catch(() => {})
    return state.starting
  }

  async function callTool(toolName, args) {
    try {
      await ensureReady()
    } catch (error) {
      const hint = error instanceof EnvironmentError && error.hint ? `\n\n${error.hint}` : ""
      throw new Error(`${error.message}${hint}`)
    }
    if (!state.client) throw new Error("MATLAB 会话尚未就绪")
    const result = await state.client.callTool({ name: toolName, arguments: args })
    return result
  }

  // ---- 注册工具 ----
  const disposers = []
  for (const spec of TOOL_SPECS) {
    const publicName = `${config.toolPrefix}_${spec.name.replace(/^matlab_/, "")}`
    disposers.push(
      ctx.tools.register(
        createMcpToolDefinition(ctx, {
          name: publicName,
          rawName: spec.name,
          description: spec.description,
          inputSchema: spec.inputSchema,
          call: (args) => callTool(spec.name, args),
        }),
      ),
    )
  }
  log.info?.(`matlab-bridge: 已注册 ${disposers.length} 个工具（前缀 ${config.toolPrefix}_）`)

  // ---- 后台预热 ----
  if (config.autoStart && !config.connectUrl) {
    ensureReady().catch((error) => {
      log.warn?.(
        [
          "matlab-bridge: 后台启动失败，工具会在首次调用时重试。",
          error.message,
          error instanceof EnvironmentError && error.hint ? error.hint : "",
        ]
          .filter(Boolean)
          .join("\n"),
      )
    })
  }

  // ---- 卸载清理 ----
  ctx.effect(
    () => () => {
      for (const dispose of disposers) {
        try {
          dispose()
        } catch {
          /* 忽略 */
        }
      }
      const client = state.client
      const server = state.server
      state.client = null
      state.server = null
      state.ready = false
      Promise.resolve()
        .then(() => client?.close?.())
        .catch(() => {})
        .finally(() => server?.stop({ graceMs: 5000 }).catch(() => {}))
    },
    "matlab-bridge.lifecycle",
  )
}
