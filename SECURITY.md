# Security and Privacy

Personal Memory stores your personal preferences and habits, so it is essential to understand its security model — **it is a privacy boundary, not encryption or operating-system-level isolation.**

## Where data lives

Everything is local, under the skill directory:

- `user-profile/public.md`, `private.md`, `projects/*.md`: formal memory (plaintext).
- `learning/`: candidate queue, miss records, archive, rules, runtime state (plaintext), plus the current review worklists under `review-worksets/`.

The batched tool's worklist stores only IDs, versions, categories and rule comparison results; it does not store separate candidate bodies or draft rules. It is still private operational material; the hashes in it are not encryption and do not mean it can be published. Worklists are not cleaned up automatically and must not enter the repository either.

The project itself initiates no network transfer: no cloud, no remote server, no offsite sync, and no telemetry reporting.

When you authorize a review, rule-learning analysis or reference, the permitted content enters the current OpenCode conversation and follows your configured model-provider settings. The project does not control that request path. A vague request to "reference memory" still requires scope clarification before reading.

**But be clear about one thing**: once content has been sent to the model provider with the conversation, the project cannot take it back. "Cancel reference" only stops further reads; **it cannot erase content that has already entered the model context, the OpenCode local database, or provider logs**. Do not put anything here that is too sensitive to risk leaking — even in private.

## Security model (stated honestly)

- **File permissions**: on POSIX, files are created as `0600` and directories as `0700`, with umask 077, and symbolic links/special files are refused. Windows keeps the same layout, but permission bits are best-effort (decided by the system, not enforced).
- **This is not isolation**: `0600/0700` lowers the chance that other ordinary users on the same machine read files by accident, but **it cannot stop a host administrator / root / anyone with disk access** from reading them. The content is plaintext; any subject that can reach that directory can see it.
- **It is not encryption**: the project encrypts no files. Do not treat it as a safe for passwords or keys.
- **Trusted-executor boundary**: the backend assumes it is called by a trusted local executor (your OpenCode main model). It provides no adversarial root-process isolation and creates no cryptographic authorization. An approval reference is only a record that "the user confirmed", not an unforgeable credential.

## Authorization model

- **Capture**: only text in the main session explicitly marked `role: "user"` is processed; sub-sessions, synthetic messages, and missing/unknown sources fail closed and are not captured.
- **Reference (read back to the model)**: off by default; reads only the exact scope you explicitly authorize. A vague "reference memory" first asks for the scope.
- **Batched review**: queue and unmatched are authorized separately; seeing statistics alone does not permit outputting candidate bodies. A workset ID does not carry read permission into a new session; a rule preview also does not equal approval to write or clean up. Screening is heuristic: it can miss or misjudge, so withheld/kept status is not proof that the remaining bodies are safe.
- **private**: approved separately every time, per field, per purpose. Project authorization and session mounting never read private as a side effect.
- **Writes**: agent-driven memory and rule changes go through a staging → you approve → commit transaction with a version-hash check. Initial setup only copies missing starter files; it does not approve personal facts.

## What you should do

- **Do not commit `learning/` and `user-profile/` into any repository.** This repo's `.gitignore` ignores them by default; do not override that.
- **Do not paste real memory content or secrets into issues / PRs / logs.**
- If several accounts share a machine and you care about privacy, understand the previous section: file permissions are not enough against administrator-level access.
- Periodically use `maintain` to prune expired miss records (30 days by default) and reduce the plaintext chat fragments piling up on disk.

## Reporting security issues

If you find a problem that could cause **unauthorized reads, bypassing authorization, or leaking data outside the local directory**, contact the maintainers privately instead of opening a public issue first. Describe the impact and reproduction steps, and **do not attach your real data**.
