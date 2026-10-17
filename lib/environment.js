/**
 * 环境探测：Python 解释器、matlab.engine 可用性、MATLAB 安装根目录。
 *
 * 设计原则：任何失败都要给出「用户下一步该做什么」，而不是抛一个英文栈。
 * 插件跑在陌生机器上时，这些提示就是他唯一的线索。
 */
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { existsSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"

const run = promisify(execFile)

/** 单次探测的上限，避免卡住 DSH 启动 */
const PROBE_TIMEOUT_MS = 20000

/** 环境类错误：带一条可操作的 hint */
export class EnvironmentError extends Error {
  constructor(message, hint) {
    super(message)
    this.name = "EnvironmentError"
    this.hint = hint
  }
}

async function tryRun(cmd, args, timeout = PROBE_TIMEOUT_MS) {
  try {
    const { stdout, stderr } = await run(cmd, args, {
      timeout,
      windowsHide: true,
      encoding: "utf8",
      maxBuffer: 8 * 1024 * 1024,
    })
    return { ok: true, stdout: (stdout ?? "").trim(), stderr: (stderr ?? "").trim() }
  } catch (error) {
    return {
      ok: false,
      stdout: String(error?.stdout ?? "").trim(),
      stderr: String(error?.stderr ?? "").trim(),
      error,
    }
  }
}

/**
 * 找 Python。
 * 顺序：显式配置 → PATH 上的 python3 → PATH 上的 python → Windows 的 py -3。
 * 返回 { path, version }。
 */
export async function findPython(configured) {
  if (configured) {
    const info = await probePython(configured)
    if (!info.ok) {
      throw new EnvironmentError(
        `配置的 pythonPath 不可用：${configured}`,
        `${info.detail}\n请检查 matlab-bridge 的 pythonPath 配置，或清空它让插件自动探测。`,
      )
    }
    return info
  }

  const candidates = []
  for (const name of ["python3", "python"]) {
    const where = await tryRun(process.platform === "win32" ? "where" : "which", [name])
    if (where.ok) {
      for (const line of where.stdout.split(/\r?\n/)) {
        const p = line.trim()
        if (p) candidates.push(p)
      }
    }
  }

  // Windows 上常见的 launcher
  if (process.platform === "win32") {
    const py = await tryRun("py", ["-3", "-c", "import sys;print(sys.executable)"])
    if (py.ok && py.stdout) candidates.push(py.stdout.split(/\r?\n/).pop().trim())
  }

  const seen = new Set()
  const failures = []
  for (const candidate of candidates) {
    if (!candidate || seen.has(candidate)) continue
    seen.add(candidate)
    if (!existsSync(candidate) && !candidate.includes("python")) continue
    const info = await probePython(candidate)
    if (info.ok) return info
    failures.push(`${candidate}: ${info.detail}`)
  }

  throw new EnvironmentError(
    "找不到可用的 Python 解释器",
    [
      "本插件需要 Python 3.8+（用它承载 MATLAB Engine）。",
      "请在 PATH 上提供 python3 或 python，或者在 matlab-bridge 配置里写死 pythonPath。",
      failures.length > 0 ? `\n已尝试但失败：\n  ${failures.join("\n  ")}` : "",
    ].join(""),
  )
}

/**
 * 探测一个解释器是否可执行、版本是否够。
 */
export async function probePython(exe) {
  const result = await tryRun(exe, ["-c", "import sys;print(sys.version_info[0],sys.version_info[1],sys.executable)"])
  if (!result.ok) {
    return { ok: false, detail: result.stderr || result.error?.message || "无法执行" }
  }
  const parts = result.stdout.split(/\s+/)
  const major = Number(parts[0])
  const minor = Number(parts[1])
  const executable = parts.slice(2).join(" ") || exe
  if (!Number.isFinite(major) || major < 3 || (major === 3 && minor < 8)) {
    return { ok: false, detail: `Python ${major}.${minor} 太旧，需要 3.8+` }
  }
  return { ok: true, path: executable, version: `${major}.${minor}` }
}

/**
 * 检查 matlab.engine 能不能 import。
 * 注意：只 import，不启动引擎 —— 启动一次十几秒，绝不能放在探测里。
 */
export async function probeMatlabEngine(pythonPath) {
  const result = await tryRun(pythonPath, [
    "-c",
    "import matlab.engine, matlab;print(getattr(matlab.engine,'__file__',''))",
  ])
  if (result.ok) {
    return { ok: true, modulePath: result.stdout.split(/\r?\n/).pop().trim() }
  }

  const detail = `${result.stderr}\n${result.stdout}`.trim()
  const missing = /No module named '?matlab/i.test(detail)

  throw new EnvironmentError(
    "当前 Python 里没有 matlab.engine",
    [
      missing
        ? "这是 MATLAB 官方的 Python 引擎，需要单独安装（不是 pip 上的同名包）。"
        : `导入 matlab.engine 失败：\n${detail}`,
      "",
      "安装方法（把 <MATLABROOT> 换成你的 MATLAB 安装目录）：",
      "  Windows:  <MATLABROOT>\\extern\\engines\\python\\setup.py install",
      "  Linux:    cd <MATLABROOT>/extern/engines/python && python3 setup.py install",
      "  macOS:    cd <MATLABROOT>/extern/engines/python && python3 setup.py install",
      "",
      "若 setup.py 因缺 setuptools 失败，可直接把 <MATLABROOT>\\extern\\engines\\python\\dist\\matlab",
      "整个目录复制到 site-packages，然后在 matlab\\engine\\_arch.txt 写四行：",
      "  win64",
      "  <MATLABROOT>\\bin\\win64",
      "  <site-packages>\\matlab\\engine\\win64",
      "  <MATLABROOT>\\extern\\bin\\win64",
      "",
      "文件目录需与当前 Python 匹配（用 python -c \"import sys;print(sys.version)\" 确认位数）。",
    ].join("\n"),
  )
}

/**
 * 从 matlab.engine 的 _arch.txt 反推 MATLAB 根目录。
 * 该文件的第二行就是 <MATLABROOT>\bin\<arch>。
 */
export function inferMatlabRoot(engineModulePath) {
  if (!engineModulePath) return ""
  // engineModulePath 形如 .../site-packages/matlab/engine/__init__.py
  const engineDir = dirname(engineModulePath)
  const archFile = join(engineDir, "_arch.txt")
  if (!existsSync(archFile)) return ""
  try {
    const lines = readFileSync(archFile, "utf8").split(/\r?\n/).map((l) => l.trim())
    const binDir = lines[1]
    if (!binDir) return ""
    // <root>/bin/win64 -> <root>
    const marker = `${join("bin")}`
    const idx = binDir.lastIndexOf(marker)
    return idx > 0 ? binDir.slice(0, idx).replace(/[\\/]+$/, "") : ""
  } catch {
    return ""
  }
}

/**
 * 一次性把环境摸清楚。
 */
export async function resolveEnvironment({ pythonPath = "" } = {}) {
  const python = await findPython(pythonPath)
  const engine = await probeMatlabEngine(python.path)
  const matlabRoot = inferMatlabRoot(engine.modulePath)
  return { python, engine, matlabRoot }
}
