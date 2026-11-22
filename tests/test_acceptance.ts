// Independent acceptance: real plugin (injected SDK client) -> Python capture;
// CLI transactions on synthetic fixtures under /tmp/opencode. The plugin spawns
// the real capture.py; memory_ops.py runs as a real subprocess. No network copy.
// Approval references below simulate confirmation ONLY inside these fixtures.
import { afterEach, describe, expect, test } from "bun:test"
import { execFile } from "node:child_process"
import { createHash } from "node:crypto"
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync,
  rmSync, symlinkSync, unlinkSync, writeFileSync,
} from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { PersonalMemoryCapturePlugin } from "../plugin/personal-memory-capture"

const HERE = dirname(fileURLToPath(import.meta.url))
const CODE = resolve(HERE, "..")
const PYTHON = process.env.PERSONAL_MEMORY_PYTHON
  || (process.platform === "win32" ? "python" : "python3")
const MAIN = "ses_acceptancemain"
const CHILD = "ses_acceptancechild"
const QUEUE = "learning/learning_queue.jsonl"
const ARCHIVE = "learning/archive.jsonl"
const UNMATCHED = "learning/unmatched.jsonl"
const PUBLIC = "user-profile/public.md"
const PRIVATE = "user-profile/private.md"
const RULES = "learning/rules.json"
const A = "FAKE-ACCEPTANCE-FIELD-A-ONLY"
const B = "FAKE-ACCEPTANCE-NEIGHBOR-B-ONLY"
const ALPHA = "SYNTHETIC-ALPHA-BODY"
const BETA = "SYNTHETIC-BETA-NOT-SELECTED"
const PUBLIC_TEXT = "# Public\n## Common\nfixture-public\n## Keep\nPreserve this unrelated block.\n"
const owned = new Set<string>()
const hash = (data: string | Buffer) => createHash("sha256").update(data).digest("hex")
const encoded = (value: unknown) => JSON.stringify(value) + "\n"

type Result = { code: number; stdout: string; stderr: string }
function execute(file: string, args: string[], input = ""): Promise<Result> {
  return new Promise((done) => {
    const child = execFile(file, args, {
      encoding: "utf8", timeout: 15_000, killSignal: "SIGKILL", maxBuffer: 2 * 1024 * 1024,
    }, (err, stdout, stderr) => {
      done({ code: err ? (typeof err.code === "number" ? err.code : -1) : 0, stdout, stderr })
    })
    child.stdin?.on("error", () => child.kill("SIGKILL"))
    child.stdin?.end(input)
  })
}

function success(result: Result): any {
  // Never include response bodies in assertion failures: they may contain fake private fields.
  expect(result.code).toBe(0)
  expect(result.stderr.length).toBe(0)
  const data = JSON.parse(result.stdout)
  expect(data.ok).toBe(true)
  return data
}

function failure(result: Result, code: string) {
  expect(result.code).toBe(1)
  expect(result.stderr.length).toBe(0)
  const data = JSON.parse(result.stdout)
  expect(data.ok).toBe(false)
  expect(data.error).toBe(code)
  expect(result.stdout.includes(A) || result.stdout.includes(B)).toBe(false)
}

function fixture() {
  const base = mkdtempSync("/tmp/opencode/pm-acceptance-")
  owned.add(base)
  chmodSync(base, 0o700)
  const root = join(base, "memory")
  mkdirSync(root, { mode: 0o700 })
  const guard = (path: string) => {
    if (!resolve(path).startsWith(base + "/")) throw new Error("FIXTURE_PATH_ESCAPE")
  }
  const put = (relative: string, data: string | Buffer) => {
    const path = join(root, relative)
    guard(path)
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
    writeFileSync(path, data, { mode: 0o600 })
    chmodSync(path, 0o600)
  }
  const bytes = (relative: string) => readFileSync(join(root, relative))
  const rows = (relative: string): any[] => existsSync(join(root, relative))
    ? bytes(relative).toString().split("\n").filter(Boolean).map((line) => JSON.parse(line)) : []
  const cli = (args: string[], payload?: unknown) => {
    guard(root)
    // No NAS/mount arguments: memory_ops is local-only and takes --root plus a subcommand.
    return execute(PYTHON, ["-B", join(CODE, "memory_ops.py"), "--root", root, ...args],
      payload === undefined ? "" : encoded(payload))
  }
  const capture = (message: string, text = "Remember: independent fixture") => {
    guard(root)
    return execute(PYTHON, ["-B", join(CODE, "capture.py"), "--root", root, "--event-json"],
      encoded({ session_id: MAIN, message_id: message, text }))
  }
  put(RULES, encoded({ Remember: ["Remember:", "记一下"] }))
  put(PUBLIC, PUBLIC_TEXT)
  put(PRIVATE, `# Private\n## FieldA\n${A}\n## FieldB\n${B}\n`)
  put("user-profile/projects/Alpha.md", `# Alpha\n${ALPHA}\n[not an authorization](../private.md)\n`)
  put("user-profile/projects/Beta.md", `# Beta\n${BETA}\n`)
  put("user-profile/not-published.txt", "synthetic unrelated local file\n")
  return { base, root, put, bytes, rows, cli, capture }
}
type Fixture = ReturnType<typeof fixture>

afterEach(() => {
  for (const base of owned) {
    if (!/^\/tmp\/opencode\/pm-acceptance-[A-Za-z0-9]+$/.test(base)) {
      throw new Error("UNSAFE_FIXTURE_CLEANUP")
    }
    rmSync(base, { recursive: true, force: true })
    expect(existsSync(base)).toBe(false)
    owned.delete(base)
  }
})

async function preparePublic(f: Fixture, ids: string[], content: string) {
  const snap = success(await f.cli(["snapshot", "--public"]))
  expect(Object.keys(snap.files).sort()).toEqual([RULES, PUBLIC].sort())
  expect(snap.files[PUBLIC].sha256).toBe(hash(f.bytes(PUBLIC)))
  const tx = success(await f.cli(["prepare"], {
    processed_ids: ids,
    writes: [{ path: PUBLIC, before_sha256: snap.files[PUBLIC].sha256, content }],
    reviewed_unmatched_ids: [],
  }))
  expect(tx.status).toBe("prepared")
  return tx.txid as string
}

describe("independent acceptance: real plugin (injected client) + CLI on synthetic fixtures", () => {
  test("B: plugin filters sources and preserves event identity through injected client and Python", async () => {
    const f = fixture()
    const seen: string[] = []
    let captures = 0
    const statuses: string[] = []
    const unexpected: string[] = []
    const route = ((file: string, args: string[], opts: any, callback: any) => {
      if (file === PYTHON && args[0] === "-B" && args[2] === "--event-json"
        && String(args[1]).endsWith("capture.py")) {
        captures += 1
        return execFile(PYTHON, ["-B", join(CODE, "capture.py"), "--root", f.root, "--event-json"], opts,
          (err, stdout, stderr) => {
            if (!err) statuses.push(JSON.parse(String(stdout)).status)
            callback(err, stdout, stderr)
          })
      }
      unexpected.push("UNEXPECTED_PLUGIN_EXEC")
      throw new Error("UNEXPECTED_PLUGIN_EXEC")
    }) as typeof execFile
    // Root session has no parentID; child session points at MAIN as its parent.
    const client = {
      session: {
        get: async ({ path: { id } }: { path: { id: string } }) => {
          seen.push(id)
          return { data: { id, parentID: id === CHILD ? MAIN : undefined } }
        },
      },
    }
    const hook = (await PersonalMemoryCapturePlugin({}, { execFile: route, client }))["chat.message"]
    const send = async (sid: string, mid: string, parts: any[]) => {
      const output = { message: { id: mid, role: "user" }, parts }
      const before = encoded(output)
      await hook({ sessionID: sid }, output)
      expect(encoded(output) === before).toBe(true)
    }
    const text = "Remember: real subprocess chain"
    await send(MAIN, "msg_acceptance001", [{ type: "text", text }])
    await send(CHILD, "msg_acceptance002", [{ type: "text", text }])
    await send(MAIN, "msg_acceptance003", [{ type: "text", text, synthetic: true }])
    await send(MAIN, "msg_acceptance004", [{ type: "text", text, ignored: true }])
    await send(MAIN, "msg_acceptance001", [{ type: "text", text }])
    await send(MAIN, "msg_acceptance005", [{ type: "text", text }])
    const long = "u".repeat(70 * 1024)
    await send(MAIN, "msg_acceptance006", [{ type: "text", text: long }])
    await send(MAIN, "msg_acceptance007", [
      { type: "text", text: "记一下：only genuine" },
      { type: "text", text: "excluded synthetic", synthetic: true },
      { type: "text", text: "excluded ignored", ignored: true },
    ])
    expect(unexpected).toEqual([])
    // One metadata query for MAIN and one for CHILD; cached conclusions skip re-query.
    expect(seen).toEqual([MAIN, CHILD])
    expect(captures).toBe(5)
    expect(statuses).toEqual(["queued", "duplicate", "queued", "unmatched", "queued"])
    const queue = f.rows(QUEUE)
    expect(queue.length).toBe(3)
    expect(queue.map((row) => row.id)).toEqual(["msg_acceptance001", "msg_acceptance005", "msg_acceptance007"]
      .map((mid) => hash(encoded([MAIN, mid]))))
    expect(queue.map((row) => row.text)).toEqual([text, text, "记一下：only genuine"])
    const unmatched = f.rows(UNMATCHED)
    expect(unmatched.length).toBe(1)
    expect(unmatched[0].original_bytes).toBe(70 * 1024)
    expect(unmatched[0].truncated).toBe(true)
    expect(Buffer.byteLength(unmatched[0].text)).toBe(16 * 1024)
    expect(unmatched[0].text.endsWith("[truncated]")).toBe(true)
    expect(unmatched[0].text_sha256).toBe(hash(long))
  }, 30_000)

  test("C: snapshot/prepare/confirmation/commit, retry idempotent, unrelated file preserved", async () => {
    const f = fixture()
    const first = success(await f.capture("msg_commitfirst"))
    success(await f.capture("msg_unmatchedfirst", "unmatched fixture"))
    const unrelated = hash(f.bytes("user-profile/not-published.txt"))
    const original = hash(f.bytes(PUBLIC))
    const next = PUBLIC_TEXT.replace("fixture-public", "approved synthetic preference")
    const txid = await preparePublic(f, [first.id], next)
    expect(hash(f.bytes(PUBLIC))).toBe(original)
    expect(f.rows(ARCHIVE).length).toBe(0)
    failure(await f.cli(["recover", "--txid", txid]), "UNCONFIRMED_REVIEW")
    expect(hash(f.bytes(PUBLIC))).toBe(original)
    expect(success(await f.cli(["status"])).pending.confirmed).toBe(false)
    const arrival = success(await f.capture("msg_duringpending", "Remember: arrived during review"))
    const committed = success(await f.cli(["commit", "--txid", txid, "--approval-id", "fixture_confirmation_ref"]))
    expect(committed.status).toBe("committed")
    expect(committed.maintenance.status).toBe("maintained")
    expect(hash(f.bytes(PUBLIC))).toBe(hash(next))
    expect(f.rows(QUEUE).map((row) => row.id)).toEqual([arrival.id])
    expect(f.rows(ARCHIVE).map((row) => row.id)).toEqual([first.id])
    expect(f.rows(UNMATCHED).length).toBe(1)
    expect(success(await f.cli(["status"])).pending).toBeNull()
    expect(hash(f.bytes("user-profile/not-published.txt"))).toBe(unrelated)
    const archiveBefore = hash(f.bytes(ARCHIVE))
    const retry = success(await f.cli(["commit", "--txid", txid, "--approval-id", "fixture_confirmation_ref"]))
    expect(retry.status).toBe("already_committed")
    expect(retry.maintenance.status).toBe("maintained")
    expect(hash(f.bytes(ARCHIVE))).toBe(archiveBefore)
  }, 30_000)

  test("D: Reference public/topics/selected topic do not expand into private or another topic", async () => {
    const f = fixture()
    const publicResult = await f.cli(["reference", "public"])
    expect(hash(success(publicResult).content)).toBe(hash(PUBLIC_TEXT))
    expect([A, B, ALPHA, BETA].some((value) => publicResult.stdout.includes(value))).toBe(false)
    const common = success(await f.cli(["reference", "public", "--section", "Common"]))
    expect(hash(common.content)).toBe(hash("## Common\nfixture-public\n"))
    const topics = await f.cli(["reference", "topics"])
    expect(success(topics)).toEqual({ ok: true, scope: "topic_names", topics: ["Alpha", "Beta"] })
    expect([A, B, ALPHA, BETA].some((value) => topics.stdout.includes(value))).toBe(false)
    failure(await f.cli(["reference", "topic", "--topic", "Alpha"]), "APPROVAL_REQUIRED")
    const selected = await f.cli(["reference", "topic", "--topic", "Alpha", "--approval-id", "fixture_topic_ref"])
    expect(hash(success(selected).content)).toBe(hash(f.bytes("user-profile/projects/Alpha.md")))
    expect([A, B, BETA].some((value) => selected.stdout.includes(value))).toBe(false)
    failure(await f.cli(["reference", "public", "--field", "FieldA"]), "INVALID_SCOPE_ARGUMENT")
  })

  test("D: private requires approval on each call and returns only the selected field", async () => {
    const f = fixture()
    const args = ["reference", "private", "--field", "FieldA", "--purpose", "fixture verification"]
    failure(await f.cli(args), "APPROVAL_REQUIRED")
    const response = await f.cli([...args, "--approval-id", "fixture_private_ref"])
    const field = success(response)
    expect(hash(field.content)).toBe(hash(`## FieldA\n${A}\n`))
    expect(field.file_sha256).toBe(hash(f.bytes(PRIVATE)))
    expect(response.stdout.includes(B)).toBe(false)
    failure(await f.cli(args), "APPROVAL_REQUIRED")
    failure(await f.cli(["reference", "private", "--field", "Unknown", "--purpose", "fixture verification", "--approval-id", "fixture_private_ref"]), "UNKNOWN_FIELD")
    failure(await f.cli(["reference", "private", "--field", "FieldA", "--approval-id", "fixture_private_ref"]), "PURPOSE_REQUIRED")
  })

  for (const [name, content] of [
    ["duplicate", `# Private\n## FieldA\n${A}\n## FieldA\n${B}\n`],
    ["setext", `# Private\n## FieldA\n${A}\nFieldB\n---\n${B}\n`],
    ["malformed ATX", `# Private\n## FieldA\n${A}\n##FieldB\n${B}\n`],
    ["HTML", `# Private\n## FieldA\n${A}\n<h2>FieldB</h2>\n${B}\n`],
    ["unclosed fence", `# Private\n## FieldA\n${A}\n\x60\x60\x60\n## FieldB\n${B}\n`],
  ]) {
    test(`D: ${name} heading ambiguity fails closed without neighboring content`, async () => {
      const f = fixture()
      f.put(PRIVATE, content)
      failure(await f.cli(["reference", "private", "--field", "FieldA", "--purpose", "fixture verification", "--approval-id", "fixture_private_ref"]), "AMBIGUOUS_MARKDOWN")
    })
  }

  test("D: symlink Reference targets are rejected without following them", async () => {
    const f = fixture()
    const outside = join(f.base, "synthetic-target.txt")
    writeFileSync(outside, `${A}\n${B}\n`, { mode: 0o600 })
    const original = hash(readFileSync(outside))
    unlinkSync(join(f.root, PRIVATE))
    symlinkSync(outside, join(f.root, PRIVATE))
    failure(await f.cli(["reference", "private", "--field", "FieldA", "--purpose", "fixture verification", "--approval-id", "fixture_private_ref"]), "UNSAFE_PATH")
    unlinkSync(join(f.root, "user-profile/projects/Alpha.md"))
    symlinkSync(outside, join(f.root, "user-profile/projects/Alpha.md"))
    failure(await f.cli(["reference", "topic", "--topic", "Alpha", "--approval-id", "fixture_topic_ref"]), "UNSAFE_PATH")
    expect(hash(readFileSync(outside))).toBe(original)
  })

  test("D: changed source version stops commit/recover and retains queued work", async () => {
    const f = fixture()
    const item = success(await f.capture("msg_versionconflict"))
    const txid = await preparePublic(f, [item.id], PUBLIC_TEXT + "staged change\n")
    const queueBefore = hash(f.bytes(QUEUE))
    f.put(PUBLIC, PUBLIC_TEXT + "independent concurrent change\n")
    const newer = hash(f.bytes(PUBLIC))
    failure(await f.cli(["commit", "--txid", txid, "--approval-id", "fixture_confirmation_ref"]), "VERSION_CONFLICT")
    expect(hash(f.bytes(PUBLIC))).toBe(newer)
    expect(hash(f.bytes(QUEUE))).toBe(queueBefore)
    expect(f.rows(ARCHIVE).length).toBe(0)
    expect(success(await f.cli(["status"])).pending.confirmed).toBe(true)
    failure(await f.cli(["recover", "--txid", txid]), "VERSION_CONFLICT")
    expect(hash(f.bytes(PUBLIC))).toBe(newer)
    expect(hash(f.bytes(QUEUE))).toBe(queueBefore)
  })

  test("D: legacy16 metadata-only migration aligns identities; commit retry does not dedup new text events", async () => {
    const f = fixture()
    const old = "a".repeat(16)
    const tombstone = { id: "b".repeat(16), raw_expired: true }
    const text = "Remember: legacy synthetic preference"
    const firstTime = new Date(Date.now() - 2000).toISOString()
    const secondTime = new Date(Date.now() - 1000).toISOString()
    const first = { id: old, time: firstTime, text }
    const second = { id: old, time: secondTime, text }
    f.put(QUEUE, encoded(first) + encoded(second))
    f.put(ARCHIVE, encoded({ ...first, archived_at: new Date().toISOString() }) + encoded(tombstone))
    const metadata = await f.cli(["snapshot", "--metadata-only"])
    expect(success(metadata)).toEqual({ ok: true, scope: "queue", record_count: 2, archive_count: 2 })
    expect([text, A, B, firstTime, secondTime, old].some((value) => metadata.stdout.includes(value))).toBe(false)
    const firstId = hash(encoded(["personal-memory:legacy16:v1", old, firstTime, hash(text)]))
    const secondId = hash(encoded(["personal-memory:legacy16:v1", old, secondTime, hash(text)]))
    expect(firstId === secondId).toBe(false)
    expect(f.rows(QUEUE).map((row) => row.id)).toEqual([firstId, secondId])
    expect(f.rows(ARCHIVE).map((row) => row.id)).toEqual([firstId, tombstone.id])
    expect(f.rows(QUEUE).every((row) => row.legacy_id === old)).toBe(true)
    expect(f.rows(ARCHIVE)[1]).toEqual(tombstone)
    const migrated = [hash(f.bytes(QUEUE)), hash(f.bytes(ARCHIVE))]
    success(await f.cli(["snapshot", "--metadata-only"]))
    expect([hash(f.bytes(QUEUE)), hash(f.bytes(ARCHIVE))]).toEqual(migrated)
    failure(await f.cli(["snapshot", "--metadata-only", "--public"]), "INVALID_SCOPE_ARGUMENT")
    const prepared = success(await f.cli(["prepare"], {
      processed_ids: [firstId], writes: [], reviewed_unmatched_ids: [],
    }))
    const commitArgs = ["commit", "--txid", prepared.txid, "--approval-id", "fixture_legacy_confirmation_ref"]
    expect(success(await f.cli(commitArgs)).status).toBe("committed")
    expect(f.rows(QUEUE).map((row) => row.id)).toEqual([secondId])
    expect(f.rows(ARCHIVE).filter((row) => row.id === firstId).length).toBe(1)
    const archiveAfter = hash(f.bytes(ARCHIVE))
    expect(success(await f.cli(commitArgs)).status).toBe("already_committed")
    expect(hash(f.bytes(ARCHIVE))).toBe(archiveAfter)
    const fresh = success(await f.capture("msg_newidenticallegacy", text))
    expect(fresh.status).toBe("queued")
    expect(fresh.id).toBe(hash(encoded([MAIN, "msg_newidenticallegacy"])))
    expect(f.rows(QUEUE).map((row) => row.id)).toEqual([secondId, fresh.id])
    expect(success(await f.capture("msg_newidenticallegacy", text)).status).toBe("duplicate")
    expect(f.rows(QUEUE).length).toBe(2)
  }, 30_000)
})
