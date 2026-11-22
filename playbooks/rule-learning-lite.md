# Lightweight rule-learning playbook (learn from a conversation/queue, or add capture rules)

Dormant playbook: it has no frontmatter trigger, is not resident in the SKILL body, and costs zero context normally. Read this file and follow its steps only when the user in the current session **explicitly asks** for something like "learn rules from this conversation / add capture rules / add a rule manually / learn rules" (「从这段对话学规则 / 新增捕获规则 / 手动加规则 / 学习规则」), etc. A generic "learn", "continue" or "organize" (「学习」「继续」「整理」), or repeating this pointer, authorizes no reading.

This process is a low-frequency, reusable fixed recipe for **manual seed + lightweight data-driven** rule addition/adjustment, distinct from:

- the first systematic rule build → `playbooks/initialization.md`;
- large-batch unmatched rule learning → the `workflow.py open/page` in the SKILL "rule learning" section.

## 1. Route and authorization

- **Manual seed (small samples)**: explicitly declare that the evidence is limited (small sample, no holdout validation), and use it after obtaining consent for this request.
- **Lightweight data-driven**: each new rule needs support from at least 2 different sessions and 4 high-value messages; precision ≥80% when a validation set exists; at most 8 diffs.
- The route choice serves only the current task and does not become a default permission for the future. Bodies require explicit approval for this request before entering the model; approving an implementation plan ≠ approving content/rules.

## 2. Local sampling (counts only, no bodies to the model yet)

- Use `workflow.py open --source queue|unmatched --approval-id USER_SOURCE_REF` to open only the authorized source; look at the open/stats counts first and do not call page prematurely. Historical material is outside this tool's read scope.
- The tool reports statistics by credential pattern, integrity, time, obvious log fragments and exact duplicates; withheld records expose no fragments. Heuristics do not guarantee detection of every sensitive item; retention ≠ verified non-sensitive. Use only the fixed tool; do not assemble ad-hoc scripts on the spot or fall back to a full snapshot for sampling.
- Only after the user sees the counts do they approve letting bodies enter the current main-session model for analysis; bodies are not sent to sub-agents.
- If a draft needs testing, use compare-rules/rescan to preview only; commands and limits are in `references/workflow.md`. Do not turn a preview into permission to write rules, move sources, or clean up.

## 3. Draft candidates (keywords first, then short bounded regexes)

- Mark value dimensions first (preference/principle/organization division/project constraint/interaction learning/background device), and distinguish reusable preferences, time-bounded project decisions, one-off commands, and third-party quotes.
- Derive context patterns from positive **and negative** examples rather than piling up word counts; keywords first, then short bounded regexes. Bare "I think / don't / subjective words" do not automatically become lifelong facts — rules only produce candidates, and Review decides facts.
- When a validation set exists, report support counts/cross-session counts/hits/false positives/limits; with few samples, report "weak evidence/undefined" and do not fabricate precision or recall.

## 4. Review (fixed five groups)

- Add / Update / Remove / Conflict / Skip, ≤8 diffs, preserving existing rules. Write "none" for empty groups.
- Skip states the reason (duplicate, no long-term value, insufficient samples, insufficient authorization, credential withholding, formatting issue). Choices that reverse over time belong in Conflict, not as permanent facts.
- Check the exact diff with the user.

## 5. prepare → approval → commit (only through memory_ops.py)

- Pure rules transaction: `processed_ids=[]`, `reviewed_unmatched_ids=[]` (unless discarding analyzed unmatched records is also confirmed), writes only `learning/rules.json`, `before_sha256` taken from the current actual configuration, a complete UTF-8 write, preserving the "category → string list" structure.
- `prepare` only stages and returns a real txid; after showing the real txid and the complete diff, obtain a **new user approval for that txid** before `commit --txid REAL --approval-id ACTUAL`.
- Not Git, not model training; `PENDING_REVIEW`/`VERSION_CONFLICT` preserve state, with no automatic cancel/rebase. After commit, use the existing local `maintain`.

## 6. Verification and wrap-up

- After commit, re-read the actual configuration and check that counts/hashes match expectations; quickly verify matching with synthetic positive + negative examples (without changing runtime/configuration).
- With zero changes, keep unmatched by default and do not automatically clear the queue or logs; remove only the IDs the user confirmed as handled, and keep newly arrived candidates.
- Report completion honestly, or that work stopped at a reviewed DRAFT. Later learning is triggered only by the user; there is no periodic automatic LLM and no automatic opening of historical data.
