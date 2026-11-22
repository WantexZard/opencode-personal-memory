# Personal Memory initialization workflow (first rule learning)

This reference covers **initializing capture rules and the usage workflow**, not reinstalling/resetting data or auto-filling a profile. See [initialization.sample.json](initialization.sample.json) for a synthetic illustration, not runtime input. If the user says "initialize Personal Memory" (「初始化 Personal Memory」), clarify whether they mean file setup, rule learning or importing facts before opening data.

## 1. Goal and boundaries

- scope = initializing capture rules and the usage workflow. Read the fixed `learning/rules.json` only when rule initialization is actually authorized; do not assume it is empty, and do not overwrite existing default rules (see `examples/rules.json` for the sample template).
- A generic "initialize" or a reference to a trigger example authorizes no reading; all ordinary Review/Reference boundaries remain in force.
- This process differs from ordinary rule learning's batched unmatched reading: initialization reads no queue/unmatched/archive/profile or historical data by default.

## 2. Choose the current route

- **Manual seed route (recommended for small samples)**: explicitly state that the evidence is limited (small sample, no holdout validation), and use it after obtaining the current user's consent.
- **Strict data-driven route**: each new rule needs support from at least 2 different sessions and 4 high-value messages; validation-set precision ≥80%; at most 8 diffs.
- The route choice serves only the current exact task; this choice does not become a default permission for future requests or other scenarios.

## 3. Source options and authorization

- Source: examples the user provides now, or the current context already authorized.
- Bodies require explicit authorization for this request before entering the model; the source and purpose are reconfirmed on every request, never guessed from a title/suffix, and different IDs with identical text are treated as copied content.
- Do not read public/private/projects for comparison without separate permission.

## 4. Mark value dimensions first

- Dimensions: principle/architecture attitude, organization/role division, project goal/constraint/tradeoff, interaction and learning preference, background/device.
- Distinguish reusable preferences, time-bounded project decisions, one-off commands, and quoted third-party content. Conceptual records keep condition/negation/scope; the capture stage does no per-message LLM call.
- Derive cues/context patterns from positive **and negative** examples, not simple word counts. Start with keywords, then short bounded regexes. Bare "I think / don't / subjective topic words" do not automatically become lifelong facts — rules only produce candidates, and Review decides facts.

## 5. Data discipline

- Do not impose a 20-item per-source sampling cap on an otherwise authorized corpus; keep the source scope the user actually approved.
- Strict mode: after cross-source/copy deduplication, split deterministically 70/30 before discovery; freeze candidates before using the holdout; do not tune parameters and then reuse the holdout as unseen data.
- When there is too little data, report "undefined/weak evidence" and do not fabricate a precision figure.
- Manual seed mode may use small synthetic samples for behavior tests, but mark real-world coverage as **unvalidated**; do not report synthetic passes as a real 80% accuracy. Do not expand data indefinitely; only ask about a meaningful new scope, or let the user switch to manual seed.

## 6. Review and output

- Fixed five groups Add/Update/Remove/Conflict/Skip, ≤8 diffs, preserving existing rules; show false positives/false negatives/support counts/uncertainty and duplication/coverage limits; require the user's approval of the exact diff.
- Examples are not approved rules or authorization.
- Rule initialization alone **does not need a Learning batch**: authorized bodies are analyzed in memory only, keeping metadata and a generic rule draft. If the user separately wants a corpus batch, that is another independent gate; the initializer never writes queue/unmatched/archive.

## 7. prepare → commit

- Only through the actual `memory_ops.py`: after the user accepts the final rule proposal and record handling, run `prepare`; a pure rules transaction uses `processed_ids=[]`, `reviewed_unmatched_ids=[]`, writes only `learning/rules.json`, a new `before_sha256`, and a complete UTF-8 write.
- Do not use clear-unmatched, do not write private, and do not forge approval. prepare is only pending; after showing the real txid and the actual diff, a **new user confirmation** is required to `commit --txid REAL --approval-id ACTUAL`.
- These are not Git commits or model training. `PENDING_REVIEW`/`VERSION_CONFLICT` preserve state; there is no automatic cancel/rebase. After commit, use the existing local `maintain` (runbook in SKILL/CLI, not repeated here).

## 8. Completion and follow-up

- Initialization is complete when the user-approved rules have committed successfully, or when it deliberately stops at a reviewed DRAFT (reporting partial completion honestly).
- Later learning is triggered only by the user (mis-capture/missed capture or an explicit scope); no periodic automatic LLM, and no automatic opening of historical data. Reuse the real helper.
