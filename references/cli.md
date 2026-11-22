# Personal Memory backend CLI

All commands below are implemented by `memory_ops.py`, except capture. The
separate fixed-batch `workflow.py` preview tool is documented in
[workflow.md](workflow.md); it reuses the same lock and never commits changes. Run with
`python3 -B` (on Windows the interpreter is usually `python`). Arguments never
carry message text, proposed content or credentials. The default root is the
directory containing the Python scripts, independent of the working directory.
`memory_ops.py --root /absolute/root COMMAND ...` overrides it; `--root` precedes
the subcommand. All runtime writes share `learning/.memory.lock`, acquired
nonblocking; busy returns exit 2. Other errors return exit 1 and a fixed JSON
error code, without exception text. Success returns exit 0.

On POSIX systems files are created 0600, data directories 0700, and umask is
077; the lock uses `fcntl.flock`. On Windows the same layout is used with
best-effort permissions (POSIX mode bits and `O_NOFOLLOW` are not enforced by the
OS) and the lock uses `msvcrt.locking`. Symlink/special-file rejection and the
version/hash transaction checks apply on every platform.

## Capture (plugin contract)

```sh
python3 -B capture.py --event-json
```

stdin JSON:

```json
{"session_id":"session-example","message_id":"message-example","text":"Remember: synthetic preference"}
```

`message_id` may be null. Stable IDs are the full SHA-256 of the compact UTF-8
JSON encoding of `[session_id,message_id]` including a trailing newline. Missing
message IDs use UUID hex. Plaintext stdin and one positional manual message are
also supported; use stdin for real content. The plugin must filter genuine
main-session user messages before invoking capture. Capture does not query the
OpenCode database, invoke a model, read profile files or synchronize.

Results contain only metadata, e.g. `{"ok":true,"status":"queued","id":"...",
"queue_count":1,"warning_threshold":null}`. Statuses include `empty`, `duplicate`,
`queued`, `unmatched`, `paused`. The same retained event is ignored; a new event with
the same text is allowed. Processed archive IDs remain as metadata after raw expiry.
Unmatched IDs have the same retention as their records, so replay after unmatched
expiry is not remembered. No text-hash semantic deduplication is performed.

### Pausing capture

Capture checks `learning/.paused` before acquiring the lock and again after
acquiring it, returning `{"ok":true,"status":"paused"}` if present. If the first
check sees the marker, it takes no lock, reads no rules and writes no candidate.
If the marker appears while capture waits, the second check stops candidate
processing, although the lock file may already have been touched. A capture that
has passed the second check may still finish: this is not cancellation of an
in-flight event. The marker file is the whole interface; there is no
`pause`/`resume` subcommand, and no
environment variable (a marker works identically on every platform and needs no
restart). Create or remove it with the platform's ordinary file tools. `status`
reports the current state as `capture_paused`. Pausing does not affect existing
memory or any authorization gate, does not stop OpenCode or other recording
tools, and does not replay messages missed while paused.

Rules remain `learning/rules.json`: an object of category names to lists of
keyword/regex strings. The existing regex-character detection is preserved;
invalid regex/config now fails closed with `INVALID_RULE_REGEX`/`INVALID_RULES`.
There is no minimum-length or 4000-character truncation. Matched text over 64 KiB
is rejected, with a safe diagnostic and rejection counter. Queue caps are 10,000
records / 20 MiB encoded JSONL, with reminders at 80 and 200. Rejected candidates
never overflow into unmatched. Unmatched text is capped at 16 KiB UTF-8 including
`[truncated]`; `original_bytes`, `truncated` and the original full text hash are
recorded. Oldest unmatched records are pruned to 30 days / 10,000 / 10 MiB.

## Status and explicitly authorized snapshots

```sh
python3 -B memory_ops.py status
python3 -B memory_ops.py snapshot
python3 -B memory_ops.py snapshot --public --topic ProjectName
python3 -B memory_ops.py snapshot --source unmatched
python3 -B memory_ops.py snapshot --metadata-only
```

Status is metadata only: queue/unmatched counts, pending txid and confirmation
boolean, pending/maintenance failure booleans, `capture_paused`, rejection count.
It never returns pending recovery content, rules, profile or candidate text.
Status takes the same short nonblocking lock for a consistent view.

`maintenance_failed` reflects the existing stored local-maintenance error marker;
a later clean local maintenance clears it. It is not a complete failure history.
A retry of the same last completed txid with no active pending clears residual
pending failure state; an unknown txid or a different active batch does not.

`snapshot` is an explicit Review/Rule Learning read, **not** a Reference command.
It returns `scope`, `records`, `source_sha256`, `redacted_count`, and `files`.
Records have stable `id` and, when eligible, `time`, `text`, `text_sha256`.
Rows with obvious credential patterns return only ID and
`withheld: "POSSIBLE_CREDENTIAL"`; malformed timestamps return only ID and
`withheld: "INVALID_TIMESTAMP"`. This is a heuristic, not guaranteed secret
detection. The caller must not imply that all other text is safe.

`files` always includes the rules hash; only explicitly requested `--public` or
`--topic NAME` files include their hash and content. An absent requested target
has null hash/content. No private file is included or searched. Legacy record IDs
are migrated under lock only by explicit snapshot/prepare, as below. Invalid
JSONL fails closed; it is never silently dropped or interpreted as a JSON array.

### Explicit legacy identity migration

Snapshot normalizes its selected queue/unmatched source and archive locally;
archive content is **never returned**. Prepare normalizes queue/archive and any
selected unmatched source before staging a transaction. All selected sources are
validated before any migration replacement, and pending Review blocks migration.
Cross-file migration is restartable rather than globally atomic: rerunning after
an interrupted replacement converges to the same IDs for known short-ID records.

- Existing canonical 32/64 lowercase-hex IDs remain unchanged.
- Missing IDs receive UUID hex, persisted once. Without old identity evidence,
  no cross-file or old-event equivalence is inferred for missing-ID records.
- Known 16 lowercase-hex IDs with an original nonempty string `time` and `text`
  are converted to a domain-separated SHA-256. The exact input is `encode()` of
  `["personal-memory:legacy16:v1", old_id, exact_time, SHA256(text_utf8)]`, using
  compact UTF-8 JSON and the trailing newline defined above. `legacy_id` preserves
  the original ID; other fields remain intact. No timezone normalization is
  guessed. Archive-only fields such as `archived_at` do not affect this identity.
  Matching queue/archive copies align; different timestamps or text stay distinct
  even when their old truncated hashes match.
- Repeated normalized IDs within one source fail `DUPLICATE_STORED_ID`; no rows
  are silently dropped. Malformed IDs fail `INVALID_IDS`. Short-ID records missing
  time/text fail the new `LEGACY_IDENTITY_REQUIRED`; restore evidence or resolve
  the affected records explicitly rather than inventing timestamps or content.
- Already-expired archive tombstones `{"id":"<16 lowercase hex>","raw_expired":true}`
  stay recognizable legacy metadata unchanged. They are allowed only in archive,
  not as Review selection IDs. Their lost time/text cannot establish equivalence
  with a newly captured event or a full legacy record; no such merge is attempted.
  Newly expired migrated records retain their canonical ID plus `legacy_id`.
- Migrated legacy records and old short tombstones do not prove modern source
  events processed. Capture does not migrate them or block solely on their
  presence; `processed_hashes.json` text-only hashes are not consulted. A new
  source event with identical text remains eligible for capture. Conflicting
  queue/archive contents still fail `RECORD_CONFLICT` at transaction verification.

For an explicitly requested local migration **without model access to candidate
or archive text**, run:

```sh
python3 -B memory_ops.py snapshot --metadata-only
# Only if unmatched migration is also explicitly requested:
python3 -B memory_ops.py snapshot --source unmatched --metadata-only
```

This returns only `ok`, `scope`, `record_count`, and `archive_count`. It rejects
`--public`/`--topic` combinations with `INVALID_SCOPE_ARGUMENT`. It does not create
pending, approve content, or archive candidates. Maintenance refuses unmigrated
full legacy archive rows with `LEGACY_REQUIRES_SNAPSHOT` before expiring their
identity evidence; use this explicit path, then retry maintenance. Existing short
tombstones can pass maintenance without fabricating new identities.

## Prepare → actual user approval → commit

The caller must show the proposed changes and obtain actual user confirmation.
`approval_id` is the real source-message/interaction reference, not a password or
cryptographic authorization. **Never fabricate an approval or treat an unrelated
"continue" as approval.** The helper validates a nonempty reference; the trusted
caller owns correspondence to user intent and scope.

```sh
python3 -B memory_ops.py prepare
```

stdin example (replace IDs/hashes with the actual snapshot metadata):

```json
{
  "processed_ids": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
  "writes": [
    {
      "path": "user-profile/public.md",
      "before_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "content": "# Public\n## Common\nApproved synthetic preference\n"
    }
  ],
  "reviewed_unmatched_ids": []
}
```

An absent new public/project target uses `before_sha256: null` (the key is
required). Writes are complete proposed UTF-8 file content, except the private
field case below. Empty writes can archive explicitly processed candidates.
Preparing returns `txid`, `status: "prepared"`, counts; it does not modify formal
Memory. Exactly one pending transaction is allowed. New capture can continue.

```sh
python3 -B memory_ops.py commit --txid TXID --approval-id USER_CONFIRMATION_REF
python3 -B memory_ops.py recover --txid TXID
```

Commit first durably stores approval reference/time, batch IDs, target before/
after hashes and manifest in `learning/pending-review/transaction.json`, then
verifies versions. Staged bytes and hashes must agree. Formal files are each
atomically replaced with fsync, then the full ID-deduplicated archive is replaced,
then only processed queue IDs are removed. New candidates survive. Reviewed
unmatched IDs are similarly removed, without clearing newly arrived messages.
Completion metadata is persisted in `learning/last-confirmation.json`; pending
body material is removed. Retrying the last completed txid is idempotent.

Recover never confirms a prepared batch. It resumes only a durably confirmed,
matching manifest whose affected files are at the known before or after hashes.
An unknown version or changed batch row halts and retains pending material.
Maintenance reports pending rather than recovering or confirming it. This is
recoverable per-file publication, not an all-files atomic transaction.

After local completion the commit/recover CLI immediately runs local maintenance
and attaches the result as `result["maintenance"]` (e.g.
`{"ok":true,"status":"maintained"}`). If maintenance fails the command returns
exit 1 with `status: "committed"` (or `already_committed`), the txid and
`maintenance_error`; **the local commit is already complete** — retry maintenance
rather than creating another Review. The Python `Memory.commit()` method performs
the local transaction only; its caller owns the subsequent maintenance call.

Whitelisted write paths:

- `user-profile/public.md`
- `user-profile/projects/<NAME>.md` (Unicode letters/numbers, spaces, `_`, `-`)
- `learning/rules.json` (validated with the same capture rule validator)
- `user-profile/private.md` only via the scoped mode below

No arbitrary paths, traversal, symlinks or special files are accepted. Project
names cannot start with a dot or contain `..` or separators.

### Private field replacement

```sh
python3 -B memory_ops.py prepare --allow-private --approved-scope Password
```

The caller must separately obtain explicit private-write approval for that field.
For the private target, `before_sha256` is the whole-file hash returned by the
authorized private Reference, but `content` is **only the replacement ATX section**:

```json
{
  "processed_ids": [],
  "writes": [{
    "path": "user-profile/private.md",
    "before_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "content": "## Password\nSynthetic replacement\n"
  }]
}
```

The helper composes the complete file locally, preserving every byte outside the
approved block. The replacement must contain exactly that one section and end
with a newline when followed by another heading. This mode replaces an existing
field; it does not create a private file or a new field.

### Reviewed unmatched clearing

Prefer `reviewed_unmatched_ids` in the confirmed rules transaction. For a separate
explicit discard:

```sh
python3 -B memory_ops.py clear-unmatched --approval-id USER_REVIEW_REF
```

stdin: `{"reviewed_ids":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}`.
Only existing, explicitly supplied stable IDs are removed. No "clear all" mode.
Legacy unmatched must first be explicitly snapshotted. Pending Review blocks this
separate operation.

## Exact Reference API

```sh
python3 -B memory_ops.py reference public
python3 -B memory_ops.py reference public --section Common
python3 -B memory_ops.py reference topics
python3 -B memory_ops.py reference topic --topic ProjectName --approval-id USER_TOPIC_REF
python3 -B memory_ops.py reference private --field Password --purpose "Current authorized task" --approval-id USER_PRIVATE_REF
```

- `public` reads only `user-profile/public.md`. Caller must have current task/
  session authorization; optional section limits output to one block.
- `topics` lists only project names, not bodies, summaries or private fields.
- `topic` reads only the exact selected `user-profile/projects/NAME.md` after
  user selection/direct authorization. Links never expand scope.
- `private` needs a fresh explicit field, purpose and approval reference on
  every read. Topic/session approval does not grant private access.

Content responses have `scope`, `path`, `section`, `file_sha256`, `content`.
`topics` has `scope: "topic_names"` and `topics` only. There is no generic
search/read-path API and no implicit private enumeration. Inappropriate options
for a scope fail rather than silently broadening it.

Section extraction requires unique conventional ATX headings (`## Title`). The
block ends at the next heading **of any level**; child headings are not included.
Fenced code is handled without interpreting its heading-like lines. Duplicate
headings, unknown fields, setext headings, malformed ATX/HTML headings and
unclosed fences fail closed. No full-file fallback or adjacent-field output.

## Local maintenance

```sh
python3 -B memory_ops.py maintain
```

`maintain` is local-only upkeep; it performs no network copy and reaches nothing
outside the skill directory. Scheduling is optional and left to the host
(`cron`, `launchd`, Windows Task Scheduler, or manual runs). It prunes unmatched
to 30 days / 10,000 records / 10 MiB, removes archived raw content after 90 days
while retaining stable-ID tombstones, and bounds metadata diagnostics to 7 days /
1 MiB. Malformed timestamps are withheld, expired locally, and reported as a fixed
maintenance failure (`INVALID_TIMESTAMP`). Raw archive is `learning/archive.jsonl`;
runtime state is `learning/maintenance.json`; diagnostics are
`learning/diagnostics.jsonl`. Bounding the unmatched log is a privacy measure, not
only a size limit: it prevents an unbounded plaintext record of unmatched chat
text from accumulating on disk.

Unmigrated full legacy archive rows fail `LEGACY_REQUIRES_SNAPSHOT` before their
identity evidence would expire; run the explicit metadata-only snapshot migration
above, then retry. Existing short tombstones pass maintenance unchanged.

## Initialization workflow (dormant playbook, no new CLI)

The reusable initialization workflow for capture rules is a dormant,
documentation-driven playbook: it carries no frontmatter trigger, stays out of
the always-resident SKILL body, and costs zero context until the user explicitly
asks to initialize capture rules. See `SKILL.md` (initialization playbook section)
and `playbooks/initialization.md` with the synthetic sample
`playbooks/initialization.sample.json`. No initialization CLI subcommand is
invented: the existing `memory_ops.py` interface (prepare → approval → commit)
applies unchanged for a pure rules transaction (`processed_ids: []`,
`reviewed_unmatched_ids: []`, writes only `learning/rules.json`).

## Validation and operational boundaries

```sh
python3 -B tests/test_memory_ops.py
python3 -B -m pytest tests/test_memory_ops.py -q   # if pytest is installed
bun test ./tests/test_capture_plugin.ts ./tests/test_acceptance.ts
```

Unit fixtures are created only under a temporary directory; no live Memory data
is required. The plugin tests inject a fake OpenCode SDK client and a fake
`execFile`, so they never touch the real database or spawn the real interpreter
against live data. The backend does not manufacture user authorization and does
not provide adversarial root-process isolation. Hand edits during review are
detected by version/hash checks; arbitrary concurrent edits by another process
cannot be made atomic with this trusted-caller file protocol. Unknown pending
conflicts require operator resolution; there is no automatic discard or rebase
command.
