# Contributing

Thanks for considering a contribution to Personal Memory. This is a deliberately lightweight project aimed at individuals; read its boundaries before contributing.

## Project positioning (read before contributing)

Personal Memory is built around three things: **local privacy**, **lightweight efficiency**, and **controllable authorization**. Contributions must respect all three, or even a useful feature does not fit this project:

- **Local-first, single-user.** The project stores data locally and adds no cloud sync or memory service. Authorized review/reference still sends permitted content through the user's configured OpenCode model provider. Multi-user or hosted-memory designs are outside the current scope.
- **Lightweight and efficient.** Capture and cleanup use local rules and no model calls. The skill instructions still occupy context, and review/reference use tokens. Changes that introduce model calls into capture or noticeably raise routine cost are not accepted by default.
- **Controllable authorization.** Do not relax existing security semantics such as per-field private authorization, "capture only user messages in the main session", or fail-closed behavior.

## Environment

- OpenCode v1 (tested during development on v1.18.32)
- Python 3 (standard library, no third-party dependencies; development baseline: Python 3.12)
- Bun (for plugin and end-to-end tests)

## Development workflow

1. Fork and create a branch.
2. After changing code, **get all tests passing** (see below), and add tests for new features.
3. Write commit messages that state what changed and why.
4. Open a PR describing the motivation, the scope of impact, and how you verified it.

## Tests

```sh
python3 -B tests/test_memory_ops.py          # Python backend
python3 -B tests/test_workflow.py            # batched preview and rule comparison
python3 -B tests/test_rule_seed_candidates.py # synthetic rule examples
bun test ./tests/test_capture_plugin.ts      # plugin unit tests
bun test ./tests/test_acceptance.ts          # end-to-end
```

All tests use synthetic fixtures in temporary directories and never read or write your real memory. **PRs must be fully green.**

## Code conventions

- Python uses the standard library only; branch for cross-platform behavior (POSIX vs Windows) with `os.name`, and do not add third-party lock or permission libraries.
- The plugin goes through the OpenCode SDK (`client.session.get(...)`); do not hardcode database paths or rely on the system `sqlite3` CLI.
- For changes that write files, keep the staging → user approval → commit transaction and the version-hash check.

## Changes we do not accept

- Bypassing authorization to read private data or the candidate queue directly.
- Calling a model on the capture path.
- Committing real personal data (`learning/`, `user-profile/`) into the repository.
- Claiming local plaintext is "encrypted" or "sandboxed".

## Reporting issues

When filing an issue, include: OpenCode version, operating system, reproduction steps, and expected vs. actual behavior. **Never paste your real memory content or any secrets into an issue.**
