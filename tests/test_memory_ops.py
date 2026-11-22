"""Synthetic-only backend tests. All runtime paths are beneath /tmp/opencode."""

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
MODULE = Path(__file__).absolute().parents[1] / "memory_ops.py"
SPEC = importlib.util.spec_from_file_location("memory_ops_under_test", MODULE)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="memory-test-", dir="/tmp/opencode"
        )
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir(mode=0o700)
        self.memory = ops.Memory(self.root)
        self.write(
            ops.RULES, {"Remember": ["remember:", "我是.*学生"], "Short": ["好"]}
        )
        self.write(ops.PUBLIC, "# Public\n## Common\nsynthetic-public\n")
        self.write(
            ops.PRIVATE,
            "# Private\n## Password\nsynthetic-one\n## Other\nsynthetic-neighbor\n",
        )
        self.write(f"{ops.PROJECTS}/项目甲.md", "# Synthetic project\n")

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

    def rows(self, name):
        return ops.records(self.root / name)

    def event(self, text="remember: synthetic", message="m1", session="s1"):
        return ops.capture(
            self.root, {"session_id": session, "message_id": message, "text": text}
        )

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(ops.OpError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def prepare(self, ids=(), writes=None, **kwargs):
        if writes is None:
            writes = [
                {
                    "path": ops.PUBLIC,
                    "before_sha256": ops.file_hash(self.root / ops.PUBLIC),
                    "content": "# Public\nupdated\n",
                }
            ]
        return self.memory.prepare(
            {"processed_ids": list(ids), "writes": writes}, **kwargs
        )["txid"]

    def cli(self, script, arguments, payload=""):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(MODULE.parent / script),
                "--root",
                str(self.root),
                *arguments,
            ],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )


class CaptureTests(Fixture):
    def test_stable_event_id_and_new_event_same_text(self):
        first = self.event()
        self.assertEqual(self.event()["status"], "duplicate")
        second = self.event(message="m2")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(first["id"]), 64)
        self.assertEqual(len(self.rows(ops.QUEUE)), 2)

    def test_missing_message_uses_uuid_each_time(self):
        self.assertNotEqual(
            self.event(message=None)["id"], self.event(message=None)["id"]
        )

    def test_short_empty_and_long_rule_match(self):
        self.assertEqual(self.event("好")["status"], "queued")
        self.assertEqual(self.event("  ", "empty")["status"], "empty")
        self.assertEqual(
            self.event("x" * 5000 + "remember:", "long")["status"], "queued"
        )

    def test_regex_and_case_insensitive_keyword(self):
        self.assertEqual(self.event("我是数学学生")["status"], "queued")
        self.assertEqual(self.event("REMEMBER: yes", "m2")["status"], "queued")

    def test_paused_marker_writes_nothing_at_all(self):
        (self.root / ops.PAUSED).touch(mode=0o600)
        self.assertEqual(self.event("remember: while paused"), {"ok": True, "status": "paused"})
        self.assertEqual(self.rows(ops.QUEUE), [])
        # A paused capture must not even take the lock: no trace of the text.
        self.assertFalse((self.root / "learning/.memory.lock").exists())

    def test_paused_marker_also_blocks_the_unmatched_log(self):
        # unmatched.jsonl is the privacy-sensitive path: it stores everything
        # that did NOT match a rule, so pausing has to stop it too.
        (self.root / ops.PAUSED).touch(mode=0o600)
        self.assertEqual(self.event("no rule matches this sentence")["status"], "paused")
        self.assertEqual(self.rows(ops.UNMATCHED), [])

    def test_capture_resumes_once_marker_removed(self):
        (self.root / ops.PAUSED).touch(mode=0o600)
        self.assertEqual(self.event("remember: paused")["status"], "paused")
        (self.root / ops.PAUSED).unlink()
        self.assertEqual(self.event("remember: resumed", "m2")["status"], "queued")
        self.assertEqual(len(self.rows(ops.QUEUE)), 1)

    def test_status_reports_whether_capture_is_paused(self):
        self.assertFalse(self.memory.status()["capture_paused"])
        (self.root / ops.PAUSED).touch(mode=0o600)
        self.assertTrue(self.memory.status()["capture_paused"])

    def test_invalid_regex_has_safe_error(self):
        self.write(ops.RULES, {"secret-category": ["[secret-pattern"]})
        result = self.cli(
            "capture.py",
            ["--event-json"],
            json.dumps(
                {"session_id": "s", "message_id": "m", "text": "private-payload"}
            ),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"], "INVALID_RULE_REGEX")
        self.assertNotIn("secret", result.stdout + result.stderr)
        self.assertNotIn("private-payload", result.stdout + result.stderr)

    def test_event_cli_and_plaintext_legacy(self):
        event = {"session_id": "s", "message_id": None, "text": "remember: event"}
        self.assertEqual(
            self.cli("capture.py", ["--event-json"], json.dumps(event)).returncode, 0
        )
        self.assertEqual(self.cli("capture.py", [], "remember: legacy").returncode, 0)
        self.assertEqual(self.cli("capture.py", ["remember: positional"]).returncode, 0)
        self.assertEqual(len(self.rows(ops.QUEUE)), 3)

    def test_invalid_event_and_argument_never_echo_payload(self):
        for args, payload in [(["--secret-value"], ""), (["--event-json"], '{"text":')]:
            result = self.cli("capture.py", args, payload)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("secret-value", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_queue_count_cap_never_overflows_to_unmatched(self):
        with mock.patch.object(ops, "QUEUE_COUNT", 2):
            self.event(message="m1")
            self.event(message="m2")
            before = (self.root / ops.QUEUE).read_bytes()
            self.assert_code("QUEUE_FULL", self.event, message="m3")
        self.assertEqual((self.root / ops.QUEUE).read_bytes(), before)
        self.assertEqual(self.rows(ops.UNMATCHED), [])
        self.assertEqual(self.memory.status()["rejected_candidates"], 1)

    def test_queue_encoded_byte_cap(self):
        with mock.patch.object(ops, "QUEUE_BYTES", 128):
            self.assert_code("QUEUE_FULL", self.event)
        self.assertEqual(self.rows(ops.QUEUE), [])

    def test_matched_64k_limit_safe_metadata(self):
        self.assert_code("CANDIDATE_TOO_LARGE", self.event, "remember:" + "假" * 22000)
        self.assertEqual(self.rows(ops.UNMATCHED), [])
        self.assertNotIn("假", (self.root / ops.DIAGNOSTICS).read_text())

    def test_queue_warning_80_200(self):
        for number in range(200):
            result = self.event(message=str(number))
            if number == 79:
                self.assertEqual(result["warning_threshold"], 80)
        self.assertEqual(result["warning_threshold"], 200)

    def test_unmatched_utf8_limit_and_marker(self):
        self.event("猫" * 10000)
        row = self.rows(ops.UNMATCHED)[0]
        self.assertLessEqual(len(row["text"].encode()), 16 * 1024)
        self.assertTrue(row["text"].endswith("[truncated]"))
        self.assertEqual(row["original_bytes"], 30000)

    def test_unmatched_age_count_bytes_oldest_pruning(self):
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.write(
            ops.UNMATCHED,
            ops.rows_bytes([{"id": "a" * 32, "time": old, "text": "old"}]),
        )
        with (
            mock.patch.object(ops, "UNMATCHED_COUNT", 2),
            mock.patch.object(ops, "UNMATCHED_BYTES", 600),
        ):
            for number in range(4):
                self.event("unmatched " + str(number), str(number))
        rows = self.rows(ops.UNMATCHED)
        self.assertEqual([row["text"] for row in rows], ["unmatched 2", "unmatched 3"])
        self.assertLessEqual(len(ops.rows_bytes(rows)), 600)

    def test_symlink_root_parent_file_and_special_file_rejected(self):
        link = self.base / "link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assert_code("UNSAFE_PATH", ops.Memory, link)
        queue = self.root / ops.QUEUE
        queue.symlink_to(self.root / ops.PUBLIC)
        self.assert_code("UNSAFE_PATH", self.event)
        queue.unlink()
        os.mkfifo(queue)
        self.assert_code("UNSAFE_PATH", self.event)
        self.assert_code("UNSAFE_PATH", ops.Memory, self.root / ".." / "source")

    def test_fixed_lock_busy_and_inode_survives_replace(self):
        with self.memory.lock():
            self.assert_code("BUSY", self.event)
        inode = (self.root / "learning/.memory.lock").stat().st_ino
        self.event()
        self.event(message="m2")
        self.assertEqual((self.root / "learning/.memory.lock").stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE((self.root / ops.QUEUE).stat().st_mode), 0o600)


class TransactionTests(Fixture):
    def test_prepare_confirmation_commit_and_retry(self):
        event_id = self.event()["id"]
        before = (self.root / ops.PUBLIC).read_bytes()
        txid = self.prepare([event_id])
        self.assertEqual((self.root / ops.PUBLIC).read_bytes(), before)
        self.assert_code("APPROVAL_REQUIRED", self.memory.commit, txid, " ")
        self.assert_code("UNCONFIRMED_REVIEW", self.memory.commit, txid, recover=True)
        self.memory.commit(txid, "user-message-confirmed")
        self.assertEqual(self.rows(ops.QUEUE), [])
        self.assertEqual(len(self.rows(ops.ARCHIVE)), 1)
        self.assertEqual(
            self.memory.commit(txid, "user-message-confirmed")["status"],
            "already_committed",
        )
        self.assertEqual(self.event()["status"], "duplicate")
        confirmation = ops.json_read(self.root / ops.CONFIRMATION)
        self.assertEqual(confirmation["manifest"]["processed_ids"], [event_id])
        self.assertEqual(confirmation["result"], "committed")

    def test_new_entries_preserved_during_review(self):
        event_id = self.event()["id"]
        txid = self.prepare([event_id])
        new = self.event(message="m2")["id"]
        self.memory.commit(txid, "confirmed")
        self.assertEqual([row["id"] for row in self.rows(ops.QUEUE)], [new])

    def test_stale_version_rejected_and_confirmation_is_durable(self):
        txid = self.prepare()
        self.write(ops.PUBLIC, "hand-edited")
        self.assert_code("VERSION_CONFLICT", self.memory.commit, txid, "confirmed")
        self.assertEqual((self.root / ops.PUBLIC).read_text(), "hand-edited")
        self.assertTrue(self.memory.pending()["confirmation"])
        self.assertTrue(self.memory.status()["pending_failed"])
        self.assert_code("VERSION_CONFLICT", self.memory.commit, txid, recover=True)

    def test_prepare_stale_hash_does_not_create_pending(self):
        writes = [{"path": ops.PUBLIC, "before_sha256": "0" * 64, "content": "new"}]
        self.assert_code("VERSION_CONFLICT", self.prepare, writes=writes)
        self.assertIsNone(self.memory.pending())

    def test_only_one_pending_and_status_contains_no_content(self):
        txid = self.prepare()
        self.assert_code("PENDING_REVIEW", self.prepare)
        status = self.memory.status()
        self.assertEqual(status["pending"], {"txid": txid, "confirmed": False})
        self.assertNotIn("updated", json.dumps(status))
        self.assert_code("PENDING_REVIEW", self.memory.maintain)
        self.assertFalse(self.memory.pending()["confirmation"])

    def test_recovery_after_each_atomic_replace_boundary(self):
        # A write is replaced and durable, then its caller is interrupted.
        for boundary in (ops.PUBLIC, ops.ARCHIVE, ops.QUEUE, ops.CONFIRMATION):
            with self.subTest(boundary=boundary):
                event_id = self.event(message=boundary)["id"]
                txid = self.prepare(
                    [event_id],
                    writes=[
                        {
                            "path": ops.PUBLIC,
                            "before_sha256": ops.file_hash(self.root / ops.PUBLIC),
                            "content": "# Updated\n" + boundary,
                        }
                    ],
                )
                original = ops.atomic_write
                failed = False

                def interrupt(path, data):
                    nonlocal failed
                    original(path, data)
                    if path == self.root / boundary and not failed:
                        failed = True
                        raise OSError("synthetic-secret-error")

                with mock.patch.object(ops, "atomic_write", interrupt):
                    self.assert_code(
                        "COMMIT_IO_ERROR", self.memory.commit, txid, "confirmed"
                    )
                self.assertTrue(self.memory.pending()["confirmation"])
                self.memory.commit(txid, recover=True)
                self.assertIsNone(self.memory.pending())
                self.assertEqual(self.rows(ops.QUEUE), [])
                ids = [row["id"] for row in self.rows(ops.ARCHIVE)]
                self.assertEqual(ids.count(event_id), 1)

    def test_recovery_after_confirmation_before_any_formal_write(self):
        txid = self.prepare()
        original = ops.atomic_write

        def interrupt(path, data):
            if path == self.root / ops.PUBLIC:
                raise OSError("synthetic")
            return original(path, data)

        with mock.patch.object(ops, "atomic_write", interrupt):
            self.assert_code("COMMIT_IO_ERROR", self.memory.commit, txid, "yes")
        self.memory.commit(txid, recover=True)
        self.assertEqual((self.root / ops.PUBLIC).read_text(), "# Public\nupdated\n")

    def test_post_unlink_fsync_failure_clears_only_on_same_txid_retry(self):
        for recover in (False, True):
            with self.subTest(recover=recover):
                event_id = self.event(message=f"post-unlink-{recover}")["id"]
                txid = self.prepare([event_id])
                pending = self.root / ops.PENDING
                original = ops.fsync_dir
                failed = False

                def interrupt(path):
                    nonlocal failed
                    if path == pending.parent and not pending.exists():
                        failed = True
                        raise OSError("synthetic post-unlink fsync failure")
                    return original(path)

                with mock.patch.object(ops, "fsync_dir", interrupt):
                    self.assert_code(
                        "COMMIT_IO_ERROR", self.memory.commit, txid, "confirmed"
                    )
                self.assertTrue(failed)
                self.assertIsNone(self.memory.pending())
                self.assertTrue(self.memory.status()["pending_failed"])
                self.assertEqual(
                    self.memory.state()["pending_failure"], "COMMIT_IO_ERROR"
                )
                confirmation = ops.json_read(self.root / ops.CONFIRMATION)
                self.assertEqual(confirmation["manifest"]["txid"], txid)
                self.assertEqual(confirmation["result"], "committed")
                paths = (
                    ops.STATE,
                    ops.CONFIRMATION,
                    ops.PUBLIC,
                    ops.QUEUE,
                    ops.ARCHIVE,
                )
                before = {name: (self.root / name).read_bytes() for name in paths}
                other_txid = "a" * 32 if txid != "a" * 32 else "b" * 32
                self.assert_code(
                    "NO_PENDING_REVIEW", self.memory.commit, other_txid, recover=True
                )
                self.assertEqual(
                    {name: (self.root / name).read_bytes() for name in paths}, before
                )
                self.assertTrue(self.memory.status()["pending_failed"])

                result = self.memory.commit(txid, "confirmed", recover=recover)
                self.assertEqual(result["status"], "already_committed")
                reopened = ops.Memory(self.root)
                self.assertFalse(reopened.status()["pending_failed"])
                state = reopened.state()
                self.assertIsNone(state["pending_status"])
                self.assertIsNone(state["pending_failure"])
                self.assertEqual(state["last_txid"], txid)
                for name in paths[1:]:
                    self.assertEqual((self.root / name).read_bytes(), before[name])
                self.assertEqual(self.rows(ops.QUEUE), [])
                self.assertEqual(
                    [row["id"] for row in self.rows(ops.ARCHIVE)].count(event_id), 1
                )

    def test_previous_txid_retry_does_not_clear_live_pending_failure(self):
        previous_txid = self.prepare()
        self.memory.commit(previous_txid, "confirmed")
        current_txid = self.prepare()
        self.assertNotEqual(current_txid, previous_txid)
        self.assert_code("PENDING_REVIEW", self.memory.maintain)
        before = {
            name: (self.root / name).read_bytes() for name in (ops.STATE, ops.PENDING)
        }
        self.assert_code(
            "TXID_MISMATCH", self.memory.commit, previous_txid, recover=True
        )
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertTrue(self.memory.status()["pending_failed"])
        self.assertEqual(self.memory.pending()["txid"], current_txid)

    def test_recovery_rejects_unknown_second_file_before_applying_first(self):
        writes = [
            {
                "path": name,
                "before_sha256": ops.file_hash(self.root / name),
                "content": "# Synthetic update\n",
            }
            for name in (ops.PUBLIC, f"{ops.PROJECTS}/项目甲.md")
        ]
        txid = self.prepare(writes=writes)
        self.write(writes[1]["path"], "external change")
        self.assert_code("VERSION_CONFLICT", self.memory.commit, txid, "yes")
        self.assertIn("synthetic-public", (self.root / ops.PUBLIC).read_text())

    def test_confirmation_manifest_tampering_rejected(self):
        txid = self.prepare()
        self.write(ops.PUBLIC, "external change")
        self.assert_code("VERSION_CONFLICT", self.memory.commit, txid, "yes")
        tx = self.memory.pending()
        tx["confirmation"]["manifest"]["processed_ids"] = ["a" * 32]
        self.write(ops.PENDING, tx)
        self.assert_code(
            "CONFIRMATION_MISMATCH", self.memory.commit, txid, recover=True
        )

    def test_legacy_ids_only_migrate_explicitly(self):
        self.write(ops.QUEUE, ops.rows_bytes([{"time": ops.now(), "text": "legacy"}]))
        self.event()
        self.assertNotIn("id", self.rows(ops.QUEUE)[0])
        snapshot = self.memory.snapshot()
        first = snapshot["records"][0]["id"]
        self.assertEqual(self.memory.snapshot()["records"][0]["id"], first)
        txid = self.prepare([first], writes=[])
        self.memory.commit(txid, "yes")
        self.assertEqual(self.rows(ops.ARCHIVE)[0]["id"], first)

    def test_rules_transaction_validates_and_clears_only_reviewed(self):
        reviewed = self.event("unmatched one")["id"]
        writes = [
            {
                "path": ops.RULES,
                "before_sha256": ops.file_hash(self.root / ops.RULES),
                "content": json.dumps({"Remember": ["newkeyword"]}),
            }
        ]
        txid = self.memory.prepare(
            {"writes": writes, "reviewed_unmatched_ids": [reviewed]}
        )["txid"]
        new = self.event("unmatched two", "m2")["id"]
        self.memory.commit(txid, "rule-review-confirmation")
        self.assertEqual([row["id"] for row in self.rows(ops.UNMATCHED)], [new])
        self.assertEqual(self.event("newkeyword", "m3")["status"], "queued")

    def test_invalid_rule_transaction_does_not_stage(self):
        writes = [
            {
                "path": ops.RULES,
                "before_sha256": ops.file_hash(self.root / ops.RULES),
                "content": '{"x":["("]}',
            }
        ]
        self.assert_code("INVALID_RULE_REGEX", self.prepare, writes=writes)
        self.assertIsNone(self.memory.pending())

    def test_clear_unmatched_only_explicit_ids(self):
        first = self.event("one")["id"]
        second = self.event("two", "m2")["id"]
        self.assert_code("APPROVAL_REQUIRED", self.memory.clear_unmatched, [first], "")
        self.memory.clear_unmatched([first], "reviewed")
        self.assertEqual([row["id"] for row in self.rows(ops.UNMATCHED)], [second])

    def test_private_write_requires_flag_and_single_scope(self):
        writes = [
            {
                "path": ops.PRIVATE,
                "before_sha256": ops.file_hash(self.root / ops.PRIVATE),
                "content": "## Password\nreplacement\n",
            }
        ]
        self.assert_code("PATH_NOT_ALLOWED", self.prepare, writes=writes)
        self.assert_code(
            "PRIVATE_SCOPE_REQUIRED", self.prepare, writes=writes, allow_private=True
        )
        txid = self.prepare(
            writes=writes, allow_private=True, approved_scope="Password"
        )
        self.memory.commit(txid, "explicit-private-write-confirmed")
        self.assertEqual(
            (self.root / ops.PRIVATE).read_text(),
            "# Private\n## Password\nreplacement\n## Other\nsynthetic-neighbor\n",
        )

    def test_private_scope_pseudo_fence_fails_closed_without_pending(self):
        # Regression: a 4-space/tab pseudo-fence made the approved scope's range
        # swallow the next heading, so staging deleted the neighbor field.
        for fence_line in ("    ```\n", "\t```\n"):
            self.write(
                ops.PRIVATE,
                f"# Private\n## Password\nsecret\n{fence_line}## Other\nneighbor\n```\n",
            )
            writes = [
                {
                    "path": ops.PRIVATE,
                    "before_sha256": ops.file_hash(self.root / ops.PRIVATE),
                    "content": "## Password\nreplacement\n",
                }
            ]
            with self.subTest(fence_line=fence_line):
                self.assert_code(
                    "AMBIGUOUS_MARKDOWN",
                    self.prepare,
                    writes=writes,
                    allow_private=True,
                    approved_scope="Password",
                )
                self.assertIsNone(self.memory.pending())
                self.assertIn(
                    "## Other\nneighbor", (self.root / ops.PRIVATE).read_text()
                )

    def test_private_scope_pseudo_fence_is_plain_text_preserves_neighbor_bytes(self):
        for fence_line in ("    ```\n", "\t```\n"):
            self.write(
                ops.PRIVATE,
                f"# Private\n## Password\n{fence_line}fake\n## Other\nneighbor\n",
            )
            writes = [
                {
                    "path": ops.PRIVATE,
                    "before_sha256": ops.file_hash(self.root / ops.PRIVATE),
                    "content": "## Password\nreplacement\n",
                }
            ]
            with self.subTest(fence_line=fence_line):
                txid = self.prepare(
                    writes=writes, allow_private=True, approved_scope="Password"
                )
                self.memory.commit(txid, "explicit-private-write-confirmed")
                self.assertEqual(
                    (self.root / ops.PRIVATE).read_text(),
                    "# Private\n## Password\nreplacement\n## Other\nneighbor\n",
                )

    def test_path_whitelist_for_writes(self):
        for name in (
            "../escape",
            "learning/unmatched.jsonl",
            "user-profile/projects/../private.md",
            "user-profile/projects/a/b.md",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ops.OpError):
                    self.prepare(
                        writes=[{"path": name, "before_sha256": None, "content": "x"}]
                    )


class LegacyMigrationTests(Fixture):
    def legacy(self, **changes):
        return {
            "id": "0123456789abcdef",
            "time": "2026-01-01T00:00:00+00:00",
            "text": "remember: synthetic legacy",
            "rules": ["Remember"],
            **changes,
        }

    def test_short_id_snapshot_prepare_commit_and_rerun(self):
        original = self.legacy()
        self.write(ops.QUEUE, ops.rows_bytes([original]))
        snapshot = self.memory.snapshot()
        row = self.rows(ops.QUEUE)[0]
        self.assertEqual(len(row["id"]), 64)
        self.assertEqual(row["legacy_id"], original["id"])
        self.assertEqual(
            {k: v for k, v in row.items() if k not in ("id", "legacy_id")},
            {k: v for k, v in original.items() if k != "id"},
        )
        self.assertEqual(snapshot["records"][0]["id"], row["id"])
        txid = self.prepare([row["id"]], writes=[])
        self.memory.commit(txid, "synthetic-review-approved")
        self.assertEqual(self.memory.snapshot()["records"], [])
        before = (self.root / ops.ARCHIVE).read_bytes()
        self.assertEqual(
            self.memory.commit(txid, "same-approval")["status"], "already_committed"
        )
        self.assertEqual((self.root / ops.ARCHIVE).read_bytes(), before)
        # Reintroduced legacy bytes deterministically map to the archived identity.
        self.write(ops.QUEUE, ops.rows_bytes([original]))
        self.assertEqual(self.memory.snapshot()["records"][0]["id"], row["id"])
        self.memory.commit(self.prepare([row["id"]], writes=[]), "review-replay")
        self.assertEqual(len(self.rows(ops.ARCHIVE)), 1)

    def test_queue_and_archive_mapping_ignores_archived_at(self):
        original = self.legacy()
        self.write(ops.QUEUE, ops.rows_bytes([original]))
        self.write(
            ops.ARCHIVE, ops.rows_bytes([{**original, "archived_at": ops.now()}])
        )
        row_id = self.memory.snapshot()["records"][0]["id"]
        self.assertEqual(self.rows(ops.ARCHIVE)[0]["id"], row_id)
        self.memory.commit(self.prepare([row_id], writes=[]), "yes")
        self.assertEqual(len(self.rows(ops.ARCHIVE)), 1)
        self.assertEqual(self.rows(ops.QUEUE), [])

    def test_same_short_hash_and_text_different_times_remain_distinct(self):
        rows = [self.legacy(), self.legacy(time="2026-01-02T00:00:00+00:00")]
        self.write(ops.QUEUE, ops.rows_bytes(rows))
        ids = [row["id"] for row in self.memory.snapshot()["records"]]
        self.assertEqual(len(set(ids)), 2)
        self.memory.commit(self.prepare(ids, writes=[]), "yes")
        self.assertEqual(len(self.rows(ops.ARCHIVE)), 2)
        self.assertEqual(
            [row["time"] for row in self.rows(ops.ARCHIVE)], [r["time"] for r in rows]
        )

    def test_short_id_collision_with_different_text_is_not_merged(self):
        self.write(
            ops.QUEUE, ops.rows_bytes([self.legacy(), self.legacy(text="different")])
        )
        ids = [row["id"] for row in self.memory.snapshot()["records"]]
        self.assertEqual(len(set(ids)), 2)

    def test_ambiguous_duplicate_legacy_rows_fail_without_rewrite(self):
        before = ops.rows_bytes([self.legacy(), self.legacy()])
        self.write(ops.QUEUE, before)
        self.assert_code("DUPLICATE_STORED_ID", self.memory.snapshot)
        self.assertEqual((self.root / ops.QUEUE).read_bytes(), before)

    def test_malformed_archive_id_prevents_all_migration_writes(self):
        queue = ops.rows_bytes([self.legacy()])
        self.write(ops.QUEUE, queue)
        for bad_id in ("not-an-id", "a" * 15, "a" * 17, 123, None):
            with self.subTest(bad_id=bad_id):
                archive = ops.rows_bytes([self.legacy(id=bad_id)])
                self.write(ops.ARCHIVE, archive)
                self.assert_code("INVALID_IDS", self.memory.snapshot)
                self.assert_code("INVALID_IDS", self.prepare)
                self.assertEqual((self.root / ops.QUEUE).read_bytes(), queue)
                self.assertEqual((self.root / ops.ARCHIVE).read_bytes(), archive)
                self.assertIsNone(self.memory.pending())

    def test_short_id_missing_identity_refuses_to_invent_context(self):
        for missing in ("time", "text"):
            row = self.legacy()
            row.pop(missing)
            before = ops.rows_bytes([row])
            self.write(ops.QUEUE, before)
            self.assert_code("LEGACY_IDENTITY_REQUIRED", self.memory.snapshot)
            self.assertEqual((self.root / ops.QUEUE).read_bytes(), before)

    def test_metadata_only_cli_migrates_archive_without_exposing_content(self):
        self.write(
            ops.QUEUE, ops.rows_bytes([self.legacy(text="synthetic-queue-secret")])
        )
        self.write(
            ops.ARCHIVE, ops.rows_bytes([self.legacy(text="synthetic-archive-secret")])
        )
        result = self.cli("memory_ops.py", ["snapshot", "--metadata-only"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "ok": True,
                "scope": "queue",
                "record_count": 1,
                "archive_count": 1,
            },
        )
        self.assertNotIn("secret", result.stdout + result.stderr)
        self.assertEqual(len(self.rows(ops.ARCHIVE)[0]["id"]), 64)
        before = (self.root / ops.ARCHIVE).read_bytes()
        self.memory.snapshot(metadata_only=True)
        self.assertEqual((self.root / ops.ARCHIVE).read_bytes(), before)
        self.assert_code(
            "INVALID_SCOPE_ARGUMENT",
            self.memory.snapshot,
            public=True,
            metadata_only=True,
        )

    def test_prepare_normalizes_archive_without_archive_snapshot_output(self):
        self.write(ops.ARCHIVE, ops.rows_bytes([self.legacy()]))
        event_id = self.event()["id"]
        txid = self.prepare([event_id], writes=[])
        self.assertEqual(len(self.rows(ops.ARCHIVE)[0]["id"]), 64)
        self.memory.commit(txid, "yes")
        self.assertEqual(len(self.rows(ops.ARCHIVE)), 2)

    def test_pending_blocks_all_identity_migration(self):
        txid = self.prepare()
        self.write(ops.QUEUE, ops.rows_bytes([self.legacy()]))
        self.write(ops.ARCHIVE, ops.rows_bytes([self.legacy()]))
        before = {
            name: (self.root / name).read_bytes()
            for name in (ops.QUEUE, ops.ARCHIVE, ops.PENDING)
        }
        self.assert_code("PENDING_REVIEW", self.memory.snapshot, metadata_only=True)
        self.assert_code("PENDING_REVIEW", self.prepare)
        self.assert_code("PENDING_REVIEW", self.memory.migrate, ops.ARCHIVE)
        self.assertEqual(self.memory.pending()["txid"], txid)
        self.assertEqual(
            before, {name: (self.root / name).read_bytes() for name in before}
        )

    def test_maintenance_requires_explicit_migration_before_expiring_legacy(self):
        self.write(ops.ARCHIVE, ops.rows_bytes([self.legacy()]))
        before = (self.root / ops.ARCHIVE).read_bytes()
        self.assert_code("LEGACY_REQUIRES_SNAPSHOT", self.memory.maintain)
        self.assertEqual((self.root / ops.ARCHIVE).read_bytes(), before)
        self.memory.snapshot(metadata_only=True)
        migrated = self.rows(ops.ARCHIVE)[0]
        self.memory.maintain()
        self.memory.maintain()
        self.assertEqual(
            self.rows(ops.ARCHIVE),
            [
                {
                    "id": migrated["id"],
                    "legacy_id": self.legacy()["id"],
                    "raw_expired": True,
                }
            ],
        )

    def test_already_expired_short_tombstone_is_preserved_not_event_identity(self):
        tombstone = {"id": self.legacy()["id"], "raw_expired": True}
        self.write(ops.ARCHIVE, ops.rows_bytes([tombstone]))
        # Old text-only processed hashes do not prove a new source event processed.
        self.write(
            "learning/processed_hashes.json",
            json.dumps([ops.digest(self.legacy()["text"].encode())[:16]]),
        )
        event_id = self.event(self.legacy()["text"])["id"]
        self.memory.snapshot(metadata_only=True)
        self.assertEqual(self.rows(ops.ARCHIVE), [tombstone])
        self.memory.commit(self.prepare([event_id], writes=[]), "yes")
        self.memory.maintain()
        self.assertEqual(self.rows(ops.ARCHIVE)[0], tombstone)
        self.assertEqual(self.event(self.legacy()["text"])["status"], "duplicate")
        self.assertEqual(
            self.event(self.legacy()["text"], "new-event")["status"], "queued"
        )

    def test_capture_keeps_missing_and_short_ids_unmigrated(self):
        missing = {"time": ops.now(), "text": "remember: missing-id"}
        self.write(ops.QUEUE, ops.rows_bytes([missing, self.legacy()]))
        self.write(ops.ARCHIVE, ops.rows_bytes([self.legacy()]))
        self.assertEqual(self.event()["status"], "queued")
        self.assertEqual(self.rows(ops.QUEUE)[:2], [missing, self.legacy()])
        self.assertEqual(self.rows(ops.ARCHIVE), [self.legacy()])

    def test_canonical_ids_unchanged_and_unmatched_migration_uses_same_identity(self):
        canonical = [self.legacy(id="a" * 32), self.legacy(id="b" * 64)]
        self.write(ops.QUEUE, ops.rows_bytes(canonical))
        self.memory.snapshot()
        self.assertEqual(self.rows(ops.QUEUE), canonical)
        self.write(ops.UNMATCHED, ops.rows_bytes([self.legacy()]))
        self.write(ops.ARCHIVE, ops.rows_bytes([self.legacy()]))
        self.assert_code(
            "LEGACY_REQUIRES_SNAPSHOT", self.memory.clear_unmatched, [], "yes"
        )
        row_id = self.memory.snapshot(source="unmatched")["records"][0]["id"]
        self.assertEqual(self.rows(ops.ARCHIVE)[0]["id"], row_id)
        self.memory.clear_unmatched([row_id], "reviewed")
        self.assertEqual(self.rows(ops.UNMATCHED), [])

    def test_interrupted_cross_file_migration_restarts_consistently(self):
        self.write(ops.QUEUE, ops.rows_bytes([self.legacy()]))
        self.write(ops.ARCHIVE, ops.rows_bytes([self.legacy()]))
        original = ops.atomic_write

        def interrupt(path, data):
            if path == self.root / ops.ARCHIVE:
                raise OSError("synthetic migration interruption")
            original(path, data)

        with (
            mock.patch.object(ops, "atomic_write", interrupt),
            self.assertRaises(OSError),
        ):
            self.memory.snapshot(metadata_only=True)
        migrated = self.rows(ops.QUEUE)[0]["id"]
        self.assertEqual(self.rows(ops.ARCHIVE)[0]["id"], self.legacy()["id"])
        self.memory.snapshot(metadata_only=True)
        self.assertEqual(self.rows(ops.ARCHIVE)[0]["id"], migrated)


class ReferenceTests(Fixture):
    def test_snapshot_redacts_obvious_credentials(self):
        self.event("remember: api_key=synthetic-secret")
        self.event("remember: normal preference", "m2")
        self.event("remember: 密码：synthetic-password", "m3")
        self.event('remember: {"api_key":"synthetic-json-secret"}', "m4")
        result = self.memory.snapshot(public=True)
        text = json.dumps(result)
        self.assertNotIn("synthetic-secret", text)
        self.assertNotIn("synthetic-password", text)
        self.assertNotIn("synthetic-json-secret", text)
        self.assertNotIn("synthetic-neighbor", text)
        self.assertEqual(result["redacted_count"], 3)
        self.assertIn("normal preference", text)
        self.assertIn(ops.PUBLIC, result["files"])

    def test_public_only_does_not_open_private_or_projects(self):
        original = ops.read_bytes

        def guard(path, *args, **kwargs):
            if (
                str(path).startswith(str(self.root / ops.PROJECTS))
                or path == self.root / ops.PRIVATE
            ):
                self.fail("unauthorized body read")
            return original(path, *args, **kwargs)

        with mock.patch.object(ops, "read_bytes", guard):
            self.assertIn(
                "synthetic-public", self.memory.reference("public")["content"]
            )
            self.memory.snapshot(public=True)
            self.assertEqual(self.memory.reference("topics")["topics"], ["项目甲"])

    def test_topic_chinese_name_and_approval(self):
        self.assert_code(
            "APPROVAL_REQUIRED", self.memory.reference, "topic", topic="项目甲"
        )
        self.assertIn(
            "Synthetic project",
            self.memory.reference("topic", topic="项目甲", approval_id="chosen-topic")[
                "content"
            ],
        )
        self.assert_code(
            "INVALID_TOPIC",
            self.memory.reference,
            "topic",
            topic="../private",
            approval_id="yes",
        )

    def test_private_exact_field_no_neighbor(self):
        result = self.memory.reference(
            "private",
            field="Password",
            purpose="synthetic task",
            approval_id="field-approved",
        )
        self.assertEqual(result["content"], "## Password\nsynthetic-one\n")
        self.assertNotIn("neighbor", json.dumps(result))
        self.assertEqual(result["file_sha256"], ops.file_hash(self.root / ops.PRIVATE))

    def test_private_missing_approval_purpose_or_field(self):
        self.assert_code(
            "APPROVAL_REQUIRED",
            self.memory.reference,
            "private",
            field="Password",
            purpose="task",
        )
        self.assert_code(
            "PURPOSE_REQUIRED",
            self.memory.reference,
            "private",
            field="Password",
            approval_id="yes",
        )
        self.assert_code(
            "FIELD_REQUIRED",
            self.memory.reference,
            "private",
            purpose="task",
            approval_id="yes",
        )

    def test_private_unknown_duplicate_malformed_no_fallback(self):
        cases = [
            ("# Private\n## Known\nx", "UNKNOWN_FIELD"),
            ("## Password\none\n## Password\ntwo", "AMBIGUOUS_MARKDOWN"),
            ("##Password\nsecret", "AMBIGUOUS_MARKDOWN"),
            ("## Password\nx\nField\n===\ny", "AMBIGUOUS_MARKDOWN"),
            ("## Password\n```\nx", "AMBIGUOUS_MARKDOWN"),
        ]
        for text, code in cases:
            with self.subTest(code=code, text=text):
                self.write(ops.PRIVATE, text)
                self.assert_code(
                    code,
                    self.memory.reference,
                    "private",
                    field="Password",
                    purpose="test",
                    approval_id="yes",
                )

    def test_section_stops_at_child_heading_and_handles_fenced_heading(self):
        self.write(
            ops.PRIVATE,
            "## Password\nvalue\n```text\n# literal\n```\n### Child\nneighbor",
        )
        result = self.memory.reference(
            "private", field="Password", purpose="test", approval_id="yes"
        )
        self.assertIn("# literal", result["content"])
        self.assertNotIn("neighbor", result["content"])

    def test_private_pseudo_fence_exploit_fails_closed(self):
        # Regression: fences were matched after line.strip(), so a 4-space or
        # tab indented pseudo-fence opened a "fence" that hid the next ATX
        # heading and returned FieldB for approved FieldA.
        for fence_line in ("    ```\n", "\t```\n"):
            text = f"# Private\n## FieldA\nsecret-a\n{fence_line}## FieldB\nsecret-b\n```\n"
            with self.subTest(fence_line=fence_line):
                self.write(ops.PRIVATE, text)
                self.assert_code(
                    "AMBIGUOUS_MARKDOWN",
                    self.memory.reference,
                    "private",
                    field="FieldA",
                    purpose="test",
                    approval_id="yes",
                )

    def test_private_pseudo_fence_is_plain_text_and_bounds_stay_exact(self):
        for fence_line in ("    ```\n", "\t```\n"):
            text = f"# Private\n## FieldA\n{fence_line}fake\n## FieldB\nneighbor\n"
            with self.subTest(fence_line=fence_line):
                self.write(ops.PRIVATE, text)
                result = self.memory.reference(
                    "private", field="FieldA", purpose="test", approval_id="yes"
                )
                self.assertEqual(result["content"], f"## FieldA\n{fence_line}fake\n")
                self.assertNotIn("neighbor", result["content"])

    def test_section_standard_fence_indentation_still_recognized(self):
        text = (
            "# Private\n## FieldA\nvalue\n   ```text\n# literal\n   ```\n"
            "### Child\nneighbor\n"
        )
        self.write(ops.PRIVATE, text)
        result = self.memory.reference(
            "private", field="FieldA", purpose="test", approval_id="yes"
        )
        self.assertIn("# literal", result["content"])
        self.assertNotIn("neighbor", result["content"])
        self.write(
            ops.PRIVATE, "# Private\n## FieldA\n```\ncode\n  ```  \n### Child\nx\n"
        )
        result = self.memory.reference(
            "private", field="FieldA", purpose="test", approval_id="yes"
        )
        self.assertIn("code", result["content"])

    def test_section_fence_info_and_closing_shapes(self):
        # A backtick inside the opening info string is not a fence for CommonMark
        # renderers; the pseudo-closing with a suffix never closes either, so a
        # heading after it stays hidden instead of becoming a field boundary.
        for text in (
            "# Private\n## FieldA\n``` ```\n## FieldB\nb\n```\n",
            "# Private\n## FieldA\n```\ncode\n``` trailing\n## FieldB\nb\n### tail\n",
        ):
            self.assert_code("AMBIGUOUS_MARKDOWN", ops.section_bounds, text, "FieldA")
        # CommonMark semantics that stay unambiguous: a shorter or foreign-marker
        # line does not close a fence, so the inner heading stays literal and
        # FieldB cannot be read as a field either.
        text = "# Private\n## FieldA\n````\ncode\n```\n## FieldB\nb\n````\n"
        start, end = ops.section_bounds(text, "FieldA")
        self.assertIn("## FieldB", text[start:end])
        self.assert_code("UNKNOWN_FIELD", ops.section_bounds, text, "FieldB")

    def test_symlink_topic_rejected_without_reading(self):
        (self.root / ops.PROJECTS / "linked.md").symlink_to(self.root / ops.PRIVATE)
        self.assert_code("UNSAFE_PATH", self.memory.reference, "topics")


class MaintenanceAndSyncTests(Fixture):
    def test_atomic_write_corrects_inherited_mode_before_writing(self):
        original_open = os.open
        original_fdopen = os.fdopen
        corrected = []

        def inherited_mode(path, flags, mode=0o777, **kwargs):
            fd = original_open(path, flags, mode, **kwargs)
            if flags & os.O_CREAT and Path(path).name.startswith(".memory-"):
                os.fchmod(fd, 0o700)  # Simulate a filesystem that ignores the create mode.
            return fd

        class CheckedWriter:
            def __init__(self, fd, mode):
                self.stream = original_fdopen(fd, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def flush(self):
                self.stream.flush()

            def write(self, data):
                actual = stat.S_IMODE(os.fstat(self.fileno()).st_mode)
                corrected.append(actual)
                self_test.assertEqual(actual, 0o600)
                return self.stream.write(data)

        self_test = self
        with (
            mock.patch.object(ops.os, "open", inherited_mode),
            mock.patch.object(ops.os, "fdopen", CheckedWriter),
        ):
            ops.atomic_write(self.root / ops.PUBLIC, b"synthetic replacement")
        self.assertEqual(corrected, [0o600])
        self.assertEqual(stat.S_IMODE((self.root / ops.PUBLIC).stat().st_mode), 0o600)

    def test_atomic_write_failed_chmod_preserves_target_and_removes_temporary(self):
        before = (self.root / ops.PUBLIC).read_bytes()
        with mock.patch.object(ops.os, "fchmod", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                ops.atomic_write(self.root / ops.PUBLIC, b"never publish")
        self.assertEqual((self.root / ops.PUBLIC).read_bytes(), before)
        self.assertEqual(list((self.root / ops.PUBLIC).parent.glob(".memory-*")), [])

    def test_atomic_write_ineffective_chmod_refuses_publication(self):
        before = (self.root / ops.PUBLIC).read_bytes()
        original_open = os.open

        def inherited_mode(path, flags, mode=0o777, **kwargs):
            fd = original_open(path, flags, mode, **kwargs)
            if flags & os.O_CREAT and Path(path).name.startswith(".memory-"):
                os.chmod(path, 0o700)
            return fd

        with (
            mock.patch.object(ops.os, "open", inherited_mode),
            mock.patch.object(ops.os, "fchmod"),
        ):
            self.assert_code(
                "UNSAFE_PERMISSIONS",
                ops.atomic_write,
                self.root / ops.PUBLIC,
                b"never publish",
            )
        self.assertEqual((self.root / ops.PUBLIC).read_bytes(), before)
        self.assertEqual(list((self.root / ops.PUBLIC).parent.glob(".memory-*")), [])

    def test_maintenance_prunes_raw_archive_unmatched_diagnostics(self):
        old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        self.write(
            ops.UNMATCHED,
            ops.rows_bytes([{"id": "a" * 32, "time": old, "text": "unmatched-old"}]),
        )
        self.write(
            ops.ARCHIVE,
            ops.rows_bytes([{"id": "b" * 32, "time": old, "text": "archive-old"}]),
        )
        self.write(
            ops.DIAGNOSTICS,
            ops.rows_bytes(
                [
                    {
                        "time": old,
                        "code": "OLD",
                        "op_id": "a" * 32,
                        "text": "should-disappear",
                    }
                ]
            ),
        )
        self.memory.maintain()
        self.memory.maintain()
        self.assertEqual(self.rows(ops.UNMATCHED), [])
        self.assertEqual(
            self.rows(ops.ARCHIVE), [{"id": "b" * 32, "raw_expired": True}]
        )
        self.assertNotIn("should-disappear", (self.root / ops.DIAGNOSTICS).read_text())

    def test_invalid_timestamp_not_sent_to_model_and_maintenance_reports(self):
        self.write(
            ops.UNMATCHED,
            ops.rows_bytes([{"time": "nonsense", "text": "do-not-output"}]),
        )
        snapshot = self.memory.snapshot(source="unmatched")
        self.assertNotIn("do-not-output", json.dumps(snapshot))
        self.assert_code("INVALID_TIMESTAMP", self.memory.maintain)
        self.assertEqual(self.rows(ops.UNMATCHED), [])

    def test_maintenance_failed_status_persists_until_clean_maintenance(self):
        self.assertIs(self.memory.status()["maintenance_failed"], False)
        self.write(
            ops.UNMATCHED,
            ops.rows_bytes(
                [{"time": "invalid-synthetic-time", "text": "synthetic-body"}]
            ),
        )
        self.assert_code("INVALID_TIMESTAMP", self.memory.maintain)
        reopened = ops.Memory(self.root)
        self.assertEqual(
            reopened.state()["maintenance_failure"], "INVALID_TIMESTAMP"
        )
        status = reopened.status()
        self.assertIs(status["maintenance_failed"], True)
        self.assertNotIn("INVALID_TIMESTAMP", json.dumps(status))
        self.assertNotIn("synthetic", json.dumps(status))
        self.assertEqual(self.rows(ops.UNMATCHED), [])
        self.assertEqual(self.memory.maintain(), {"ok": True, "status": "maintained"})
        self.assertIsNone(reopened.state()["maintenance_failure"])
        self.assertIs(reopened.status()["maintenance_failed"], False)

    def test_maintenance_status_exposes_boolean_not_stored_error_text(self):
        self.write(ops.STATE, {"maintenance_failure": "synthetic-private-error-body"})
        status = self.memory.status()
        self.assertIs(status["maintenance_failed"], True)
        self.assertNotIn("synthetic-private-error-body", json.dumps(status))
        self.assertNotIn("maintenance_failure", status)

    def test_diagnostics_cap_and_exception_output_safe(self):
        with self.memory.lock(), mock.patch.object(ops, "DIAGNOSTIC_BYTES", 200):
            for _ in range(10):
                self.memory.diagnostic("SYNTHETIC")
        self.assertLessEqual((self.root / ops.DIAGNOSTICS).stat().st_size, 200)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = ops.run_safe(
                lambda: (_ for _ in ()).throw(OSError("secret-payload"))
            )
        self.assertEqual(result, 1)
        self.assertNotIn("secret-payload", output.getvalue())


class CaptureLockRecheckTests(Fixture):
    def test_marker_after_second_check_does_not_cancel_inflight_capture(self):
        original = ops.json_read

        def pause_when_reading_rules(path):
            if Path(path) == self.root / ops.RULES:
                (self.root / ops.PAUSED).touch(mode=0o600)
            return original(path)

        with mock.patch.object(ops, "json_read", pause_when_reading_rules):
            self.assertEqual(self.event()["status"], "queued")
        self.assertEqual(len(self.rows(ops.QUEUE)), 1)

    def test_paused_before_lock_reads_no_rules_or_queues(self):
        (self.root / ops.PAUSED).touch(mode=0o600)
        reads = []
        records = []
        real_read = ops.read_bytes
        real_records = ops.records

        def read(path, absent=None):
            reads.append(Path(path))
            return real_read(path, absent)

        def record(path):
            records.append(Path(path))
            return real_records(path)

        with (
            mock.patch.object(ops, "read_bytes", read),
            mock.patch.object(ops, "records", record),
        ):
            self.assertEqual(
                self.event("remember: synthetic paused"),
                {"ok": True, "status": "paused"},
            )
        self.assertEqual(reads, [])
        self.assertEqual(records, [])
        self.assertFalse((self.root / "learning/.memory.lock").exists())
        self.assertEqual(self.rows(ops.QUEUE), [])
        self.assertEqual(self.rows(ops.UNMATCHED), [])

    def test_marker_appearing_while_waiting_for_lock_is_rechecked(self):
        calls = []
        reads = []
        real_lock = ops.lock_file
        real_read = ops.read_bytes

        def waiting(fd):
            calls.append(fd)
            if len(calls) == 1:
                (self.root / ops.PAUSED).touch(mode=0o600)
                raise BlockingIOError("synthetic contention")
            return real_lock(fd)

        def read(path, absent=None):
            reads.append(Path(path))
            return real_read(path, absent)

        with (
            mock.patch.object(ops, "lock_file", waiting),
            mock.patch.object(ops, "read_bytes", read),
        ):
            self.assertEqual(
                self.event("remember: synthetic race"),
                {"ok": True, "status": "paused"},
            )
        self.assertGreaterEqual(len(calls), 2)
        # The marker appeared between the pre-check and the acquired lock; the
        # in-lock recheck returns before any rules or queue read.
        self.assertEqual(reads, [])
        self.assertEqual(self.rows(ops.QUEUE), [])
        self.assertEqual(self.rows(ops.UNMATCHED), [])

    def test_match_rules_keeps_literal_regex_and_rule_order_semantics(self):
        compiled = ops.validate_rules(
            {
                "Later": ["beta"],
                "First": ["alpha"],
                "Regex": [r"^starts", r"\bword\b"],
                "Literal": ["MiXeD"],
            }
        )
        self.assertEqual(
            ops.match_rules("alpha starts word MIXED beta", compiled),
            ["Later", "First", "Regex", "Literal"],
        )
        # Category order follows rule order, not the order text mentions them.
        self.assertEqual(
            ops.match_rules("beta alpha", compiled), ["Later", "First"]
        )
        # Plain patterns stay case-insensitive substring matches.
        self.assertEqual(ops.match_rules("BETA", compiled), ["Later"])
        self.assertEqual(ops.match_rules("xxmixedxx", compiled), ["Literal"])
        # Patterns with metacharacters remain case-insensitive regex searches.
        self.assertEqual(ops.match_rules("STARTS here", compiled), ["Regex"])
        self.assertEqual(ops.match_rules("nothing at all", compiled), [])
        self.assertEqual(
            ops.match_rules("anything", ops.validate_rules({"Empty": []})), []
        )

    def test_match_rules_dot_pattern_keeps_legacy_regex_behavior(self):
        # validate_rules still classifies any pattern containing "." as regex,
        # exactly as capture did before match_rules was extracted.
        compiled = ops.validate_rules({"Version": ["v1.0"]})
        self.assertEqual(ops.match_rules("V1X0", compiled), ["Version"])
        self.assertEqual(ops.match_rules("v1 0", compiled), ["Version"])
        self.assertEqual(ops.match_rules("v10", compiled), [])


if __name__ == "__main__":
    unittest.main()
