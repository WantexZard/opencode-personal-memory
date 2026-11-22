#!/usr/bin/env python3
"""Explicit, local, fixed-batch candidate previews (not confirmed memory).

CLI (global --root must precede the command; it defaults to memory_ops' root):
  open --source queue|unmatched --approval-id REF
  stats --work-id ID
  page --work-id ID [--offset N] [--limit N] [--view records|comparison]
       [--filter all|eligible|matched|added|removed]
  compare-rules --work-id ID             # full rules object on stdin
  rescan --work-id ID [--draft-stdin]    # unmatched only; optional full rules

Open freezes the chosen source's IDs, encoded-row hashes, classifications,
rule-file hash and both Python source-file hashes under Memory.lock. Only
learning/review-worksets/<32hex>.json is written, apart from the existing lock
and its directory/permission bookkeeping. It contains no candidate bodies,
raw approval reference, category names or rule patterns. Category IDs are
SHA-256 of each category name's UTF-8 bytes, sorted for stable set comparisons.
Approval references are caller-supplied audit references, not authentication.

Every later command revalidates the original rows and versions. Newly arrived
IDs contribute only to new_count, never to this batch or its comparisons.
Unmatched rows expire after the existing 30-day retention period, even before
maintenance removes them; queue rows have no additional age limit. A missing,
changed or expired original row invalidates the whole workset. Invalid/future
timestamps are withheld; their classification stays frozen. Legacy/missing IDs
require an explicit existing-helper migration; this module never migrates.

Stats/open/comparison responses contain counts, not candidate text. In records
pages, matched means a baseline rule hit; in comparison pages it means a hit
under the last preview's rules. Added/removed filters require comparison view
and mean any gained/lost category, not source movement. Offsets index the chosen
filtered view; keep view/filter fixed when following next_offset. A new preview
replaces the previous comparison; restart comparison paging afterwards.

Only eligible, exact-text-deduplicated candidates are matched or displayed.
Suppressed rows expose IDs and fixed reasons (plus duplicate_of), not bodies.
Deduplication prefers the first displayable complete copy, regardless of order;
it compares actual text, not a possibly truncated row's claimed original hash.
original_count = eligible_count + withheld_count + duplicate_count: duplicates
take counting precedence even when they also have another withholding reason.
Matched_count counts eligible rows with at least one baseline category hit.
Comparison counts are also row counts: changed/added/removed may overlap.
None of these counters means human-reviewed or confirmed fact.

Pages are complete UTF-8 JSON including LF, at most 40 KiB and 50 items. A page
shrinks by whole records; an oversized first item fails with RECORD_TOO_LARGE.
Credential filtering uses the existing CREDENTIAL_RE. The small structural log
heuristic below only reduces obvious transcript/log misreads: neither it nor
credential filtering is a complete privacy scrubber. Unlike legacy snapshot,
this outlet also withholds truncated/empty text and obvious log quotations.
It never reads profiles, archive, a database or the other candidate source.

Compare/rescan save only the latest draft hash and ID/category deltas; drafts
remain in memory. They do not move candidates, change rules, confirm memory or
clean up anything. Preview therefore DOES write workset metadata, but never
writes the sources, rules or formal memory. Worksets are not auto-cleaned.
Errors use memory_ops.run_safe's fixed-code envelope and exit statuses.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import uuid

import memory_ops as ops


WORKSETS = "learning/review-worksets"
SOURCES = {"queue": ops.QUEUE, "unmatched": ops.UNMATCHED}
PAGE_BYTES = 40 * 1024
REASONS = {
    "POSSIBLE_CREDENTIAL",
    "TRUNCATED",
    "INVALID_TIMESTAMP",
    "EMPTY_TEXT",
    "SUSPECTED_LOG_QUOTE",
    "EXACT_DUPLICATE",
}
# Structural evidence only; ordinary words like "log" or "remember" are not
# classifiers. Two timestamped severity lines, two transcript roles, explicit
# serialization delimiters, or a Python traceback with a frame are sufficient.
LOG_LINE = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"[^\r\n]*?\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\b",
    re.MULTILINE,
)
ROLE_LINE = re.compile(r"^\s*(?:user|assistant|system|tool):[ \t]", re.MULTILINE)
SERIALIZED_ROLE = re.compile(
    r"^\s*(?:<\|(?:im_start|im_end|start_header_id|eot_id)\|>|"
    r"</?(?:tool_call|tool_result)>)",
    re.MULTILINE,
)


def tool_hash():
    """No compatibility framework: any change to either implementation expires it."""
    return ops.digest(
        ops.encode(
            [
                ops.file_hash(Path(ops.__file__).absolute()),
                ops.file_hash(Path(__file__).absolute()),
            ]
        )
    )


def category_ids(text, compiled):
    return sorted(
        ops.digest(category.encode("utf-8"))
        for category in ops.match_rules(text, compiled)
    )


def withholding_reason(row, instant):
    text = row.get("text", "")
    if ops.CREDENTIAL_RE.search(text):
        return "POSSIBLE_CREDENTIAL"
    if row.get("truncated") or text.endswith("\n[truncated]"):
        return "TRUNCATED"
    date = ops.parsed_time(row.get("time"))
    if date is None or date > instant:
        return "INVALID_TIMESTAMP"
    if not text.strip():
        return "EMPTY_TEXT"
    if (
        len(LOG_LINE.findall(text)) >= 2
        or len(ROLE_LINE.findall(text)) >= 2
        or SERIALIZED_ROLE.search(text)
        or (
            re.search(r"^Traceback \(most recent call last\):\s*$", text, re.MULTILINE)
            and re.search(r'^\s+File "[^"\n]+", line \d+', text, re.MULTILINE)
        )
    ):
        return "SUSPECTED_LOG_QUOTE"
    return None


def check_expiry(rows, source, instant):
    if source == "unmatched":
        cutoff = instant - timedelta(days=30)
        for row in rows:
            date = ops.parsed_time(row.get("time"))
            if date is not None and date < cutoff:
                raise ops.OpError("WORKSET_EXPIRED")


def freeze_records(rows, compiled, instant):
    entries = []
    copies = {}
    for row in rows:
        entry = {
            "id": row["id"],
            "row_sha256": ops.digest(ops.encode(row)),
            "withheld": withholding_reason(row, instant),
            "duplicate_of": None,
            "category_ids": [],
        }
        entries.append(entry)
        text = row.get("text", "")
        if text.strip():
            copies.setdefault(text, []).append(entry)
    for group in copies.values():
        preferred = next(
            (entry for entry in group if entry["withheld"] is None), group[0]
        )
        for entry in group:
            if entry is not preferred:
                entry["duplicate_of"] = preferred["id"]
                entry["withheld"] = entry["withheld"] or "EXACT_DUPLICATE"
    for row, entry in zip(rows, entries):
        if entry["withheld"] is None:
            entry["category_ids"] = category_ids(row["text"], compiled)
    return entries


def is_hash(value):
    return isinstance(value, str) and ops.HASH_RE.fullmatch(value) is not None


def valid_categories(value):
    return (
        isinstance(value, list)
        and all(is_hash(item) for item in value)
        and value == sorted(set(value))
    )


def validate_workset(value, work_id):
    """Validate the on-disk boundary before trusting paths, IDs or output fields."""
    if (
        not isinstance(value, dict)
        or set(value) != {
            "work_id", "source", "approval_sha256", "rules_sha256",
            "tool_sha256", "records", "comparison",
        }
        or value["work_id"] != work_id
        or not isinstance(value["source"], str)
        or value["source"] not in SOURCES
        or not all(is_hash(value[key]) for key in (
            "approval_sha256", "rules_sha256", "tool_sha256",
        ))
        or not isinstance(value["records"], list)
    ):
        raise ops.OpError("INVALID_WORKSET")
    by_id = {}
    for entry in value["records"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {
                "id", "row_sha256", "withheld", "duplicate_of", "category_ids",
            }
            or not isinstance(entry["id"], str)
            or not ops.ID_RE.fullmatch(entry["id"])
            or entry["id"] in by_id
            or not is_hash(entry["row_sha256"])
            or not (
                entry["withheld"] is None
                or isinstance(entry["withheld"], str) and entry["withheld"] in REASONS
            )
            or not (
                entry["duplicate_of"] is None or isinstance(entry["duplicate_of"], str)
            )
            or not valid_categories(entry["category_ids"])
            or entry["withheld"] is not None and entry["category_ids"]
        ):
            raise ops.OpError("INVALID_WORKSET")
        by_id[entry["id"]] = entry
    for entry in by_id.values():
        duplicate = entry["duplicate_of"]
        if duplicate is not None and (
            duplicate not in by_id
            or duplicate == entry["id"]
            or by_id[duplicate]["duplicate_of"] is not None
            or entry["withheld"] is None
        ):
            raise ops.OpError("INVALID_WORKSET")
        if entry["withheld"] == "EXACT_DUPLICATE" and duplicate is None:
            raise ops.OpError("INVALID_WORKSET")
    comparison = value["comparison"]
    if comparison is None:
        return
    if (
        not isinstance(comparison, dict)
        or set(comparison) != {"operation", "draft_sha256", "changes"}
        or comparison["operation"] not in ("compare-rules", "rescan")
        or comparison["operation"] == "rescan" and value["source"] != "unmatched"
        or not (
            is_hash(comparison["draft_sha256"])
            or comparison["draft_sha256"] is None and comparison["operation"] == "rescan"
        )
        or not isinstance(comparison["changes"], list)
        or comparison["draft_sha256"] is None and comparison["changes"]
    ):
        raise ops.OpError("INVALID_WORKSET")
    seen = set()
    for change in comparison["changes"]:
        if (
            not isinstance(change, dict)
            or set(change) != {"id", "category_ids"}
            or not isinstance(change["id"], str)
            or change["id"] not in by_id
            or change["id"] in seen
            or by_id[change["id"]]["withheld"] is not None
            or not valid_categories(change["category_ids"])
        ):
            raise ops.OpError("INVALID_WORKSET")
        seen.add(change["id"])


def comparison_results(work):
    """Expand ID/category deltas in memory, without a saved draft or body pool."""
    comparison = work["comparison"]
    if comparison is None:
        raise ops.OpError("NO_COMPARISON")
    changes = {entry["id"]: entry["category_ids"] for entry in comparison["changes"]}
    results = {}
    for entry in work["records"]:
        if entry["withheld"] is not None:
            continue
        before = entry["category_ids"]
        after = changes.get(entry["id"], before)
        results[entry["id"]] = {
            "before_category_ids": before,
            "after_category_ids": after,
            "added_category_ids": sorted(set(after) - set(before)),
            "removed_category_ids": sorted(set(before) - set(after)),
        }
    return results


def summary(work, new_count):
    entries = work["records"]
    duplicate_count = sum(entry["duplicate_of"] is not None for entry in entries)
    eligible_count = sum(entry["withheld"] is None for entry in entries)
    result = {
        "ok": True,
        "work_id": work["work_id"],
        "source": work["source"],
        "original_count": len(entries),
        "eligible_count": eligible_count,
        "withheld_count": len(entries) - eligible_count - duplicate_count,
        "duplicate_count": duplicate_count,
        "matched_count": sum(bool(entry["category_ids"]) for entry in entries),
        "new_count": new_count,
        "comparison": None,
    }
    if work["comparison"] is not None:
        rows = list(comparison_results(work).values())
        result["comparison"] = {
            "matched_count": sum(bool(row["after_category_ids"]) for row in rows),
            "changed_count": sum(
                row["before_category_ids"] != row["after_category_ids"] for row in rows
            ),
            "added_count": sum(bool(row["added_category_ids"]) for row in rows),
            "removed_count": sum(bool(row["removed_category_ids"]) for row in rows),
        }
    return result


class Workflow:
    def __init__(self, root=None):
        self.memory = ops.Memory(root)

    def work_path(self, work_id):
        if not isinstance(work_id, str) or not ops.GEN_RE.fullmatch(work_id):
            raise ops.OpError("INVALID_WORK_ID")
        return self.memory.path(f"{WORKSETS}/{work_id}.json")

    def read_rules(self, expected=None):
        data = ops.read_bytes(self.memory.path(ops.RULES))
        version = None if data is None else ops.digest(data)
        if expected is not None and version != expected:
            raise ops.OpError("WORKSET_RULES_CHANGED")
        if data is None:
            raise ops.OpError("INVALID_RULES")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise ops.OpError("INVALID_RULES") from None
        return version, ops.validate_rules(value)

    def load(self, work_id, unmatched_only=False):
        """Caller holds Memory.lock for the entire read/validate/use operation."""
        work = ops.json_read(self.work_path(work_id))
        if work is None:
            raise ops.OpError("WORKSET_NOT_FOUND")
        validate_workset(work, work_id)
        if unmatched_only and work["source"] != "unmatched":
            raise ops.OpError("UNMATCHED_WORKSET_REQUIRED")
        if work["tool_sha256"] != tool_hash():
            raise ops.OpError("WORKSET_TOOL_CHANGED")
        _, compiled = self.read_rules(work["rules_sha256"])
        rows = ops.records(self.memory.path(SOURCES[work["source"]]))
        ops.stored_ids(rows)
        by_id = {row["id"]: row for row in rows}
        originals = []
        eligible_texts = set()
        instant = datetime.now(timezone.utc)
        for entry in work["records"]:
            row = by_id.get(entry["id"])
            if row is None:
                raise ops.OpError("WORKSET_STALE")
            if ops.digest(ops.encode(row)) != entry["row_sha256"]:
                raise ops.OpError("WORKSET_RECORD_CHANGED")
            # A malformed on-disk cache must not bypass output safety. Previously
            # withheld rows stay withheld even if a future timestamp has passed.
            if entry["withheld"] is None:
                if (
                    withholding_reason(row, instant) is not None
                    or row["text"] in eligible_texts
                ):
                    raise ops.OpError("INVALID_WORKSET")
                eligible_texts.add(row["text"])
            originals.append(row)
        check_expiry(originals, work["source"], instant)
        # Discard all newcomers after counting. They never enter the frozen view.
        return (
            work,
            {row["id"]: row for row in originals},
            len(rows) - len(originals),
            compiled,
        )

    def open(self, source, approval_id):
        if source not in SOURCES:
            raise ops.OpError("INVALID_SOURCE")
        reference = ops.approval(approval_id)
        with self.memory.lock():
            rows = ops.records(self.memory.path(SOURCES[source]))
            ops.stored_ids(rows)
            version, compiled = self.read_rules()
            instant = datetime.now(timezone.utc)
            check_expiry(rows, source, instant)
            work = {
                "work_id": uuid.uuid4().hex,
                "source": source,
                "approval_sha256": ops.digest(reference.encode("utf-8")),
                "rules_sha256": version,
                "tool_sha256": tool_hash(),
                "records": freeze_records(rows, compiled, instant),
                "comparison": None,
            }
            path = self.work_path(work["work_id"])
            if path.exists():
                raise ops.OpError("WORKSET_ID_COLLISION")
            ops.atomic_write(path, ops.encode(work))
            return summary(work, 0)

    def stats(self, work_id):
        with self.memory.lock():
            work, _, new_count, _ = self.load(work_id)
            return summary(work, new_count)

    def page(self, work_id, offset=0, limit=20, view="records", filter_name="all"):
        if offset < 0 or not 1 <= limit <= 50:
            raise ops.OpError("INVALID_PAGE")
        if view not in ("records", "comparison") or filter_name not in (
            "all", "eligible", "matched", "added", "removed",
        ):
            raise ops.OpError("INVALID_PAGE")
        if view == "records" and filter_name in ("added", "removed"):
            raise ops.OpError("INVALID_SCOPE_ARGUMENT")
        with self.memory.lock():
            work, rows, new_count, _ = self.load(work_id)
            comparisons = comparison_results(work) if view == "comparison" else {}
            selected = []
            for entry in work["records"]:
                comparison = comparisons.get(entry["id"], {})
                hits = (
                    comparison.get("after_category_ids", [])
                    if view == "comparison" else entry["category_ids"]
                )
                if (
                    filter_name == "all"
                    or filter_name == "eligible" and entry["withheld"] is None
                    or filter_name == "matched" and hits
                    or filter_name == "added" and comparison.get("added_category_ids")
                    or filter_name == "removed" and comparison.get("removed_category_ids")
                ):
                    selected.append(entry)
            if offset > len(selected):
                raise ops.OpError("INVALID_OFFSET")
            result = {
                "ok": True,
                "work_id": work_id,
                "source": work["source"],
                "view": view,
                "filter": filter_name,
                "offset": offset,
                "next_offset": None,
                "total_count": len(selected),
                "new_count": new_count,
                "items": [],
            }
            for entry in selected[offset:offset + limit]:
                item = {"id": entry["id"]}
                if entry["withheld"] is not None:
                    item["withheld"] = entry["withheld"]
                    if entry["duplicate_of"] is not None:
                        item["duplicate_of"] = entry["duplicate_of"]
                else:
                    row = rows[entry["id"]]
                    item.update(
                        time=row["time"],
                        text=row["text"],
                        text_sha256=ops.digest(row["text"].encode("utf-8")),
                    )
                    if view == "comparison":
                        item.update(comparisons[entry["id"]])
                    else:
                        item["category_ids"] = entry["category_ids"]
                result["items"].append(item)
                end = offset + len(result["items"])
                result["next_offset"] = end if end < len(selected) else None
                if len(ops.encode(result)) > PAGE_BYTES:
                    result["items"].pop()
                    if not result["items"]:
                        raise ops.OpError("RECORD_TOO_LARGE")
                    result["next_offset"] = offset + len(result["items"])
                    break
            return result

    def compare(self, work_id, draft=None, rescan=False):
        if draft is None and not rescan:
            raise ops.OpError("INVALID_RULES")
        with self.memory.lock():
            work, rows, new_count, compiled = self.load(work_id, unmatched_only=rescan)
            if draft is not None:
                compiled = ops.validate_rules(draft)
            changes = []
            for entry in work["records"]:
                if entry["withheld"] is not None:
                    continue
                after = category_ids(rows[entry["id"]]["text"], compiled)
                if after != entry["category_ids"]:
                    changes.append({"id": entry["id"], "category_ids": after})
            work["comparison"] = {
                "operation": "rescan" if rescan else "compare-rules",
                "draft_sha256": None if draft is None else ops.digest(ops.encode(draft)),
                "changes": changes,
            }
            ops.atomic_write(self.work_path(work_id), ops.encode(work))
            return summary(work, new_count)


def main():
    parser = ops.SafeParser(description=__doc__)
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=ops.SafeParser)
    opening = sub.add_parser("open")
    opening.add_argument("--source", choices=tuple(SOURCES), required=True)
    opening.add_argument("--approval-id", required=True)
    for name in ("stats", "page", "compare-rules", "rescan"):
        command = sub.add_parser(name)
        command.add_argument("--work-id", required=True)
        if name == "page":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=20)
            command.add_argument(
                "--view", choices=("records", "comparison"), default="records"
            )
            command.add_argument(
                "--filter", choices=("all", "eligible", "matched", "added", "removed"),
                default="all",
            )
        elif name == "rescan":
            command.add_argument("--draft-stdin", action="store_true")
    args = parser.parse_args()
    workflow = Workflow(args.root)
    if args.command == "open":
        result = workflow.open(args.source, args.approval_id)
    elif args.command == "stats":
        result = workflow.stats(args.work_id)
    elif args.command == "page":
        result = workflow.page(args.work_id, args.offset, args.limit, args.view, args.filter)
    else:
        use_draft = args.command == "compare-rules" or args.draft_stdin
        # Read stdin before taking the lock; a slow producer must not hold it.
        draft = ops.load_input() if use_draft else None
        result = workflow.compare(args.work_id, draft, rescan=args.command == "rescan")
    # Emit exactly the UTF-8 bytes measured by page(), including one LF. Do not
    # rely on the console encoding or Windows text-mode newline translation.
    sys.stdout.buffer.write(ops.encode(result))


if __name__ == "__main__":
    sys.exit(ops.run_safe(main))
