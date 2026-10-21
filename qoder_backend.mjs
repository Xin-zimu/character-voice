#!/usr/bin/env node
/**
 * qoder_backend.mjs — 可选后端：把本机的 Qoder 连接器暴露成 OpenAI 兼容端点。
 *
 * ## 为什么需要它
 *
 * `dsh-qoder-connect` 插件本身已经有一个 loopback shim（插件 src/shim.ts），
 * 但它的端口是随机的、Bearer 是每进程随机的，两者都只活在 DSH 进程内存里，
 * 不落盘。外部程序（`server.py`）无法发现它，所以这里用**插件自己的代码**
 * 再起一个已知地址的端点。
 *
 * 这不是重新实现 Qoder 协议：凭证读取、请求签名、WASM 编码、SSE 转换
 * 全部走插件导出的模块。本文件只负责「发现插件、选变体、起端点、报地址」。
 *
 * ## 用法
 *
 *   node qoder_backend.mjs                    # 国内版 qoder，随机端口
 *   node qoder_backend.mjs --provider qoder-global
 *   node qoder_backend.mjs --port 39280
 *
 * 启动后向 stdout 打一行 JSON，并把同样的内容写进状态文件，
 * 供 `server.py` 的 `/api/backends` 发现：
 *
 *   {"base_url":"http://127.0.0.1:PORT","token":"…","provider":"qoder",
 *    "region":"china","models":[…]}
 *
 * 也可以 `node qoder_backend.mjs --check` 只做体检，不起端点、不打印 token。
 *
 * ## 安全
 *
 * - 只绑 127.0.0.1
 * - 每个请求都要 Bearer（每次启动重新生成）
 * - 不把 Qoder 的 PAT 打印或写盘；token 是本地端点自己的，不是上游凭证
 * - 状态文件权限按当前用户可读（Windows 下由目录 ACL 决定）
 *
 * ## 这个后端的脆弱点（必须知道）
 *
 * 它依赖插件的内部构建产物：
 *   - 目录结构 `lib/index.js` + `lib/variants-*.js`
 *   - `createQoderTransport` 在压缩产物里，导出名是单字母别名，
 *     所以本文件按**函数名**查找，不按别名
 *
 * 插件升级、改打包方式、或改掉该函数名，这个后端就会失效。
 * 失效时应当**明确报错**，不要静默降级成「用不了但看起来能用」。
 */
import { existsSync, readFileSync, readdirSync, writeFileSync, mkdirSync, unlinkSync } from 'node:fs'
import path from 'node:path'
import os from 'node:os'

const HOME = os.homedir()
const STATE_DIR = path.join(HOME, '.dsh', 'qoder-backend')
const STATE_FILE = path.join(STATE_DIR, 'state.json')

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback
}
const providerId = flag('--provider', 'qoder')
const wantPort = Number(flag('--port', '0'))
const checkOnly = args.includes('--check')

/** 依次尝试几个已知的插件安装位置。 */
function findPlugin() {
  if (process.env.QODER_CONNECT_PLUGIN) {
    const p = process.env.QODER_CONNECT_PLUGIN
    return existsSync(p) ? p : null
  }
  const profilesDir = path.join(HOME, '.dsh', 'profiles')
  const candidates = []
  if (existsSync(profilesDir)) {
    for (const profile of readdirSync(profilesDir)) {
      candidates.push(path.join(profilesDir, profile, 'node_modules', 'dsh-qoder-connect', 'lib', 'index.js'))
    }
  }
  // 全局安装的情况
  candidates.push(path.join(HOME, 'AppData', 'Roaming', 'npm', 'node_modules', 'dsh-qoder-connect', 'lib', 'index.js'))
  return candidates.find(p => existsSync(p)) ?? null
}

/** 从压缩产物里按函数名找导出，避开单字母别名。 */
async function loadTransportFactory(libDir) {
  const chunk = readdirSync(libDir).find(f => /^variants-.*\.js$/.test(f))
  if (!chunk) throw new Error(`插件产物里找不到 variants-*.js（${libDir}）`)
  const mod = await import('file:///' + path.join(libDir, chunk).replace(/\\/g, '/'))
  const factory = Object.values(mod).find(v => typeof v === 'function' && v.name === 'createQoderTransport')
  if (!factory) throw new Error('variants 产物里没有 createQoderTransport —— 插件可能改了打包方式')
  return factory
}

/** 读插件已保存的模型目录；拿不到就用插件内置 fallback。 */
function loadSavedModels(variant) {
  const tag = variant.id === 'qoder' ? 'qoder' : 'qoder-global'
  const dirs = [
    path.join(HOME, '.dsh', 'profiles', 'desktop', '.dsh-qoder-connect', 'state'),
    path.join(HOME, '.dsh', 'profiles', 'web', '.dsh-qoder-connect', 'state'),
  ]
  for (const dir of dirs) {
    const f = path.join(dir, `.${tag}-catalog.json`)
    try {
      if (!existsSync(f)) continue
      const entries = JSON.parse(readFileSync(f, 'utf8'))?.entries ?? {}
      for (const key of Object.keys(entries)) {
        const models = entries[key]?.models
        if (Array.isArray(models) && models.length) return models
      }
    } catch { /* 换下一个 */ }
  }
  return []
}

const pluginPath = findPlugin()
if (!pluginPath) {
  console.error('找不到 dsh-qoder-connect 插件。')
  console.error('用 QODER_CONNECT_PLUGIN 指向它的 lib/index.js，或先在 DSH 里安装该插件。')
  process.exit(2)
}

const mod = await import('file:///' + pluginPath.replace(/\\/g, '/'))
const { QoderCredentialStore, QoderUpstreamClient, QoderCatalog, createQoderShim, variantFor } = mod
if (!QoderCredentialStore || !createQoderShim) {
  console.error('插件导出缺少 QoderCredentialStore / createQoderShim，版本不兼容。')
  process.exit(2)
}

const variant = variantFor?.(providerId)
if (!variant) {
  console.error(`未知 provider: ${providerId}`)
  process.exit(2)
}

const store = new QoderCredentialStore({ variant })

try {
  await store.resolve()
} catch (err) {
  console.error(`没有可用凭证（${variant.displayName}）：${err?.message ?? err}`)
  console.error(`先在 DSH 里登录该插件，或运行：dsh-qoder-connect pat set <token> --provider ${providerId}`)
  process.exit(2)
}

if (checkOnly) {
  process.stdout.write(`ok  ${variant.displayName}  ${variant.region}  plugin=${pluginPath}\n`)
  process.exit(0)
}

const createQoderTransport = await loadTransportFactory(path.dirname(pluginPath))
const transport = createQoderTransport({
  region: variant.region,
  resolvePat: () => store.patPromise(),
})
const client = new QoderUpstreamClient({
  region: variant.region,
  providerId: variant.id,
  getPat: () => store.patPromise(),
  transport,
})

const saved = loadSavedModels(variant)
const catalog = saved.length
  ? new QoderCatalog(saved.map(m => ({
      id: m.id,
      name: m.name ?? m.id,
      contextWindow: m.contextWindow ?? 128000,
      maxTokens: m.maxTokens ?? 32000,
      supportsImages: m.supportsImages ?? true,
      billing: m.billing ?? { free: false, rateUnknown: true },
      ...(m.reasoning ? { reasoning: m.reasoning } : {}),
    })))
  : new QoderCatalog()

const log = (...a) => console.error('[qoder-backend]', ...a)
const shim = createQoderShim({
  store, client, catalog, providerId: variant.id,
  ...(wantPort ? { port: wantPort } : {}),
  logger: { warn: log, error: log },
})

await shim.ready
const baseUrl = shim.baseUrl()
const port = new URL(baseUrl).port

let models = []
let reasoningModels = []
try {
  const r = await fetch(`${baseUrl}/v1/models`, {
    headers: { Authorization: `Bearer ${shim.token()}`, Host: `127.0.0.1:${port}` },
  })
  if (r.ok) models = (await r.json()).data?.map(m => m.id) ?? []
} catch { /* 列表拿不到不影响对话 */ }

// 从插件目录读每个模型是否带推理，供前端避开「默认选了会先想很久的模型」。
try {
  const tag = variant.id === 'qoder' ? 'qoder' : 'qoder-global'
  for (const dir of [
    path.join(os.homedir(), '.dsh', 'profiles', 'desktop', '.dsh-qoder-connect', 'state'),
    path.join(os.homedir(), '.dsh', 'profiles', 'web', '.dsh-qoder-connect', 'state'),
  ]) {
    const f = path.join(dir, `.${tag}-catalog.json`)
    if (!existsSync(f)) continue
    const entries = JSON.parse(readFileSync(f, 'utf8'))?.entries ?? {}
    for (const key of Object.keys(entries)) {
      for (const m of entries[key]?.models ?? []) {
        if (m?.reasoning?.supports) reasoningModels.push(m.id)
      }
    }
    if (reasoningModels.length) break
  }
} catch { /* 拿不到就当全部非推理 */ }

// 推荐模型：只根据「能不能用」来排，不根据「像不像她」。
// 后者靠一两轮对话判断不出来，也不是这里该决定的事（见 VALIDATION.md 的证据分级）。
//
// 已知事实：
//   mmodel 调用直接 502（可能只是暂时性，所以仍列在可选项里，只是不推荐）
//   auto 是重推理模型，正文很久才出来
// 其余模型的排序只表示「先试这个」，不代表谁更像。
const PREFER = ['dfmodel', 'qfmodel', 'qmodel', 'dmodel', 'gmodel', 'kmodel']
const recommended = PREFER.find(id => models.includes(id)) ?? models[0] ?? ''

const doc = {
  base_url: baseUrl,
  token: shim.token(),
  provider: variant.id,
  region: variant.region,
  models,
  reasoning_models: reasoningModels,
  recommended,
  pid: process.pid,
  started_at: new Date().toISOString(),
}

try {
  mkdirSync(STATE_DIR, { recursive: true })
  writeFileSync(STATE_FILE, JSON.stringify(doc, null, 2), 'utf8')
} catch (err) {
  log('状态文件写入失败（不影响对话）:', err?.message ?? err)
}

process.stdout.write(JSON.stringify(doc) + '\n')

const stop = async () => {
  try { await shim.close() } catch {}
  try {
    // 只删自己写的那份，避免关掉别的实例的状态。
    const cur = JSON.parse(readFileSync(STATE_FILE, 'utf8'))
    if (cur.pid === process.pid) unlinkSync(STATE_FILE)
  } catch {}
  process.exit(0)
}
process.on('SIGINT', stop)
process.on('SIGTERM', stop)
