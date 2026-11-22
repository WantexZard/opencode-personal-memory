"""Synthetic-only workflow preview tests.

Every runtime path is an isolated stdlib temporary directory. No fixture here
reads or writes the real profile, rules, queues or database.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
MODULE_DIR = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location(
    "workflow_under_test", MODULE_DIR / "workflow.py"
)
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)
ops = workflow.ops


def iso(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).isoformat()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir(mode=0o700)
        self.workflow = workflow.Workflow(self.root)
        self.write(ops.RULES, {"CatA": ["alpha"]})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (
            ops.encode(value)
            if isinstance(value, dict)
            else value.encode()
            if isinstance(value, str)
            else value
        )
        path.write_bytes(data)
        path.chmod(0o600)

    def bytes(self, name):
        path = self.root / name
        return path.read_bytes() if path.exists() else None

    def row(self, ident, text, when=None, **extra):
        row = {"id": ident, "time": when or iso(), "text": text}
        row.update(extra)
        return row

    def write_rows(self, name, rows):
        self.write(name, ops.rows_bytes(rows))

    def open(self, source="queue", approval="synthetic-audit-ref"):
        return self.workflow.open(source, approval)

    def workset_path(self, work_id):
        return self.root / workflow.WORKSETS / f"{work_id}.json"

    def workset(self, work_id):
        return json.loads(self.workset_path(work_id).read_bytes())

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(ops.OpError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def track(self, name):
        seen = []
        original = getattr(ops, name)

        def wrapper(*args, **kwargs):
            seen.append(Path(args[0]))
            return original(*args, **kwargs)

        patcher = mock.patch.object(ops, name, wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def read_names(self, seen):
        return {
            path.relative_to(self.root).as_posix()
            for path in seen
            if path.is_relative_to(self.root)
        }

    def learning_files(self):
        learning = self.root / "learning"
        if not learning.exists():
            return set()
        return {
            path.relative_to(self.root).as_posix()
            for path in learning.rglob("*")
            if path.is_file()
        }

    def cli(self, arguments, payload=b""):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(MODULE_DIR / "workflow.py"),
                "--root",
                str(self.root),
                *arguments,
            ],
            input=payload,
            capture_output=True,
            check=False,
        )


class OpenTests(Fixture):
    def test_open_freezes_only_the_chosen_source(self):
        self.write_rows(
            ops.QUEUE, [self.row("1" * 32, "alpha queue-synthetic-body")]
        )
        self.write_rows(
            ops.UNMATCHED, [self.row("2" * 32, "beta unmatched-synthetic-body")]
        )
        result = self.open("queue")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "queue")
        self.assertEqual(result["original_count"], 1)
        work = self.workset(result["work_id"])
        self.assertEqual([entry["id"] for entry in work["records"]], ["1" * 32])
        self.assertNotIn(b"unmatched-synthetic-body", self.workset_path(
            result["work_id"]
        ).read_bytes())

    def test_open_reads_no_other_source_archive_or_profile(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        self.write_rows(ops.UNMATCHED, [self.row("2" * 32, "beta body")])
        self.write_rows(ops.ARCHIVE, [self.row("3" * 32, "gamma body")])
        self.write(ops.PUBLIC, "# Public\nsynthetic-public-body\n")
        self.write(ops.PRIVATE, "# Private\nsynthetic-private-body\n")

        seen = self.track("read_bytes")
        opened = self.open("queue")
        self.workflow.stats(opened["work_id"])
        self.workflow.page(opened["work_id"])
        self.workflow.compare(opened["work_id"], {"CatA": ["alpha", "beta"]})

        names = self.read_names(seen)
        self.assertIn(ops.QUEUE, names)
        self.assertIn(ops.RULES, names)
        self.assertNotIn(ops.UNMATCHED, names)
        self.assertNotIn(ops.ARCHIVE, names)
        self.assertFalse(any(name.startswith("user-profile/") for name in names))

    def test_open_unmatched_reads_only_unmatched(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha queue body")])
        self.write_rows(ops.UNMATCHED, [self.row("2" * 32, "beta unmatched body")])
        seen = self.track("read_bytes")
        result = self.open("unmatched")
        self.assertEqual(result["source"], "unmatched")
        names = self.read_names(seen)
        self.assertEqual(
            names, {ops.UNMATCHED, ops.RULES}
        )
        self.assertEqual(
            [entry["id"] for entry in self.workset(result["work_id"])["records"]],
            ["2" * 32],
        )

    def test_workset_has_no_body_approval_category_name_or_pattern(self):
        self.write(
            ops.RULES,
            {"CATEGORY-MARKER-2b7c": ["PATTERN-MARKER-4d1e"]},
        )
        self.write_rows(
            ops.QUEUE,
            [self.row("1" * 32, "PATTERN-MARKER-4d1e BODY-MARKER-9f3a")],
        )
        approval = "APPROVAL-MARKER-7a2f"
        result = self.open("queue", approval)
        data = self.workset_path(result["work_id"]).read_bytes()
        for marker in (
            b"BODY-MARKER-9f3a",
            b"APPROVAL-MARKER-7a2f",
            b"CATEGORY-MARKER-2b7c",
            b"PATTERN-MARKER-4d1e",
            b'"text"',
        ):
            self.assertNotIn(marker, data)
        work = self.workset(result["work_id"])
        self.assertEqual(
            work["approval_sha256"], ops.digest(approval.encode("utf-8"))
        )
        self.assertEqual(len(work["records"][0]["category_ids"]), 1)

    def test_stats_contains_counts_not_candidate_text(self):
        self.write(
            ops.RULES, {"CATEGORY-MARKER-2b7c": ["PATTERN-MARKER-4d1e"]}
        )
        self.write_rows(
            ops.QUEUE,
            [self.row("1" * 32, "PATTERN-MARKER-4d1e BODY-MARKER-9f3a")],
        )
        result = self.open("queue")
        stats = self.workflow.stats(result["work_id"])
        rendered = json.dumps(stats)
        for marker in (
            "BODY-MARKER-9f3a",
            "PATTERN-MARKER-4d1e",
            "CATEGORY-MARKER-2b7c",
            "text",
            "category_ids",
        ):
            self.assertNotIn(marker, rendered)
        self.assertEqual(stats["original_count"], 1)
        self.assertEqual(stats["eligible_count"], 1)
        self.assertEqual(stats["matched_count"], 1)
        self.assertEqual(stats["new_count"], 0)

    def test_preview_writes_only_workset_metadata(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        self.write_rows(ops.UNMATCHED, [self.row("2" * 32, "beta body")])
        self.write_rows(ops.ARCHIVE, [self.row("3" * 32, "gamma body")])
        self.write(ops.PUBLIC, "# Public\nsynthetic-public\n")
        before = {
            name: self.bytes(name)
            for name in (ops.QUEUE, ops.UNMATCHED, ops.ARCHIVE, ops.RULES, ops.PUBLIC)
        }
        result = self.open("queue")
        work_id = result["work_id"]
        self.workflow.stats(work_id)
        self.workflow.page(work_id)
        self.workflow.compare(work_id, {"CatA": ["alpha", "beta"]})
        after = {
            name: self.bytes(name)
            for name in (ops.QUEUE, ops.UNMATCHED, ops.ARCHIVE, ops.RULES, ops.PUBLIC)
        }
        self.assertEqual(before, after)
        self.assertEqual(
            self.learning_files(),
            {
                "learning/.memory.lock",
                f"{workflow.WORKSETS}/{work_id}.json",
                ops.QUEUE,
                ops.UNMATCHED,
                ops.ARCHIVE,
                ops.RULES,
            },
        )

    def test_open_rejects_invalid_source_and_approval(self):
        self.assert_code("INVALID_SOURCE", self.open, "archive", "ref")
        self.assert_code("APPROVAL_REQUIRED", self.open, "queue", "   ")
        self.assert_code("APPROVAL_REQUIRED", self.open, "queue", "x" * 513)
        self.assertFalse((self.root / workflow.WORKSETS).exists())

    def test_legacy_and_missing_ids_refuse_without_migration(self):
        self.write_rows(ops.QUEUE, [self.row("a" * 16, "alpha legacy body")])
        before = self.bytes(ops.QUEUE)
        self.assert_code("LEGACY_REQUIRES_SNAPSHOT", self.open, "queue")
        self.assertEqual(self.bytes(ops.QUEUE), before)
        self.write_rows(ops.QUEUE, [{"time": iso(), "text": "alpha missing id"}])
        self.assert_code("LEGACY_REQUIRES_SNAPSHOT", self.open, "queue")
        self.assertFalse((self.root / workflow.WORKSETS).exists())

    def test_queue_has_no_age_limit_but_unmatched_expires(self):
        self.write_rows(
            ops.QUEUE, [self.row("1" * 32, "alpha old queue", iso(-timedelta(days=100)))]
        )
        self.assertTrue(self.open("queue")["ok"])
        self.write_rows(
            ops.UNMATCHED,
            [self.row("2" * 32, "beta old unmatched", iso(-timedelta(days=31)))],
        )
        self.assert_code("WORKSET_EXPIRED", self.open, "unmatched")
        self.write_rows(
            ops.UNMATCHED,
            [self.row("3" * 32, "beta recent", iso(-timedelta(days=29)))],
        )
        self.assertTrue(self.open("unmatched")["ok"])


class RevalidationTests(Fixture):
    def test_new_arrivals_only_increase_new_count(self):
        rows = [self.row(f"{index:032x}", f"alpha body {index}") for index in range(3)]
        self.write_rows(ops.QUEUE, rows)
        work_id = self.open("queue")["work_id"]
        original_ids = [row["id"] for row in rows]
        self.write_rows(
            ops.QUEUE,
            rows
            + [
                self.row("a" * 32, "alpha newcomer one"),
                self.row("b" * 32, "alpha newcomer two"),
            ],
        )
        stats = self.workflow.stats(work_id)
        self.assertEqual(stats["new_count"], 2)
        self.assertEqual(stats["original_count"], 3)
        page = self.workflow.page(work_id, limit=50)
        self.assertEqual(page["new_count"], 2)
        self.assertEqual(page["total_count"], 3)
        self.assertEqual([item["id"] for item in page["items"]], original_ids)

    def test_changed_or_deleted_original_row_rejects_workset(self):
        rows = [
            self.row("1" * 32, "alpha first"),
            self.row("2" * 32, "alpha second"),
        ]
        self.write_rows(ops.QUEUE, rows)
        work_id = self.open("queue")["work_id"]
        original = self.bytes(ops.QUEUE)
        changed = [dict(rows[0], text="alpha changed"), rows[1]]
        self.write_rows(ops.QUEUE, changed)
        self.assert_code("WORKSET_RECORD_CHANGED", self.workflow.stats, work_id)
        self.write(ops.QUEUE, original)
        self.write_rows(ops.QUEUE, [rows[0]])
        self.assert_code("WORKSET_STALE", self.workflow.stats, work_id)

    def test_rules_or_tool_change_rejects_old_workset(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]
        self.write(ops.RULES, {"CatA": ["alpha", "beta"]})
        self.assert_code("WORKSET_RULES_CHANGED", self.workflow.stats, work_id)

        self.write(ops.RULES, {"CatA": ["alpha"]})
        second = self.open("queue")["work_id"]
        with mock.patch.object(workflow, "tool_hash", return_value="f" * 64):
            self.assert_code("WORKSET_TOOL_CHANGED", self.workflow.stats, second)

    def test_unmatched_expiry_is_rechecked_on_every_command(self):
        self.write_rows(
            ops.UNMATCHED,
            [self.row("1" * 32, "beta nearly expired", iso(-timedelta(days=29)))],
        )
        work_id = self.open("unmatched")["work_id"]

        class FixedNow:
            @staticmethod
            def now(tz=None):
                return datetime.now(timezone.utc) + timedelta(days=2)

        with mock.patch.object(workflow, "datetime", FixedNow):
            self.assert_code("WORKSET_EXPIRED", self.workflow.stats, work_id)

    def test_future_timestamp_stays_withheld_after_it_passes(self):
        self.write_rows(
            ops.QUEUE,
            [self.row("1" * 32, "alpha future body", iso(timedelta(days=1)))],
        )
        work_id = self.open("queue")["work_id"]
        page = self.workflow.page(work_id)
        self.assertEqual(page["items"][0]["withheld"], "INVALID_TIMESTAMP")
        self.assertNotIn("text", page["items"][0])

        class FixedNow:
            @staticmethod
            def now(tz=None):
                return datetime.now(timezone.utc) + timedelta(days=2)

        with mock.patch.object(workflow, "datetime", FixedNow):
            self.assertEqual(self.workflow.stats(work_id)["withheld_count"], 1)
            page = self.workflow.page(work_id)
        self.assertEqual(page["items"][0]["withheld"], "INVALID_TIMESTAMP")
        self.assertNotIn("text", page["items"][0])


class PageTests(Fixture):
    def test_encoded_boundary_includes_escaping_and_final_newline(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, '中\t\\"😀' * 12)])
        work_id = self.open("queue")["work_id"]
        expected = self.workflow.page(work_id)
        encoded = ops.encode(expected)
        self.assertTrue(encoded.endswith(b"\n"))
        with mock.patch.object(workflow, "PAGE_BYTES", len(encoded)):
            self.assertEqual(self.workflow.page(work_id), expected)
        with mock.patch.object(workflow, "PAGE_BYTES", len(encoded) - 1):
            self.assert_code("RECORD_TOO_LARGE", self.workflow.page, work_id)

    def test_empty_batch(self):
        result = self.open("queue")
        self.assertEqual(result["original_count"], 0)
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["matched_count"], 0)
        page = self.workflow.page(result["work_id"])
        self.assertEqual(page["total_count"], 0)
        self.assertEqual(page["items"], [])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(page["offset"], 0)
        self.assert_code(
            "INVALID_OFFSET", self.workflow.page, result["work_id"], offset=1
        )

        compared = self.workflow.compare(result["work_id"], {"CatA": ["alpha"]})
        self.assertEqual(
            compared["comparison"],
            {"matched_count": 0, "changed_count": 0, "added_count": 0, "removed_count": 0},
        )
        page = self.workflow.page(
            result["work_id"], view="comparison", filter_name="added"
        )
        self.assertEqual(page["total_count"], 0)

    def test_filters_and_withheld_items_expose_metadata_only(self):
        rows = [
            self.row("1" * 32, "alpha one"),
            self.row("2" * 32, "beta two"),
            self.row("3" * 32, "password: synthetic-hunter2"),
            self.row("4" * 32, "alpha one"),
        ]
        self.write_rows(ops.QUEUE, rows)
        work_id = self.open("queue")["work_id"]
        stats = self.workflow.stats(work_id)
        self.assertEqual(stats["original_count"], 4)
        self.assertEqual(stats["eligible_count"], 2)
        self.assertEqual(stats["withheld_count"], 1)
        self.assertEqual(stats["duplicate_count"], 1)
        self.assertEqual(stats["matched_count"], 1)

        page = self.workflow.page(work_id, limit=50)
        self.assertEqual([item["id"] for item in page["items"]], [row["id"] for row in rows])
        self.assertNotIn("synthetic-hunter2", json.dumps(page))

        eligible = self.workflow.page(work_id, filter_name="eligible")
        self.assertEqual([item["id"] for item in eligible["items"]], ["1" * 32, "2" * 32])
        matched = self.workflow.page(work_id, filter_name="matched")
        self.assertEqual([item["id"] for item in matched["items"]], ["1" * 32])

        withheld = page["items"][2]
        self.assertEqual(set(withheld), {"id", "withheld"})
        self.assertEqual(withheld["withheld"], "POSSIBLE_CREDENTIAL")
        duplicate = page["items"][3]
        self.assertEqual(duplicate["withheld"], "EXACT_DUPLICATE")
        self.assertEqual(duplicate["duplicate_of"], "1" * 32)
        self.assertNotIn("text", duplicate)
        for filter_name in ("added", "removed"):
            self.assert_code(
                "INVALID_SCOPE_ARGUMENT",
                self.workflow.page,
                work_id,
                filter_name=filter_name,
            )

    def test_pagination_cursor_and_page_byte_budget(self):
        rows = [
            self.row(f"{index:032x}", f"record-{index}-" + "x" * 15000)
            for index in range(5)
        ]
        self.write_rows(ops.QUEUE, rows)
        work_id = self.open("queue")["work_id"]
        expected = [row["id"] for row in rows]
        collected = []
        offset = 0
        pages = 0
        while True:
            page = self.workflow.page(work_id, offset=offset, limit=50)
            pages += 1
            self.assertLessEqual(len(ops.encode(page)), workflow.PAGE_BYTES)
            self.assertTrue(ops.encode(page).endswith(b"\n"))
            self.assertEqual(page["total_count"], 5)
            self.assertEqual(page["offset"], offset)
            collected.extend(item["id"] for item in page["items"])
            if page["next_offset"] is None:
                break
            self.assertEqual(
                page["next_offset"], offset + len(page["items"])
            )
            offset = page["next_offset"]
        self.assertGreater(pages, 1)
        self.assertEqual(collected, expected)
        self.assertEqual(len(set(collected)), len(expected))

    def test_unicode_and_escaped_text_round_trip(self):
        text = 'quote " backslash \\ newline\n emoji 😀 CJK 记忆'
        self.write_rows(ops.QUEUE, [self.row("1" * 32, text)])
        work_id = self.open("queue")["work_id"]
        page = self.workflow.page(work_id)
        self.assertEqual(page["items"][0]["text"], text)
        self.assertLessEqual(len(ops.encode(page)), workflow.PAGE_BYTES)
        self.assertEqual(json.loads(ops.encode(page))["items"][0]["text"], text)

    def test_oversized_record_and_whole_record_shrinking(self):
        self.write_rows(
            ops.QUEUE,
            [
                self.row("1" * 32, "small body"),
                self.row("2" * 32, "z" * (41 * 1024)),
            ],
        )
        work_id = self.open("queue")["work_id"]
        page = self.workflow.page(work_id, limit=50)
        self.assertEqual([item["id"] for item in page["items"]], ["1" * 32])
        self.assertEqual(page["next_offset"], 1)
        self.assertLessEqual(len(ops.encode(page)), workflow.PAGE_BYTES)
        self.assert_code(
            "RECORD_TOO_LARGE", self.workflow.page, work_id, offset=1, limit=1
        )

    def test_invalid_page_arguments(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]
        for offset, limit in ((-1, 20), (0, 0), (0, 51)):
            self.assert_code(
                "INVALID_PAGE",
                self.workflow.page,
                work_id,
                offset=offset,
                limit=limit,
            )
        self.assert_code(
            "INVALID_PAGE", self.workflow.page, work_id, view="records2"
        )
        self.assert_code(
            "INVALID_PAGE", self.workflow.page, work_id, filter_name="unknown"
        )
        self.assertEqual(
            self.workflow.page(work_id, offset=1)["items"], []
        )
        self.assertEqual(
            self.workflow.page(work_id, limit=50)["total_count"], 1
        )
        self.assert_code("INVALID_WORK_ID", self.workflow.page, "not-an-id")


class WithholdingTests(Fixture):
    def test_withholding_reasons_expose_only_metadata(self):
        rows = [
            self.row("1" * 32, "password: synthetic-hunter2"),
            self.row("2" * 32, "truncated synthetic body", truncated=True),
            self.row("3" * 32, "alpha bad time", when="not-a-time"),
            self.row("4" * 32, "alpha future", iso(timedelta(days=1))),
            self.row("5" * 32, "   "),
            self.row(
                "6" * 32,
                "2026-01-01T00:00:00 INFO synthetic-log-start\n"
                "2026-01-01T00:00:01 ERROR synthetic-log-fail",
            ),
            self.row("7" * 32, "please read the log and remember this 记忆 note"),
        ]
        self.write_rows(ops.QUEUE, rows)
        work_id = self.open("queue")["work_id"]
        stats = self.workflow.stats(work_id)
        self.assertEqual(stats["original_count"], 7)
        self.assertEqual(stats["eligible_count"], 1)
        self.assertEqual(stats["withheld_count"], 6)
        self.assertEqual(stats["matched_count"], 0)

        page = self.workflow.page(work_id, limit=50)
        by_id = {item["id"]: item for item in page["items"]}
        expected = {
            "1" * 32: "POSSIBLE_CREDENTIAL",
            "2" * 32: "TRUNCATED",
            "3" * 32: "INVALID_TIMESTAMP",
            "4" * 32: "INVALID_TIMESTAMP",
            "5" * 32: "EMPTY_TEXT",
            "6" * 32: "SUSPECTED_LOG_QUOTE",
        }
        for ident, reason in expected.items():
            self.assertEqual(by_id[ident]["withheld"], reason)
            self.assertNotIn("text", by_id[ident])
            self.assertNotIn("time", by_id[ident])
        rendered = json.dumps(page)
        for marker in (
            "synthetic-hunter2",
            "truncated synthetic body",
            "synthetic-log-start",
            "synthetic-log-fail",
        ):
            self.assertNotIn(marker, rendered)
        self.assertEqual(by_id["7" * 32]["text"], rows[6]["text"])
        self.assertEqual(by_id["7" * 32]["category_ids"], [])

    def test_truncated_suffix_and_serialized_role_are_withheld(self):
        self.write_rows(
            ops.QUEUE,
            [
                self.row("1" * 32, "synthetic suffix body\n[truncated]"),
                self.row("2" * 32, "synthetic serialized\n<|im_start|>user"),
            ],
        )
        work_id = self.open("queue")["work_id"]
        page = self.workflow.page(work_id)
        self.assertEqual(
            [item["withheld"] for item in page["items"]],
            ["TRUNCATED", "SUSPECTED_LOG_QUOTE"],
        )
        self.assertNotIn("synthetic suffix body", json.dumps(page))
        self.assertNotIn("synthetic serialized", json.dumps(page))

    def test_role_lines_and_traceback_are_log_quotes(self):
        self.write_rows(
            ops.QUEUE,
            [
                self.row("1" * 32, "user: synthetic\nassistant: synthetic"),
                self.row(
                    "2" * 32,
                    "Traceback (most recent call last):\n"
                    '  File "synthetic.py", line 7, in <module>\n'
                    "    raise ValueError",
                ),
            ],
        )
        work_id = self.open("queue")["work_id"]
        page = self.workflow.page(work_id)
        self.assertEqual(
            [item["withheld"] for item in page["items"]],
            ["SUSPECTED_LOG_QUOTE", "SUSPECTED_LOG_QUOTE"],
        )
        self.assertNotIn("synthetic.py", json.dumps(page))

    def test_duplicate_prefers_normal_copy_and_counts_once(self):
        self.write_rows(
            ops.QUEUE,
            [
                self.row("1" * 32, "duplicate synthetic body", truncated=True),
                self.row("2" * 32, "duplicate synthetic body"),
            ],
        )
        work_id = self.open("queue")["work_id"]
        stats = self.workflow.stats(work_id)
        self.assertEqual(stats["original_count"], 2)
        self.assertEqual(stats["eligible_count"], 1)
        self.assertEqual(stats["duplicate_count"], 1)
        self.assertEqual(stats["withheld_count"], 0)
        page = self.workflow.page(work_id)
        self.assertEqual(page["items"][0]["withheld"], "TRUNCATED")
        self.assertEqual(page["items"][0]["duplicate_of"], "2" * 32)
        self.assertEqual(page["items"][1]["text"], "duplicate synthetic body")
        self.assertEqual(
            sum(1 for item in page["items"] if "text" in item), 1
        )
        self.assertNotIn("duplicate synthetic body", json.dumps(page["items"][0]))

    def test_duplicate_reason_takes_counting_precedence(self):
        self.write_rows(
            ops.QUEUE,
            [
                self.row("1" * 32, "password: duplicate-synthetic-secret"),
                self.row("2" * 32, "password: duplicate-synthetic-secret"),
            ],
        )
        work_id = self.open("queue")["work_id"]
        stats = self.workflow.stats(work_id)
        self.assertEqual(stats["eligible_count"], 0)
        self.assertEqual(stats["duplicate_count"], 1)
        self.assertEqual(stats["withheld_count"], 1)
        page = self.workflow.page(work_id)
        self.assertEqual(
            [item["withheld"] for item in page["items"]],
            ["POSSIBLE_CREDENTIAL", "POSSIBLE_CREDENTIAL"],
        )
        self.assertEqual(page["items"][1]["duplicate_of"], "1" * 32)
        self.assertNotIn("duplicate-synthetic-secret", json.dumps(page))


class CompareTests(Fixture):
    def test_compare_counts_and_comparison_page_views(self):
        rows = [
            self.row("1" * 32, "alpha only"),
            self.row("2" * 32, "beta only"),
            self.row("3" * 32, "alpha beta both"),
            self.row("4" * 32, "nothing here"),
        ]
        self.write_rows(ops.QUEUE, rows)
        work_id = self.open("queue")["work_id"]

        first = self.workflow.compare(
            work_id, {"CatA": ["alpha"], "CatB": ["beta"]}
        )
        self.assertEqual(
            first["comparison"],
            {"matched_count": 3, "changed_count": 2, "added_count": 2, "removed_count": 0},
        )
        added = self.workflow.page(
            work_id, view="comparison", filter_name="added"
        )
        self.assertEqual([item["id"] for item in added["items"]], ["2" * 32, "3" * 32])
        for item in added["items"]:
            self.assertEqual(
                item["added_category_ids"],
                sorted(set(item["after_category_ids"]) - set(item["before_category_ids"])),
            )
        matched = self.workflow.page(
            work_id, view="comparison", filter_name="matched"
        )
        self.assertEqual(
            [item["id"] for item in matched["items"]],
            ["1" * 32, "2" * 32, "3" * 32],
        )

        second = self.workflow.compare(work_id, {"CatB": ["beta"]})
        self.assertEqual(
            second["comparison"],
            {"matched_count": 2, "changed_count": 3, "added_count": 2, "removed_count": 2},
        )
        removed = self.workflow.page(
            work_id, view="comparison", filter_name="removed"
        )
        self.assertEqual(
            [item["id"] for item in removed["items"]], ["1" * 32, "3" * 32]
        )
        added = self.workflow.page(
            work_id, view="comparison", filter_name="added"
        )
        self.assertEqual([item["id"] for item in added["items"]], ["2" * 32, "3" * 32])
        eligible = self.workflow.page(
            work_id, view="comparison", filter_name="eligible"
        )
        self.assertEqual(eligible["total_count"], 4)

    def test_compare_draft_body_is_not_stored(self):
        self.write_rows(
            ops.QUEUE, [self.row("1" * 32, "draftpatternmarker synthetic body")]
        )
        work_id = self.open("queue")["work_id"]
        draft = {"DraftCategoryMarker": ["draftpatternmarker"]}
        result = self.workflow.compare(work_id, draft)
        data = self.workset_path(work_id).read_bytes()
        for marker in (
            b"DraftCategoryMarker",
            b"draftpatternmarker",
            b"synthetic body",
        ):
            self.assertNotIn(marker, data)
        work = self.workset(work_id)
        self.assertEqual(
            work["comparison"]["draft_sha256"], ops.digest(ops.encode(draft))
        )
        self.assertEqual(work["comparison"]["changes"][0]["id"], "1" * 32)
        self.assertEqual(result["comparison"]["added_count"], 1)

    def test_comparison_view_requires_a_saved_comparison(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]
        self.assert_code(
            "NO_COMPARISON", self.workflow.page, work_id, view="comparison"
        )
        self.assert_code(
            "INVALID_SCOPE_ARGUMENT",
            self.workflow.page,
            work_id,
            filter_name="added",
        )

    def test_invalid_draft_and_rule_files_use_fixed_errors(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]
        self.assert_code("INVALID_RULES", self.workflow.compare, work_id, None)
        self.assert_code("INVALID_RULES", self.workflow.compare, work_id, [])
        self.assert_code(
            "INVALID_RULES", self.workflow.compare, work_id, {"": ["alpha"]}
        )
        self.assert_code(
            "INVALID_RULES", self.workflow.compare, work_id, {"Cat": "alpha"}
        )
        self.assert_code(
            "INVALID_RULE_REGEX",
            self.workflow.compare,
            work_id,
            {"Cat": ["[synthetic-bad"]},
        )
        self.assertIsNone(self.workset(work_id)["comparison"])

        self.write(ops.RULES, b"{")
        self.assert_code("INVALID_RULES", self.open, "queue")
        self.write(ops.RULES, {"Cat": ["[synthetic-bad"]})
        self.assert_code("INVALID_RULE_REGEX", self.open, "queue")

    def test_rescan_rejects_queue_without_reading_candidates(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]
        before = self.bytes(ops.QUEUE)
        records = self.track("records")
        self.assert_code(
            "UNMATCHED_WORKSET_REQUIRED",
            self.workflow.compare,
            work_id,
            rescan=True,
        )
        self.assertEqual(records, [])
        self.assertEqual(self.bytes(ops.QUEUE), before)
        self.assertIsNone(self.workset(work_id)["comparison"])

    def test_rescan_default_and_draft_are_preview_only(self):
        self.write_rows(
            ops.UNMATCHED,
            [
                self.row("1" * 32, "beta synthetic body"),
                self.row("2" * 32, "alpha synthetic body"),
            ],
        )
        work_id = self.open("unmatched")["work_id"]
        before = {name: self.bytes(name) for name in (ops.UNMATCHED, ops.RULES)}

        default = self.workflow.compare(work_id, rescan=True)
        self.assertEqual(default["comparison"]["changed_count"], 0)
        work = self.workset(work_id)
        self.assertEqual(work["comparison"]["operation"], "rescan")
        self.assertIsNone(work["comparison"]["draft_sha256"])
        self.assertEqual(work["comparison"]["changes"], [])

        draft = {"CatB": ["beta"]}
        drafted = self.workflow.compare(work_id, draft, rescan=True)
        self.assertEqual(drafted["comparison"]["added_count"], 1)
        self.assertEqual(drafted["comparison"]["removed_count"], 1)
        work = self.workset(work_id)
        self.assertEqual(
            work["comparison"]["draft_sha256"], ops.digest(ops.encode(draft))
        )
        data = self.workset_path(work_id).read_bytes()
        self.assertNotIn(b"CatB", data)
        self.assertNotIn(b"beta synthetic body", data)
        self.assertEqual(
            before, {name: self.bytes(name) for name in (ops.UNMATCHED, ops.RULES)}
        )
        added = self.workflow.page(
            work_id, view="comparison", filter_name="added"
        )
        self.assertEqual([item["id"] for item in added["items"]], ["1" * 32])
        removed = self.workflow.page(
            work_id, view="comparison", filter_name="removed"
        )
        self.assertEqual([item["id"] for item in removed["items"]], ["2" * 32])


class CorruptWorksetTests(Fixture):
    def test_invalid_work_ids_and_missing_workset(self):
        for work_id in ("not-an-id", "A" * 32, "a" * 31, "a" * 33, "../" + "a" * 32):
            self.assert_code("INVALID_WORK_ID", self.workflow.stats, work_id)
        self.assert_code("WORKSET_NOT_FOUND", self.workflow.stats, "a" * 32)

    def test_malformed_or_structurally_invalid_workset_fails_closed(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]
        path = self.workset_path(work_id)
        original = self.workset(work_id)

        path.write_bytes(b"{")
        path.chmod(0o600)
        self.assert_code("INVALID_STORED_JSON", self.workflow.stats, work_id)

        self.write(
            f"{workflow.WORKSETS}/{work_id}.json", {"work_id": work_id}
        )
        self.assert_code("INVALID_WORKSET", self.workflow.stats, work_id)

        work = json.loads(json.dumps(original))
        work["work_id"] = "b" * 32
        self.write(f"{workflow.WORKSETS}/{work_id}.json", work)
        self.assert_code("INVALID_WORKSET", self.workflow.stats, work_id)

        work = json.loads(json.dumps(original))
        work["records"][0]["withheld"] = "EXACT_DUPLICATE"
        work["records"][0]["duplicate_of"] = None
        work["records"][0]["category_ids"] = []
        self.write(f"{workflow.WORKSETS}/{work_id}.json", work)
        self.assert_code("INVALID_WORKSET", self.workflow.stats, work_id)


class CliTests(Fixture):
    def test_page_stdout_is_one_utf8_json_line_with_lf(self):
        text = "unicode 记忆 😀 body"
        self.write_rows(ops.QUEUE, [self.row("1" * 32, text)])
        work_id = self.open("queue")["work_id"]
        result = self.cli(
            ["page", "--work-id", work_id, "--limit", "1"]
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith(b"\n"))
        self.assertEqual(result.stdout.count(b"\n"), 1)
        self.assertLessEqual(len(result.stdout), workflow.PAGE_BYTES)
        self.assertEqual(
            json.loads(result.stdout.decode("utf-8"))["items"][0]["text"], text
        )

    def test_cli_fixed_errors_never_echo_payload(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]

        result = self.cli(
            ["compare-rules", "--work-id", work_id], b'{"secret-synthetic":'
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout), {"ok": False, "error": "INVALID_JSON"}
        )
        self.assertNotIn(b"secret-synthetic", result.stdout + result.stderr)

        result = self.cli(
            ["compare-rules", "--work-id", work_id],
            b'{"SecretCategory": ["[secret-synthetic"]}',
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout),
            {"ok": False, "error": "INVALID_RULE_REGEX"},
        )
        self.assertNotIn(b"SecretCategory", result.stdout + result.stderr)
        self.assertNotIn(b"secret-synthetic", result.stdout + result.stderr)

    def test_cli_argument_scope_and_rescan_errors(self):
        self.write_rows(ops.QUEUE, [self.row("1" * 32, "alpha body")])
        work_id = self.open("queue")["work_id"]

        result = self.cli(["stats", "--work-id", "not-an-id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout), {"ok": False, "error": "INVALID_WORK_ID"}
        )

        result = self.cli(
            ["page", "--work-id", work_id, "--view", "not-a-view"]
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout), {"ok": False, "error": "INVALID_ARGUMENT"}
        )

        result = self.cli(["rescan", "--work-id", work_id])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout),
            {"ok": False, "error": "UNMATCHED_WORKSET_REQUIRED"},
        )


if __name__ == "__main__":
    unittest.main()
