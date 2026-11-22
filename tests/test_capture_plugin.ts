// personal-memory 捕获插件 — fake-harness 单元测试（Bun test）
//
// 边界（本 lane 只测插件，不测 Python）：
//  - 全程 mock child_process.execFile（工厂 opts.execFile 注入）与 SDK client
//    （工厂 opts.client 注入的假 client），不读真实 DB、不触发真实 capture.py、
//    不启动 OpenCode server、无网络。
//  - 会话主/子判断走 client.session.get（父会话无 parentID，子会话有 parentID），
//    不再依赖 sqlite3 CLI 与任何本机 DB 路径。
//  - 不产生任何真实会话数据；不写 learning/ 与 user-profile/。
//  - memory_ops lane（tests/test_memory_ops.py）归 parent，互不冲突。
//
// 运行：bun test tests/test_capture_plugin.ts
import { afterEach, beforeEach, describe, test, expect, spyOn } from "bun:test"
import { EventEmitter } from "node:events"
import * as pluginExports from "../plugin/personal-memory-capture"
import { PersonalMemoryCapturePlugin } from "../plugin/personal-memory-capture"

// ---- 唯一 id 生成（避免模块级缓存跨测试串扰） ---------------------------------

let seq = 0
function uid(prefix: "ses" | "msg"): string {
  seq += 1
  const stem = seq.toString(36).padStart(10, "0") + "abc"
  return `${prefix}_${stem}`      // 符合 ^(ses|msg)_[A-Za-z0-9]+$
}

// ---- 假 client harness：会话主/子判断走 OpenCode SDK client --------------------

type ClientReply = "main" | "sub" | "nodata" | "throw" | "wrongid" | "hang"

/**
 * 假 client：session.get({path:{id}}) 返回 { data } 包裹的 Session。
 *  - "main"   → data 无 parentID（根会话）
 *  - "sub"    → data.parentID 为合成 ses_ 串（子会话）
 *  - "nodata" → 空对象（无 data，模拟查不到）
 *  - "throw"  → 抛异常（模拟瞬时失败）
 * gets 记录每次查询的 id，供“结论缓存 / unknown 不缓存”断言。
 */
function mkClient(reply: (sid: string) => ClientReply) {
  const gets: string[] = []
  const client = {
    session: {
      get: async ({ path }: { path: { id: string } }) => {
        gets.push(path.id)
        const r = reply(path.id)
        if (r === "throw") throw new Error("synthetic-client-failure")
        if (r === "nodata") return {}
        if (r === "hang") return new Promise<never>(() => {})   // never resolves
        if (r === "wrongid") return { data: { id: "ses_someoneelse01" } }  // id mismatch
        if (r === "sub") return { data: { id: path.id, parentID: "ses_testparent01" } }
        return { data: { id: path.id } }
      },
    },
  }
  return { client, gets }
}

// ---- 假 execFile harness（仅 python 子进程）-----------------------------------

// 平台/环境相关的 Python 可执行名，与插件内保持同一推导逻辑。
const PYTHON_BIN = process.env.PERSONAL_MEMORY_PYTHON
  || (process.platform === "win32" ? "python" : "python3")

interface Call {
  file: string
  args: string[]
  opts: Record<string, any>
  stdinChunks: Buffer[]
  stdinEnded: boolean
  kills: string[]
  completed: boolean
  finish: () => void
  expire: () => void
}

interface PythonReply {
  err?: any
  stdout?: string
  stderr?: string
  stdin?: "missing" | "error" | "throw"
  hold?: boolean
  throwSpawn?: boolean
}

/**
 * 构造假 execFile：只剩 python 子进程（会话判断已不走子进程）。
 * stdin 写入按调用序记录，供断言“正文只走 stdin、不在 argv”。
 */
function mkHarness(python: () => PythonReply = () => ({})) {
  const calls: Call[] = []
  const exec: any = (file: string, args: string[], opts: any, cb: any) => {
    const reply = python()
    if (reply.throwSpawn) throw new Error("synthetic-sensitive-spawn-detail")
    const rec: Call = { file, args, opts, stdinChunks: [], stdinEnded: false,
      kills: [], completed: false, finish: () => {}, expire: () => {} }
    calls.push(rec)
    const stdin: any = new EventEmitter()
    stdin.end = (b: any) => {
      if (reply.stdin === "throw") throw new Error("synthetic-sensitive-stdin-detail")
      if (b !== undefined) rec.stdinChunks.push(Buffer.from(b))
      rec.stdinEnded = true
      if (reply.stdin === "error") stdin.emit("error", new Error("synthetic-sensitive-EPIPE"))
    }
    const child: any = {
      stdin: reply.stdin === "missing" ? null : stdin,
      kill: (signal: string) => { rec.kills.push(signal); return true },
    }
    rec.expire = () => child.kill(opts.killSignal)
    rec.finish = () => {
      if (rec.completed) return
      rec.completed = true
      cb(reply.err ?? null, reply.stdout ?? '{"ok":true,"status":"unmatched"}', reply.stderr ?? "")
    }
    if (!reply.hold) queueMicrotask(rec.finish)
    return child
  }
  return { calls, exec }
}

// ---- 组合环境：假 execFile + 假 client -----------------------------------------

function mkEnv(reply: (sid: string) => ClientReply, python: () => PythonReply = () => ({})) {
  const h = mkHarness(python)
  const c = mkClient(reply)
  return { calls: h.calls, exec: h.exec, client: c.client, gets: c.gets }
}

const pyCalls = (calls: Call[]) => calls.filter((c) => c.file === PYTHON_BIN)

function pyPayload(c: Call): { session_id: string; message_id: string | null; text: string } {
  expect(c.stdinChunks.length).toBe(1)
  return JSON.parse(c.stdinChunks[0].toString("utf8"))
}

// ---- hook 构造 ----------------------------------------------------------------

async function mkHooks(h: { exec: any; client: any }) {
  const hooks = (await (PersonalMemoryCapturePlugin as any)({}, { execFile: h.exec, client: h.client })) as any
  expect(typeof hooks["chat.message"]).toBe("function")
  expect(typeof hooks.event).toBe("function")
  return hooks
}

const mkInput = (sid: string, mid?: string) => ({ sessionID: sid, ...(mid ? { messageID: mid } : {}) })
const mkOutput = (parts: any[], msg?: any) => ({
  message: msg ?? { id: uid("msg"), role: "user" },
  parts,
})

// ---- 测试 ----------------------------------------------------------------------

describe("personal-memory-capture plugin (fake harness)", () => {
  let warnings: ReturnType<typeof spyOn>
  beforeEach(() => { warnings = spyOn(console, "warn").mockImplementation(() => {}) })
  afterEach(() => { warnings.mockRestore() })

  test("module has exactly one runtime plugin export", () => {
    expect(Object.keys(pluginExports)).toEqual(["PersonalMemoryCapturePlugin"])
  })

  test("factory reads the SDK client from PluginInput (production path)", async () => {
    const sid = uid("ses")
    const h = mkHarness()
    const c = mkClient(() => "main")
    // 生产调用形状：Plugin(input) 的 input.client 为 SDK client。
    const hooks = (await (PersonalMemoryCapturePlugin as any)({ client: c.client }, { execFile: h.exec })) as any
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "prod" }]))
    expect(c.gets).toEqual([sid])
    expect(pyCalls(h.calls).length).toBe(1)
  })

  test("main session: real user text forwarded via stdin JSON, text never in argv", async () => {
    const sid = uid("ses")
    const mid = uid("msg")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    // output.message.id 与 input.messageID 同时存在：以稳定的 output.message.id 为准
    await hooks["chat.message"](mkInput(sid, "in_" + mid.slice(4)),
      mkOutput([{ type: "text", text: "hello world" }], { id: mid, role: "user" }))
    const calls = pyCalls(h.calls)
    expect(calls.length).toBe(1)
    const c = calls[0]
    // 脚本路径不再硬编码：从插件自身位置推导，只断言结构。
    expect(c.args[0]).toBe("-B")
    expect(c.args[1].endsWith("capture.py")).toBe(true)
    expect(c.args[2]).toBe("--event-json")
    expect(c.args.join("|")).not.toContain("hello")        // 正文不进 argv
    expect(c.stdinEnded).toBe(true)
    const p = pyPayload(c)
    expect(p).toEqual({ session_id: sid, message_id: mid, text: "hello world" })
    // 会话判断是只读元数据查询，且只查一次。
    expect(h.gets).toEqual([sid])
  })

  test("sub session (parentID present): fail-closed, no capture", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "sub")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "child text" }]))
    expect(pyCalls(h.calls).length).toBe(0)
    // 子会话结论被缓存：第二条消息不再查 client
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "child text 2" }]))
    expect(h.gets.length).toBe(1)
  })

  test("missing session metadata (no data): unknown → skip, NOT cached (re-queried next message)", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "nodata")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "early hook" }]))
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "still early" }]))
    expect(pyCalls(h.calls).length).toBe(0)
    expect(h.gets.length).toBe(2)   // unknown 不写缓存
  })

  test("SDK id mismatch: fail-closed unknown, no capture, not cached", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "wrongid")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "spoofed row" }]))
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "spoofed row 2" }]))
    expect(pyCalls(h.calls).length).toBe(0)   // id 不符绝不当作主会话采集
    expect(h.gets.length).toBe(2)             // unknown 不缓存，每条重查
  })

  test("hung SDK getter: bounded timeout fails closed, no capture, not cached", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "hang")
    const hooks = await mkHooks(h)
    let resolved = false
    const p = hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "hang" }]))
      .then(() => { resolved = true })        // 若无超时，hook 永不 resolve
    await Promise.race([p, new Promise((r) => setTimeout(r, 6000))])
    expect(resolved).toBe(true)               // 有界超时把 hook 放行
    expect(pyCalls(h.calls).length).toBe(0)   // 超时的查询绝不采集
    // 超时得到的 unknown 不缓存：第二条再次查询（仍卡死）
    const p2 = hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "again" }]))
    await Promise.race([p2, new Promise((r) => setTimeout(r, 6000))])
    expect(h.gets.length).toBe(2)
  }, 20000)

  test("transient client failure: skip, later recovery re-queries and captures", async () => {
    const sid = uid("ses")
    let state: ClientReply = "throw"
    const h = mkEnv(() => state)
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "client down" }]))
    expect(pyCalls(h.calls).length).toBe(0)
    expect(h.gets.length).toBe(1)
    state = "main"                            // client 恢复后重查
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "client back" }]))
    expect(pyCalls(h.calls).length).toBe(1)
    expect(pyPayload(pyCalls(h.calls)[0]).text).toBe("client back")
    // 主会话结论现已缓存：第三条不再查 client
    const getCount = h.gets.length
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "cached now" }]))
    expect(h.gets.length).toBe(getCount)
    expect(pyCalls(h.calls).length).toBe(2)
  })

  test("synthetic and ignored parts are skipped", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([
      { type: "text", synthetic: true, text: "synthetic text" },
      { type: "text", ignored: true, text: "ignored text" },
    ]))
    expect(pyCalls(h.calls).length).toBe(0)
  })

  test("whitespace-only text part is skipped (blank rule is python-side, blank skip is plugin-side)", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "   \n\t " }]))
    expect(pyCalls(h.calls).length).toBe(0)
  })

  test("no arbitrary min-length gate: 2-char text IS captured (old min4 removed)", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "ok" }]))
    const calls = pyCalls(h.calls)
    expect(calls.length).toBe(1)
    expect(pyPayload(calls[0]).text).toBe("ok")
  })

  test("no 4000-char truncation: 5000-char text forwarded in full", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const text = "x".repeat(5000)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text }]))
    const calls = pyCalls(h.calls)
    expect(calls.length).toBe(1)
    expect(pyPayload(calls[0]).text).toBe(text)
  })

  test("70 KiB text forwarded intact for Python matched/unmatched policy", async () => {
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const text = "y".repeat(70 * 1024)
    await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text }]))
    expect(pyPayload(pyCalls(h.calls)[0]).text).toBe(text)
    expect(warnings).not.toHaveBeenCalled()
  })

  test("oversize text (>1 MiB): whole message skipped with warning, not truncated", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "y".repeat(1024 * 1024 + 1) }]))
    expect(pyCalls(h.calls).length).toBe(0)
    expect(warnings).toHaveBeenCalledWith("[personal-memory-capture] pmc.event.oversize")
  })

  test("role check: only an explicit user role is captured", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput(
      [{ type: "text", text: "system says" }],
      { id: uid("msg"), role: "system" },
    ))
    await hooks["chat.message"](mkInput(sid), mkOutput(
      [{ type: "text", text: "assistant says" }],
      { id: uid("msg"), role: "assistant" },
    ))
    await hooks["chat.message"](mkInput(sid), mkOutput(
      [{ type: "text", text: "missing role" }],
      { id: uid("msg") },
    ))
    await hooks["chat.message"](mkInput(sid), mkOutput(
      [{ type: "text", text: "unknown role" }],
      { id: uid("msg"), role: "operator" },
    ))
    expect(pyCalls(h.calls).length).toBe(0)
    await hooks["chat.message"](mkInput(sid), mkOutput(
      [{ type: "text", text: "real user" }],
      { id: uid("msg"), role: "user" },
    ))
    expect(pyCalls(h.calls).length).toBe(1)
  })

  test("invalid session IDs rejected without sanitize: no client query, no capture, no throw", async () => {
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const bad = [uid("msg"), "ses; DROP TABLE session;--", "ses_' OR 1=1--", "foo_abc12345", "", "ses_", undefined, 42, "ses_short!.x"]
    for (const s of bad) {
      await hooks["chat.message"]({ sessionID: s as any }, mkOutput([{ type: "text", text: "nope" }]))
    }
    expect(h.gets.length).toBe(0)   // 未清洗成合法 id 进查询
    expect(pyCalls(h.calls).length).toBe(0)
  })

  test("malformed parts handled gracefully: non-array, null entries, missing text", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), { message: { id: uid("msg"), role: "user" }, parts: "not-array" })
    await hooks["chat.message"](mkInput(sid), { message: { id: uid("msg"), role: "user" }, parts: [null, 42, { type: "text" }, {}] })
    await hooks["chat.message"](mkInput(sid), {} as any)   // 无 parts 字段
    expect(pyCalls(h.calls).length).toBe(0)
  })

  test("multi-part text joined with newline, single capture per message", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([
      { type: "text", text: "part one" },
      { type: "other", text: "not a text part" },
      { type: "text", text: "part two" },
    ]))
    const calls = pyCalls(h.calls)
    expect(calls.length).toBe(1)
    expect(pyPayload(calls[0]).text).toBe("part one\npart two")
  })

  test("replay forwarding: distinct message ids forwarded separately for python-side dedup", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "same text" }], { id: uid("msg"), role: "user" }))
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "same text" }], { id: uid("msg"), role: "user" }))
    const calls = pyCalls(h.calls)
    expect(calls.length).toBe(2)
    const p1 = pyPayload(calls[0])
    const p2 = pyPayload(calls[1])
    expect(p1.text).toBe(p2.text)
    expect(p1.message_id).not.toBe(p2.message_id)   // 两条都转发，去重归 Python
  })

  test("message id fallback: input.messageID used when output.message.id absent; null when neither", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"]({ sessionID: sid, messageID: uid("msg") }, { message: { role: "user" }, parts: [{ type: "text", text: "fallback" }] })
    await hooks["chat.message"]({ sessionID: sid }, { message: { role: "user" }, parts: [{ type: "text", text: "no id" }] })
    const calls = pyCalls(h.calls)
    expect(calls.length).toBe(2)
    expect(pyPayload(calls[0]).message_id).not.toBeNull()
    expect(pyPayload(calls[1]).message_id).toBeNull()
  })

  test("python timeout: chat hook resolves without throwing, chat stays healthy", async () => {
    const sid = uid("ses")
    const timeoutErr = Object.assign(new Error("spawn ETIMEDOUT"), { code: null, killed: true })
    const h = mkEnv(() => "main", () => ({ err: timeoutErr }))
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "hang" }]))   // 不应 throw
    expect(pyCalls(h.calls).length).toBe(1)
    // 后续消息仍正常处理（聊天健康）
    const h2 = mkEnv(() => "main")
    const hooks2 = await mkHooks(h2)
    await hooks2["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "after" }]))
    expect(pyCalls(h2.calls).length).toBe(1)
  })

  test("python nonzero exit: no throw, no retry storm, chat stays healthy", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main", () => ({ err: Object.assign(new Error("exit 1"), { code: 1 }) }))
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "boom" }]))
    expect(pyCalls(h.calls).length).toBe(1)   // 一次调用，不重试轰炸
  })

  test("python spawn failure (e.g. script missing): swallowed, no throw", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main", () => ({ err: Object.assign(new Error("ENOENT"), { code: "ENOENT" }) }))
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "missing script" }]))
    expect(pyCalls(h.calls).length).toBe(1)
  })

  test("session.deleted clears cache: known main sid re-queried after deletion", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "first" }]))
    expect(h.gets.length).toBe(1)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "cached" }]))
    expect(h.gets.length).toBe(1)   // 缓存命中
    await hooks.event({ event: { type: "session.deleted", properties: { info: { id: sid } } } })
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "after delete" }]))
    expect(h.gets.length).toBe(2)   // 缓存已清，重新查询
    expect(pyCalls(h.calls).length).toBe(3)
  })

  test("session.deleted with sessionID-shaped event also clears; unrelated/invalid sids ignored", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "x" }]))
    await hooks.event({ event: { type: "session.deleted", properties: { sessionID: sid } } })
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "y" }]))
    expect(h.gets.length).toBe(2)
    // 无效 sid / 缺 properties：不抛、不影响
    await hooks.event({ event: { type: "session.deleted", properties: { info: { id: "bad id!" } } } })
    await hooks.event({ event: { type: "session.deleted" } })
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } })
  })

  test("output object never mutated by the plugin", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const parts = [{ type: "text", text: "immutable" }]
    const output: any = { message: { id: uid("msg"), role: "user" }, parts }
    const snapshot = JSON.stringify(output)
    await hooks["chat.message"](mkInput(sid), output)
    expect(JSON.stringify(output)).toBe(snapshot)
    expect(output.parts).toBe(parts)
  })

  test("bounded subprocess options: finite timeout and maxBuffer on python child", async () => {
    const sid = uid("ses")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: "bounded" }]))
    for (const c of pyCalls(h.calls)) {
      expect(Number.isFinite(c.opts.timeout)).toBe(true)
      expect(c.opts.timeout).toBeGreaterThan(0)
      expect(c.opts.timeout).toBeLessThanOrEqual(10_000)
      expect(Number.isFinite(c.opts.maxBuffer)).toBe(true)
      expect(c.opts.maxBuffer).toBeLessThanOrEqual(4096)
      expect(c.opts.killSignal).toBe("SIGKILL")
      expect(c.opts.detached).toBeFalsy()      // 非 detached / unref 语义
    }
  })

  test("cold start semantics: unknown session on fresh state queries client exactly once per message until known", async () => {
    // 模拟“重启后缓存为空”：全新 sid、全新 harness
    const sid = uid("ses")
    const h = mkEnv(() => "nodata")
    const hooks = await mkHooks(h)
    for (let i = 0; i < 3; i++) {
      await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: `m${i}` }]))
    }
    expect(h.gets.length).toBe(3)    // 每条都重查（hook 早于会话落库也不永久跳过）
    expect(pyCalls(h.calls).length).toBe(0)
  })

  test("truthy nonboolean synthetic/ignored markers fail closed", async () => {
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(uid("ses")), mkOutput([
      { type: "text", synthetic: "true", text: "excluded" },
      { type: "text", ignored: 1, text: "excluded" },
      { type: "text", synthetic: {}, text: "excluded" },
      { type: "text", synthetic: false, ignored: 0, text: "real" },
    ]))
    expect(pyPayload(pyCalls(h.calls)[0]).text).toBe("real")
  })

  test("session prefix cannot be used as a message ID in either source", async () => {
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "x" }], { id: uid("ses"), role: "user" }))
    await hooks["chat.message"](mkInput(uid("ses"), uid("ses")), { message: { role: "user" }, parts: [{ type: "text", text: "x" }] })
    expect(h.calls).toHaveLength(0)
    expect(h.gets).toHaveLength(0)
    expect(warnings).toHaveBeenCalledTimes(1)
    expect(warnings).toHaveBeenCalledWith("[personal-memory-capture] pmc.mid.invalid")
  })

  test("exact 1 MiB JSON boundary includes metadata overhead", async () => {
    const sid = uid("ses"), mid = uid("msg")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const overhead = Buffer.byteLength(JSON.stringify({ session_id: sid, message_id: mid, text: "" }))
    const text = "x".repeat(1024 * 1024 - overhead)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text }], { id: mid, role: "user" }))
    expect(pyCalls(h.calls)[0].stdinChunks[0].length).toBe(1024 * 1024)
    await hooks["chat.message"](mkInput(sid), mkOutput([{ type: "text", text: text + "x" }], { id: mid, role: "user" }))
    expect(pyCalls(h.calls)).toHaveLength(1)
  })

  test("JSON escaping can exceed event budget even when raw text is below it", async () => {
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    for (const text of ['"'.repeat(530_000), "\u0000".repeat(180_000)]) {
      await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text }]))
    }
    expect(h.calls).toHaveLength(0)
  })

  test("multipart separators count as two JSON bytes at the boundary", async () => {
    const sid = uid("ses"), mid = uid("msg")
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const overhead = Buffer.byteLength(JSON.stringify({ session_id: sid, message_id: mid, text: "" }))
    const text = "x".repeat(1024 * 1024 - overhead - 2)
    await hooks["chat.message"](mkInput(sid), mkOutput([
      { type: "text", text }, { type: "text", text: "y" },
    ], { id: mid }))
    expect(h.calls).toHaveLength(0)
  })

  test("multibyte Unicode and lone surrogates are measured as encoded JSON", async () => {
    const h = mkEnv(() => "main")
    const hooks = await mkHooks(h)
    const text = "记忆😀\ud800".repeat(10_000)
    await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text }]))
    expect(pyPayload(pyCalls(h.calls)[0]).text).toBe(text)
    await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "😀".repeat(270_000) }]))
    expect(pyCalls(h.calls)).toHaveLength(1)
  })

  for (const stdin of ["missing", "error", "throw"] as const) {
    test(`${stdin} stdin kills and waits for completion; exit zero still warns`, async () => {
      const h = mkEnv(() => "main", () => ({ stdin, hold: true }))
      const hooks = await mkHooks(h)
      let resolved = false
      const pending = hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "fake" }]))
        .then(() => { resolved = true })
      for (let n = 0; n < 20 && !pyCalls(h.calls).length; n++) await Promise.resolve()
      const c = pyCalls(h.calls)[0]
      expect(c.kills).toEqual(["SIGKILL"])
      expect(c.completed).toBe(false)
      expect(resolved).toBe(false)
      c.finish() // Even a successful child exit cannot erase the stdin failure.
      await pending
      expect(resolved).toBe(true)
      expect(warnings).toHaveBeenCalledWith("[personal-memory-capture] pmc.capture.failed")
    })
  }

  test("python deadline uses unignorable kill and awaits completion", async () => {
    const h = mkEnv(() => "main", () => ({ hold: true, err: new Error("synthetic timeout") }))
    const hooks = await mkHooks(h)
    let resolved = false
    const pending = hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "fake" }]))
      .then(() => { resolved = true })
    for (let n = 0; n < 20 && !pyCalls(h.calls).length; n++) await Promise.resolve()
    const c = pyCalls(h.calls)[0]
    expect(c.opts.timeout).toBe(5000)
    c.expire() // Model execFile's own deadline; a SIGTERM-ignoring child cannot ignore SIGKILL.
    expect(c.kills).toEqual(["SIGKILL"])
    expect(resolved).toBe(false)
    c.finish()
    await pending
    expect(c.completed).toBe(true)
    expect(warnings).toHaveBeenCalledWith("[personal-memory-capture] pmc.capture.failed")
  })

  for (const [result, code] of [
    [{ ok: true, status: "queued", warning_threshold: 80 }, "pmc.queue.80"],
    [{ ok: true, status: "queued", warning_threshold: 200 }, "pmc.queue.200"],
    [{ ok: false, error: "QUEUE_FULL" }, "pmc.queue.full"],
    [{ ok: false, error: "CANDIDATE_TOO_LARGE" }, "pmc.candidate.oversize"],
  ] as const) {
    test(`allowlisted metadata emits only rate-limited ${code}`, async () => {
      const h = mkEnv(() => "main", () => ({ stdout: JSON.stringify({ ...result, text: "synthetic-secret" }),
        stderr: "synthetic-secret", err: result.ok ? undefined : new Error("synthetic-secret") }))
      const hooks = await mkHooks(h)
      for (let n = 0; n < 2; n++) {
        await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "fake" }]))
      }
      expect(warnings.mock.calls).toEqual([[`[personal-memory-capture] ${code}`]])
    })
  }

  test("warning becomes eligible again after the rate interval", async () => {
    const clock = spyOn(Date, "now").mockReturnValue(100_000)
    try {
      const h = mkEnv(() => "main", () => ({ err: new Error("synthetic-secret") }))
      const hooks = await mkHooks(h)
      for (const time of [100_000, 159_999, 160_000]) {
        clock.mockReturnValue(time)
        await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "fake" }]))
      }
      expect(warnings.mock.calls).toEqual(Array(2).fill(["[personal-memory-capture] pmc.capture.failed"]))
    } finally { clock.mockRestore() }
  })

  test("malformed and unallowlisted output never leaks raw stdout, stderr, or errors", async () => {
    for (const stdout of ["synthetic-secret", "null", "[]", '{"ok":false,"error":"synthetic-secret"}',
      '{"ok":true,"status":"synthetic-secret"}', '{"ok":true,"status":"queued","warning_threshold":"synthetic-secret"}']) {
      const h = mkEnv(() => "main", () => ({ stdout, stderr: "synthetic-secret" }))
      const hooks = await mkHooks(h)
      await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "fake" }]))
    }
    expect(warnings.mock.calls).toEqual(Array(6).fill(["[personal-memory-capture] pmc.capture.failed"]))
  })

  test("synchronous spawn throw is converted to safe failure", async () => {
    const h = mkEnv(() => "main", () => ({ throwSpawn: true }))
    const hooks = await mkHooks(h)
    await hooks["chat.message"](mkInput(uid("ses")), mkOutput([{ type: "text", text: "fake" }]))
    expect(warnings.mock.calls).toEqual([["[personal-memory-capture] pmc.capture.failed"]])
  })
})
