/**
 * 端到端自检：环境探测 → 起 server → 打一次 MCP 调用 → 关掉。
 *
 * 不经过 DSH，直接跑：
 *   node scripts/selfcheck.mjs
 *
 * 首次运行会等 MATLAB 引擎启动（约 15~20 秒），之后引擎随脚本退出。
 */
import { resolveEnvironment } from "../lib/environment.js";
import { MatlabServer } from "../lib/session.js";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, "..", "bridge", "matlab_mcp_server.py");
const PORT = Number(process.env.SELFCHECK_PORT ?? 8799);

const t0 = Date.now();
const step = (label) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s] ${label}`);

step("1/4 探测环境 …");
let env;
try {
  env = await resolveEnvironment({ pythonPath: process.env.SELFCHECK_PYTHON ?? "" });
} catch (err) {
  console.error("");
  console.error(`环境探测失败：${err.message}`);
  console.error("如果报错是 EPERM / spawn 失败，说明当前终端跑在受限文件沙箱里");
  console.error("（Node 无法 spawn 子进程），请换到完整权限的终端再试。");
  console.error("如果只是找不到解释器，可以显式指定：");
  console.error('  SELFCHECK_PYTHON="C:\\Python312\\python.exe" node scripts/selfcheck.mjs');
  process.exit(1);
}
step(`    Python ${env.python.version} → ${env.python.path}`);
step(`    matlab.engine → ${env.engine.modulePath}`);
step(`    MATLAB root → ${env.matlabRoot || "(未推断出)"}`);

step("2/4 启动 server …");
const server = new MatlabServer({
  pythonPath: env.python.path,
  scriptPath: SCRIPT,
  port: PORT,
  logger: { info: (m) => console.log(`    ${m}`), warn: (m) => console.log(`    ! ${m}`) },
});
await server.start({ timeoutMs: 180000 });
step(`    ready at ${server.url}`);

step("3/4 调 matlab_eval …");
const init = await fetch(server.url, {
  method: "POST",
  headers: { "content-type": "application/json", accept: "application/json" },
  body: JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "selfcheck", version: "0" } },
  }),
});
step(`    initialize → HTTP ${init.status}`);

const call = await fetch(server.url, {
  method: "POST",
  headers: { "content-type": "application/json", accept: "application/json" },
  body: JSON.stringify({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "matlab_eval", arguments: { code: "a = [10 20 30]; sum(a)" } },
  }),
});
const body = await call.json();
const text = body?.result?.content?.[0]?.text ?? JSON.stringify(body).slice(0, 300);
step(`    tools/call → ${text}`);

step("4/4 关闭 server …");
await server.stop();
step(`    完成，总耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
process.exit(0);
