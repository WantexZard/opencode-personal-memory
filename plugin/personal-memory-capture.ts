// Thin capture only: genuine main-session text -> fixed Python helper via stdin.
// No model calls, Memory reads, synchronization, or output.parts mutation.
// Unknown session metadata fails closed and is never cached.
import { execFile } from "node:child_process"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

// Platform-neutral defaults, no machine-specific paths. The Python executable
// name differs by platform (POSIX python3, Windows python) and the helper
// script is resolved relative to this plugin's own location under the standard
// install layout (<root>/plugins/ <-> <root>/skills/personal-memory/capture.py).
// Both are overridable through environment variables for non-standard installs.
const PYTHON = process.env.PERSONAL_MEMORY_PYTHON
  || (process.platform === "win32" ? "python" : "python3")
const SCRIPT = process.env.PERSONAL_MEMORY_SCRIPT
  || join(dirname(fileURLToPath(import.meta.url)), "..", "skills", "personal-memory", "capture.py")
const MAX_EVENT_BYTES = 1024 * 1024
const CHILD_TIMEOUT_MS = 5000
// Shared subprocess/SDK deadline: the Python capture child (execFile timeout)
// and the SDK session lookup (Promise.race) both bound at the same value so a
// hung backend can never hang the chat hook.
const SESSION_GET_TIMEOUT_MS = CHILD_TIMEOUT_MS
// execFile bounds each output stream; raw stdout/stderr never reach diagnostics.
const CHILD_OUT_LIMIT_BYTES = 4096
const WARN_INTERVAL_MS = 60_000

type SessionClass = "main" | "sub" | "unknown"
type CaptureEvent = { session_id: string; message_id: string | null; text: string }
type WarningCode =
  | "pmc.sid.invalid" | "pmc.mid.invalid" | "pmc.session.unknown"
  | "pmc.event.oversize" | "pmc.capture.failed" | "pmc.internal.error"
  | "pmc.queue.80" | "pmc.queue.200" | "pmc.queue.full" | "pmc.candidate.oversize"

function validId(x: unknown, prefix: "ses" | "msg"): x is string {
  return typeof x === "string" && x.length >= 8 && x.length <= 128
    && x.startsWith(`${prefix}_`) && /^(?:ses|msg)_[A-Za-z0-9]+$/.test(x)
}

/** SIGKILL deadlines cannot be ignored like SIGTERM. Await execFile's completion
 * callback (exit/pipe closure), including after stdin errors or missing stdin.
 * No detached child, early resolve on kill, raw exception, or output logging.
 */
async function runChild(
  file: string,
  args: string[],
  exec: typeof execFile,
  payload?: Buffer,
): Promise<{ failed: boolean; stdout: string }> {
  return await new Promise((resolve) => {
    let child: ReturnType<typeof execFile> | undefined
    let inputFailed = false
    const failInput = () => {
      inputFailed = true
      try { child?.kill("SIGKILL") } catch {}
      // Even if kill fails, the execFile deadline remains armed; await completion.
    }
    try {
      child = exec(file, args, {
        timeout: CHILD_TIMEOUT_MS,
        killSignal: "SIGKILL",
        encoding: "utf8",
        maxBuffer: CHILD_OUT_LIMIT_BYTES,
      }, (err, stdout) => {
        resolve({ failed: Boolean(err) || inputFailed, stdout: stdout ?? "" })
      })
    } catch {
      resolve({ failed: true, stdout: "" })
      return
    }
    // Register for both children: a failed stream must not become an unhandled error.
    child.stdin?.on("error", failInput)
    if (payload !== undefined && !child.stdin) {
      failInput()
      return
    }
    try { child.stdin?.end(payload) } catch { failInput() }
  })
}

/** Determine whether a session is a root (main) session or a child (sub) session
 * through the OpenCode SDK client instead of a local database file or sqlite3 CLI.
 * A root session has no parentID; a child session carries one. Any missing client,
 * malformed response, id mismatch, or thrown error degrades to "unknown"
 * (fail-closed): unknown sessions are never captured and never cached, so a
 * transient failure re-queries. A hung backend is bounded by a race deadline so
 * it cannot stall the chat hook.
 */
async function querySessionClass(sid: string, client: any): Promise<SessionClass> {
  try {
    if (!client?.session?.get) return "unknown"
    let res: any
    // A backend that never resolves must not hang the chat hook. Race the lookup
    // against a bounded timer; timeout fails closed to "unknown" (never cached).
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const timeout = new Promise<"__pmc_timeout__">((resolve) => {
        timer = setTimeout(() => resolve("__pmc_timeout__"), SESSION_GET_TIMEOUT_MS)
      })
      const outcome = await Promise.race([client.session.get({ path: { id: sid } }), timeout])
      if (outcome === "__pmc_timeout__") return "unknown"
      res = outcome
    } finally {
      if (timer !== undefined) clearTimeout(timer)
    }
    const data = res?.data ?? res            // tolerate {data} wrapper or direct return
    if (!res || res.error || !data || typeof data !== "object") return "unknown"
    // Fail closed unless the SDK returned the exact session we asked for: a
    // mismatched/absent id must not be misread as a main session.
    if (data.id !== sid) return "unknown"
    if (typeof data.parentID === "string" && data.parentID.length > 0) return "sub"
    if (data.parentID === undefined || data.parentID === null) return "main"
    // parentID present but not a usable string (empty/other): ambiguous -> unknown.
    return "unknown"
  } catch {
    return "unknown"                          // fail-closed: never misclassify as main
  }
}

/** Bound the complete UTF-8 JSON event, including escaped text, metadata and
 * newline separators, before joining parts. Python alone applies matched 64 KiB
 * rejection versus unmatched 16 KiB truncation; the plugin has no 64 KiB gate.
 */
function extractEvent(
  parts: unknown,
  sid: string,
  mid: string | null,
): { ok: true; payload: Buffer } | { ok: false; reason: "empty" | "oversize" } {
  if (!Array.isArray(parts)) return { ok: false, reason: "empty" }
  const event: CaptureEvent = { session_id: sid, message_id: mid, text: "" }
  let total = Buffer.byteLength(JSON.stringify(event), "utf8")
  const texts: string[] = []
  for (const part of parts) {
    if (!part || typeof part !== "object" || part.type !== "text") continue
    if (part.synthetic || part.ignored || typeof part.text !== "string") continue
    if (!part.text.trim()) continue
    // This preliminary bound prevents serializing an arbitrarily large part.
    if (Buffer.byteLength(part.text, "utf8") > MAX_EVENT_BYTES - total) {
      return { ok: false, reason: "oversize" }
    }
    total += Buffer.byteLength(JSON.stringify(part.text), "utf8") - 2
      + (texts.length ? 2 : 0) // JSON encodes the joining newline as \\n.
    if (total > MAX_EVENT_BYTES) return { ok: false, reason: "oversize" }
    texts.push(part.text)
  }
  if (!texts.length) return { ok: false, reason: "empty" }
  event.text = texts.join("\n")
  const payload = Buffer.from(JSON.stringify(event), "utf8")
  return payload.length <= MAX_EVENT_BYTES
    ? { ok: true, payload } : { ok: false, reason: "oversize" }
}

/** Translate only the backend's allowlisted metadata, never interpolate child
 * output, exception messages, IDs, or arbitrary error/status strings into logs.
 */
function captureWarning(result: { failed: boolean; stdout: string }): WarningCode | undefined {
  let data: any
  try { data = JSON.parse(result.stdout) } catch { return "pmc.capture.failed" }
  if (!data || typeof data !== "object" || Array.isArray(data)) return "pmc.capture.failed"
  if (data.ok === false && data.error === "QUEUE_FULL") return "pmc.queue.full"
  if (data.ok === false && data.error === "CANDIDATE_TOO_LARGE") return "pmc.candidate.oversize"
  if (result.failed || data.ok !== true) return "pmc.capture.failed"
  if (data.status === "queued") {
    if (data.warning_threshold === 200) return "pmc.queue.200"
    if (data.warning_threshold === 80) return "pmc.queue.80"
    if (data.warning_threshold !== null) return "pmc.capture.failed"
    return
  }
  if (!["empty", "duplicate", "unmatched"].includes(data.status)) return "pmc.capture.failed"
}

// Exactly ONE runtime export: OpenCode treats every exported function as a plugin.
// OpenCode invokes Plugin(input) with the PluginInput (input.client is the SDK
// client). The synthetic test seam is opts.execFile plus opts.client; production
// falls back to the real execFile and the real SDK client on PluginInput.
export const PersonalMemoryCapturePlugin = async (
  input?: { client?: any },
  opts?: { execFile?: typeof execFile; client?: any },
) => {
  const client = opts?.client ?? input?.client
  const exec = opts?.execFile ?? execFile
  const mainSids = new Set<string>()
  const subSids = new Set<string>()
  const lastWarnAt = new Map<WarningCode, number>()
  const warn = (code: WarningCode) => {
    const now = Date.now()
    const prev = lastWarnAt.get(code)
    if (prev !== undefined && now - prev < WARN_INTERVAL_MS) return
    lastWarnAt.set(code, now)
    try { console.warn(`[personal-memory-capture] ${code}`) } catch {}
  }

  return {
    "chat.message": async (input: any, output: any) => {
      try {
        const sid = input?.sessionID
        if (!validId(sid, "ses")) { warn("pmc.sid.invalid"); return }
        const msg = output?.message
        if (!msg || typeof msg !== "object" || msg.role !== "user") return
        const mid = msg?.id ?? input?.messageID ?? null
        if (mid !== null && !validId(mid, "msg")) { warn("pmc.mid.invalid"); return }
        const extracted = extractEvent(output?.parts, sid, mid)
        if (!extracted.ok) {
          if (extracted.reason === "oversize") warn("pmc.event.oversize")
          return
        }

        const cls = mainSids.has(sid) ? "main" : subSids.has(sid) ? "sub"
          : await querySessionClass(sid, client)
        if (cls === "unknown") { warn("pmc.session.unknown"); return }
        if (cls === "sub") { subSids.add(sid); return }
        mainSids.add(sid)
        const result = await runChild(PYTHON, ["-B", SCRIPT, "--event-json"], exec, extracted.payload)
        const warning = captureWarning(result)
        if (warning) warn(warning)
      } catch {
        warn("pmc.internal.error")
      }
    },

    event: async (input: any) => {
      try {
        const ev = input?.event
        if (ev?.type !== "session.deleted") return
        const sid = ev.properties?.info?.id ?? ev.properties?.sessionID
        if (validId(sid, "ses")) { mainSids.delete(sid); subSids.delete(sid) }
      } catch {}
    },
  }
}
