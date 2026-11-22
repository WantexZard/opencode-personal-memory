# Batched review and rule preview

`workflow.py` turns statistics, pagination and rule comparison into fixed commands. It calls no model, writes no formal memory, and never automatically accepts facts or deletes candidates.

## Choose the scope before looking at content

- For organizing facts, the default is `queue`; for learning rules or rescanning, the default is `unmatched`. The two sides are authorized and opened separately, and reading one never pulls in the other.
- When only statistics are permitted, use `open`, `stats` or a rule comparison, and do not run `page`, which returns candidate bodies.
- Permission to adjust rules does not mean permission to read candidates; permission to preview does not mean approval to write.
- This round's workset does not carry read permission into a new session. public/topic memory goes through `reference` separately; private still requires approval every time, per field, per purpose.

## Open this round's workset

Below, `$PM` is the skill directory. Use `python3` on POSIX and usually `python` on Windows. Replace `USER_SOURCE_REF` with the actual authorization reference and `WORK_ID` with the value returned by the tool.

```sh
# Pick one: open only the source you are authorized to read
python3 -B "$PM/workflow.py" open --source queue --approval-id USER_SOURCE_REF
python3 -B "$PM/workflow.py" open --source unmatched --approval-id USER_SOURCE_REF

python3 -B "$PM/workflow.py" stats --work-id WORK_ID
python3 -B "$PM/workflow.py" page --work-id WORK_ID --offset 0 --limit 20
```

Each round fixes the original records; newly arrived messages only increase `new_count` and never join this round. Follow the returned `next_offset` until it is `null`. Keep the same `view` and `filter` while paging.

Each page holds at most 50 items (`--limit` accepts 1..50) and at most 40 KiB of complete JSON, including the trailing newline. To fit complete records, the actual item count may be smaller. A single record that does not fit raises `RECORD_TOO_LARGE`; it is not truncated or skipped — state the limitation and do not treat the fragment as a complete fact.

Credential patterns, truncation, empty text, invalid timestamps and obvious log/conversation fragments return only a reason; exact duplicates show one body copy while the other IDs are kept. Filtering can miss or misjudge, and does not guarantee that the remaining bodies are safe. The legacy `snapshot` keeps its original output and migration behavior and does not have this pagination or the full screening here.

## Compare rules and rescan

```sh
# Read the complete draft rules object from stdin
python3 -B "$PM/workflow.py" compare-rules --work-id WORK_ID

# The next two accept an unmatched workset only
python3 -B "$PM/workflow.py" rescan --work-id WORK_ID
python3 -B "$PM/workflow.py" rescan --work-id WORK_ID --draft-stdin

# Only after being authorized to read the source bodies, inspect the records involved in changes
python3 -B "$PM/workflow.py" page --work-id WORK_ID --view comparison --filter added
```

A draft is passed through the execution interface's stdin as a complete JSON object, e.g. `{"DemoA":["alpha"],"DemoB":["beta"]}`, without an outer `rules` wrapper and not as command-line arguments.

`compare-rules` compares the current rules with the draft. `rescan` uses the current rules by default and the draft with `--draft-stdin`. They only process the displayable, deduplicated records of this round; they do not move candidates, change rules, or clean the source. Comparing again replaces the previous round's comparison result, and comparison paging then restarts from the beginning.

## Reading the statistics

| Field | Meaning |
|---|---|
| `original_count` | total records in this round |
| `eligible_count` | records whose body can be shown, after deduplication |
| `withheld_count` | non-duplicate records whose body cannot be shown |
| `duplicate_count` | exact duplicate records; counted here first even if another withholding reason also applies |
| `matched_count` | hits of the original rules among displayable records |
| `new_count` | records added after this round was opened, not mixed into paging |
| `comparison.matched_count` | hits of the rules in this preview |
| `comparison.changed_count` | records whose set of hit categories changed |
| `comparison.added_count` / `removed_count` | records that gained / lost at least one category; the two may overlap |

`original_count = eligible_count + withheld_count + duplicate_count`. Gaining a category does not necessarily mean a first match; switching categories may also count in both added and removed.

`category_ids` in records pages are SHA-256 of the category name's UTF-8 bytes; comparison pages give the before and after IDs and the added/removed IDs separately. They are used to compare sets, not as readable category names, and they are not an anonymization guarantee. The meaning of a rule still has to be judged together with the authorized rule configuration and the draft.

None of these numbers counts "the AI has read everything", "the user has confirmed", or "facts have been written". A rule that does not match also does not prove a record has no long-term value.

## What is stored

The tool only stores this round's IDs, versions, categories and comparison results under `learning/review-worksets/`; it does not store a separate pool of candidate bodies, draft expressions, or raw authorization references. Categories and authorization references are identified by hash. It reuses the existing Memory file lock and adds no background process, model or third-party dependency.

A preview writes this metadata and may create lock files or maintain directory permissions; "preview only" means it does not modify the candidate source, the formal rules or formal memory, not that nothing at all is written to disk. Worksets are still private operational material and must not be committed to the repository; hashes are not encryption. These files are not cleaned up automatically.

## Invalidation and later writes

| Error | Handling |
|---|---|
| `WORKSET_STALE` / `WORKSET_RECORD_CHANGED` | the original record was deleted or changed; stop this round and reopen after authorization |
| `WORKSET_EXPIRED` | an unmatched original record exceeded the existing 30-day limit; first ask whether to run the existing local maintenance, and do not use a preview to delete anything automatically |
| `WORKSET_RULES_CHANGED` / `WORKSET_TOOL_CHANGED` | the rules or the tool changed; explicitly reopen rather than silently continuing under new rules |
| `LEGACY_REQUIRES_SNAPSHOT` | obtain separate approval first and migrate the old identity explicitly with the original helper; a preview never migrates |
| `UNMATCHED_WORKSET_REQUIRED` | a rescan may only use an authorized, opened unmatched workset |
| `NO_COMPARISON` | run a rule comparison or rescan first |
| `RECORD_TOO_LARGE` | a complete record does not fit in one page; state the limitation and do not truncate |
| `BUSY` | retry later; do not issue repeated concurrent calls |

When suggestions need to be accepted, keep using the original `memory_ops.py prepare → user approval → commit`; there is no second write path. The target file must be separately authorized for reading, and the actual version hash must be used. The cleanup list defaults to empty; accepting a fact does not mean agreeing to delete its source, and skipping does not mean agreeing to archive it.

For tests, point `workflow.py --root ABS COMMAND ...` at a synthetic directory, placing `--root` before the subcommand. Do not override the install directory in daily use. Real-machine verification status on Windows/macOS matches the project README; synthetic tests are no substitute.
