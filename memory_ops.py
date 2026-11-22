#!/usr/bin/env python3
"""Personal Memory: local JSONL candidates, confirmed writes, scoped reads.

Cross-platform (Linux, macOS, Windows). All state lives under the skill's own
directory; there is no network copy. File locking uses fcntl on POSIX and
msvcrt on Windows. POSIX-only hardening (0600/0700 modes, O_NOFOLLOW) is applied
where the platform supports it and skipped cleanly elsewhere.
"""

import argparse
import contextlib
import hashlib
import json
import os
import random
from pathlib import Path
import re
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
import uuid

POSIX = os.name == "posix"
if POSIX:
    import fcntl
else:  # pragma: no cover - exercised only on Windows
    import msvcrt

# Open-flag hardening bits that only some platforms expose. getattr keeps the
# module importable on Windows, where these constants are absent; value 0 is a
# no-op inside the bitmask. O_BINARY avoids CRLF translation on Windows.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_BINARY = getattr(os, "O_BINARY", 0)

DEFAULT_ROOT = Path(__file__).absolute().parent


def lock_file(fd):
    """Take an exclusive, non-blocking lock; raise if another holder has it."""
    if POSIX:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:  # pragma: no cover - Windows only
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def unlock_file(fd):
    """Release the lock. POSIX drops it on close; Windows must unlock the byte."""
    if not POSIX:  # pragma: no cover - Windows only
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
QUEUE = "learning/learning_queue.jsonl"
UNMATCHED = "learning/unmatched.jsonl"
ARCHIVE = "learning/archive.jsonl"
RULES = "learning/rules.json"
PUBLIC = "user-profile/public.md"
PRIVATE = "user-profile/private.md"
PROJECTS = "user-profile/projects"
PENDING = "learning/pending-review/transaction.json"
STATE = "learning/maintenance.json"
CONFIRMATION = "learning/last-confirmation.json"
DIAGNOSTICS = "learning/diagnostics.jsonl"
# Presence of this marker pauses capture. A marker file is the only switch:
# it behaves identically on every platform, needs no restart, and is one
# `stat` inside a process that already starts an interpreter and touches
# files. An environment variable would need a restart and is awkward to set
# outside a container, so it is deliberately not offered.
PAUSED = "learning/.paused"
QUEUE_COUNT = 10_000
QUEUE_BYTES = 20 * 1024 * 1024
MATCHED_BYTES = 64 * 1024
UNMATCHED_COUNT = 10_000
UNMATCHED_BYTES = 10 * 1024 * 1024
UNMATCHED_TEXT_BYTES = 16 * 1024
DIAGNOSTIC_BYTES = 1024 * 1024
LOCK_RETRIES = 5
LOCK_RETRY_DELAY = 0.02
LOCK_RETRY_CAP = 0.2
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"(?:[0-9a-f]{64}|[0-9a-f]{32})\Z")
LEGACY_ID_RE = re.compile(r"[0-9a-f]{16}\Z")
GEN_RE = re.compile(r"[0-9a-f]{32}\Z")
CREDENTIAL_RE = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|(?:access|refresh)[_ -]?token|secret|password|passwd|"
    r"client[_ -]?secret|aws_secret_access_key|aws_session_token|"
    r"authorization|bearer|token)\b[\"']?\s*[:=： ]\s*\S+|"
    r"(?:密码|密钥|口令|令牌)\s*[:=：]\s*\S+|"
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
    r"AKIA[A-Z0-9]{16})\b|-----BEGIN [^-]*PRIVATE KEY-----|"
    r"https?://[^\s/:]+:[^\s/@]+@)"
)


class OpError(Exception):
    """Only fixed, non-content-bearing codes cross the CLI boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise OpError("INVALID_ARGUMENT")


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def digest(data):
    return hashlib.sha256(data).hexdigest()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def run_safe(action):
    try:
        os.umask(0o077)
        result = action()
        return result if isinstance(result, int) else 0
    except OpError as exc:
        emit({"ok": False, "error": exc.code})
        return 2 if exc.code == "BUSY" else 1
    except (Exception, KeyboardInterrupt):
        emit({"ok": False, "error": "IO_OR_FORMAT_ERROR"})
        return 1


def load_input():
    try:
        value = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        raise OpError("INVALID_JSON") from None
    if not isinstance(value, dict):
        raise OpError("INVALID_JSON")
    return value


def checked(path, directory=False, missing=False):
    """Reject symlinks/special files in every component, including an input root."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise OpError("UNSAFE_PATH")
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if missing:
                return path
            raise OpError("MISSING_PATH") from None
        last = index == len(path.parts) - 2
        if stat.S_ISLNK(mode) or not (
            stat.S_ISDIR(mode) if not last or directory else stat.S_ISREG(mode)
        ):
            raise OpError("UNSAFE_PATH")
    return path


def secure_dir(path):
    path = checked(path, directory=True, missing=True)
    if not path.exists():
        if not path.parent.exists():
            secure_dir(path.parent)
        else:
            checked(path.parent, directory=True)
        path.mkdir(mode=0o700)
        fsync_dir(path.parent)
    if POSIX:
        # 0700 keeps other local users out of the profile/queue. Windows uses
        # ACL inheritance instead; chmod there only toggles the read-only bit.
        os.chmod(path, 0o700)
    return path


def fsync_dir(path):
    # Directory fsync makes a rename durable, but Windows cannot open a
    # directory as a file descriptor. The rename itself is still ordered there.
    if not POSIX:
        return
    fd = os.open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_bytes(path, absent=None):
    checked(path, missing=True)
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY)
    except FileNotFoundError:
        return absent
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OpError("UNSAFE_PATH")
        return stream.read()


def atomic_write(path, data):
    checked(path, missing=True)
    secure_dir(path.parent)
    temporary = path.parent / (".memory-" + uuid.uuid4().hex)
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_BINARY, 0o600
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            if POSIX:
                # Force the inode mode before any bytes land, in case a
                # restrictive umask was not in effect for this descriptor.
                os.fchmod(stream.fileno(), 0o600)
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
                    raise OpError("UNSAFE_PERMISSIONS")
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checked(path, missing=True)
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def file_hash(path):
    data = read_bytes(path)
    return None if data is None else digest(data)


def json_read(path, default=None):
    data = read_bytes(path)
    if data is None:
        return default
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise OpError("INVALID_STORED_JSON") from None


def records(path):
    data = read_bytes(path, b"")
    result = []
    try:
        for line in data.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError
            if "text" in row and not isinstance(row["text"], str):
                raise ValueError
            result.append(row)
    except (ValueError, UnicodeError):
        raise OpError("INVALID_JSONL") from None
    return result


def rows_bytes(rows):
    return b"".join(encode(row) for row in rows)


def parsed_time(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError
        return date
    except (AttributeError, TypeError, ValueError):
        return None


def valid_ids(values):
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not ID_RE.fullmatch(value) for value in values
    ):
        raise OpError("INVALID_IDS")
    if len(set(values)) != len(values):
        raise OpError("DUPLICATE_IDS")
    return values


def legacy_id(value):
    return isinstance(value, str) and LEGACY_ID_RE.fullmatch(value) is not None


def archive_tombstone(row):
    return row.get("raw_expired") is True and set(row) in (
        {"id", "raw_expired"},
        {"id", "raw_expired", "legacy_id"},
    )


def stored_ids(rows, archive=False):
    """Validate without migrating; short tombstones are archive-only metadata."""
    seen = set()
    for row in rows:
        value = row.get("id")
        if "id" not in row or (
            legacy_id(value) and not (archive and archive_tombstone(row))
        ):
            raise OpError("LEGACY_REQUIRES_SNAPSHOT")
        if not (archive and archive_tombstone(row) and legacy_id(value)):
            valid_ids([value])
        if "legacy_id" in row and not legacy_id(row["legacy_id"]):
            raise OpError("INVALID_IDS")
        if value in seen:
            raise OpError("DUPLICATE_STORED_ID")
        seen.add(value)


def approval(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise OpError("APPROVAL_REQUIRED")
    return value


def topic_path(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or name.startswith(".")
        or ".." in name
        or any(not (c.isalnum() or c in " _-") for c in name)
        or name != name.strip()
    ):
        raise OpError("INVALID_TOPIC")
    return f"{PROJECTS}/{name}.md"


def formal_path(name, private=False):
    if name in (PUBLIC, RULES) or (private and name == PRIVATE):
        return name
    if (
        isinstance(name, str)
        and name.startswith(PROJECTS + "/")
        and name.endswith(".md")
    ):
        if topic_path(name[len(PROJECTS) + 1 : -3]) == name:
            return name
    raise OpError("PATH_NOT_ALLOWED")


def validate_rules(value):
    if not isinstance(value, dict) or not value:
        raise OpError("INVALID_RULES")
    compiled = {}
    for category, patterns in value.items():
        if not isinstance(category, str) or not category.strip():
            raise OpError("INVALID_RULES")
        if not isinstance(patterns, list):
            raise OpError("INVALID_RULES")
        compiled[category] = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern:
                raise OpError("INVALID_RULES")
            if re.search(r"[.*^$|()\[\]\\]", pattern):
                try:
                    compiled[category].append(re.compile(pattern, re.IGNORECASE))
                except re.error:
                    raise OpError("INVALID_RULE_REGEX") from None
            else:
                compiled[category].append(pattern.lower())
    return compiled


def match_rules(text, compiled):
    """Return matching categories in rule order, using capture's original semantics."""
    lowered = text.lower()
    return [
        category
        for category, patterns in compiled.items()
        if any(
            pattern in lowered if isinstance(pattern, str) else pattern.search(text)
            for pattern in patterns
        )
    ]


def section_bounds(text, title):
    """Strict ATX blocks end at the next heading of ANY level, never a neighbor.

    Fences are recognized only on raw lines with 0-3 leading spaces, 3+ identical
    marker characters, no fence character in the opening info string, and
    closings with no suffix. Four-space or tab indented pseudo-fences are plain
    text and can never hide a following ATX heading; unsupported shapes fail
    closed.
    """
    if not isinstance(title, str) or not title.strip() or "\n" in title:
        raise OpError("INVALID_FIELD")
    headings = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        if body.endswith("\r"):
            body = body[:-1]
        stripped = line.strip()
        indent = len(body) - len(body.lstrip(" "))
        marker = re.match(r"(`{3,}|~{3,})(.*)", body[indent:]) if indent <= 3 else None
        if marker:
            token, rest = marker.group(1), marker.group(2)
            if fence is None:
                if token[0] == "`" and "`" in rest:
                    raise OpError("AMBIGUOUS_MARKDOWN")
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not rest.strip():
                fence = None
        elif fence is None:
            heading = re.fullmatch(r"(#{1,6})[ \t]+([^\r\n]+?)[ \t]*", body)
            if heading:
                name = heading.group(2)
                if name.endswith("#") or name != name.strip():
                    raise OpError("AMBIGUOUS_MARKDOWN")
                headings.append((name, offset))
            elif (
                stripped.startswith("#")
                or re.fullmatch(r"(?:=+|-+)", stripped)
                or re.match(r"(?i)</?h[1-6]\b", stripped)
            ):
                raise OpError("AMBIGUOUS_MARKDOWN")
        offset += len(line)
    if fence is not None or not headings:
        raise OpError("AMBIGUOUS_MARKDOWN")
    names = [name for name, _ in headings]
    if len(set(names)) != len(names):
        raise OpError("AMBIGUOUS_MARKDOWN")
    if title not in names:
        raise OpError("UNKNOWN_FIELD")
    index = names.index(title)
    start = headings[index][1]
    end = headings[index + 1][1] if index + 1 < len(headings) else len(text)
    return start, end


class Memory:
    def __init__(self, root=None):
        self.root = checked(
            Path(root) if root is not None else DEFAULT_ROOT, directory=True
        )

    def path(self, name):
        if (
            not isinstance(name, str)
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise OpError("UNSAFE_PATH")
        return checked(self.root / name, missing=True)

    @contextlib.contextmanager
    def lock(self):
        if POSIX:
            os.umask(0o077)
        secure_dir(self.root / "learning")
        path = self.path("learning/.memory.lock")
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_BINARY, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OpError("UNSAFE_PATH")
            if POSIX:
                os.fchmod(fd, 0o600)
            # Non-blocking lock with bounded exponential backoff. A single
            # session almost never contends, but a plugin that bursts events
            # could momentarily collide; a few short retries avoid dropping a
            # candidate without introducing an unbounded wait or a dependency.
            delay = LOCK_RETRY_DELAY
            for attempt in range(LOCK_RETRIES + 1):
                try:
                    lock_file(fd)
                    break
                except (BlockingIOError, OSError):
                    if attempt == LOCK_RETRIES:
                        raise OpError("BUSY") from None
                    time.sleep(delay + random.uniform(0, delay))
                    delay = min(delay * 2, LOCK_RETRY_CAP)
            try:
                yield
            finally:
                unlock_file(fd)
        finally:
            os.close(fd)

    def state(self):
        value = json_read(self.path(STATE), {})
        if not isinstance(value, dict):
            raise OpError("INVALID_STATE")
        return value

    def update_state(self, **changes):
        value = self.state()
        value.update(changes)
        atomic_write(self.path(STATE), encode(value))

    def diagnostic(self, code):
        path = self.path(DIAGNOSTICS)
        rows = records(path)
        rows.append({"time": now(), "code": code, "op_id": uuid.uuid4().hex})
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        # Reconstruct only known metadata; never perpetuate arbitrary fields.
        rows = [
            {"time": row["time"], "code": row["code"], "op_id": row["op_id"]}
            for row in rows
            if (
                parsed_time(row.get("time"))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            >= cutoff
            and re.fullmatch(r"[A-Z_]{1,64}", str(row.get("code", "")))
            and GEN_RE.fullmatch(str(row.get("op_id", "")))
        ]
        while rows and len(rows_bytes(rows)) > DIAGNOSTIC_BYTES:
            rows.pop(0)
        atomic_write(path, rows_bytes(rows))

    def pending(self):
        value = json_read(self.path(PENDING))
        if value is not None and not isinstance(value, dict):
            raise OpError("INVALID_PENDING")
        return value

    def migrate(self, *names):
        """Caller holds the fixed lock. Validate all sources before replacing any.

        Cross-file replacement is restartable, not globally atomic: known short
        IDs map deterministically, independent of archive-only metadata.
        """
        if self.pending() is not None:
            raise OpError("PENDING_REVIEW")
        sources = {}
        changed = []
        for name in names:
            rows = records(self.path(name))
            before = rows_bytes(rows)
            for row in rows:
                if "id" not in row:
                    row["id"] = uuid.uuid4().hex
                elif legacy_id(row["id"]):
                    if name == ARCHIVE and archive_tombstone(row):
                        continue  # Lost time/text cannot establish a modern event ID.
                    if (
                        not isinstance(row.get("time"), str)
                        or not row["time"]
                        or "text" not in row
                    ):
                        raise OpError("LEGACY_IDENTITY_REQUIRED")
                    original = row["id"]
                    if row.get("legacy_id", original) != original:
                        raise OpError("INVALID_IDS")
                    row["legacy_id"] = original
                    row["id"] = digest(
                        encode(
                            [
                                "personal-memory:legacy16:v1",
                                original,
                                row["time"],
                                digest(row["text"].encode("utf-8")),
                            ]
                        )
                    )
            stored_ids(rows, archive=name == ARCHIVE)
            sources[name] = rows
            if rows_bytes(rows) != before:
                changed.append(name)
        for name in changed:
            atomic_write(self.path(name), rows_bytes(sources[name]))
        return sources

    def snapshot(self, source="queue", public=False, topics=(), metadata_only=False):
        """Legacy, unbounded snapshot; not the fixed-batch workflow page outlet.

        This explicitly migrates the selected source AND archive. Non-metadata
        output withholds invalid timestamps and CREDENTIAL_RE matches only; it
        does not screen truncation, empty text, log quotations or duplicates.
        Optional public/topic reads and the existing output shape are unchanged.
        Neither outlet is a complete sensitive-data detector.
        """
        with self.lock():
            if self.pending() is not None:
                raise OpError("PENDING_REVIEW")
            name = QUEUE if source == "queue" else UNMATCHED
            if source not in ("queue", "unmatched"):
                raise OpError("INVALID_SOURCE")
            if metadata_only and (public or topics):
                raise OpError("INVALID_SCOPE_ARGUMENT")
            sources = self.migrate(name, ARCHIVE)
            rows = sources[name]
            if metadata_only:
                return {
                    "ok": True,
                    "scope": source,
                    "record_count": len(rows),
                    "archive_count": len(sources[ARCHIVE]),
                }
            result = []
            redacted = 0
            for row in rows:
                item = {"id": row["id"]}
                text = row.get("text", "")
                if parsed_time(row.get("time")) is None:
                    item["withheld"] = "INVALID_TIMESTAMP"
                elif CREDENTIAL_RE.search(text):
                    item["withheld"] = "POSSIBLE_CREDENTIAL"
                    redacted += 1
                else:
                    item.update(time=row["time"], text=text)
                    item["text_sha256"] = digest(text.encode("utf-8"))
                result.append(item)
            files = {RULES: {"sha256": file_hash(self.path(RULES))}}
            names = ([PUBLIC] if public else []) + [
                topic_path(topic) for topic in topics
            ]
            for name in names:
                data = read_bytes(self.path(name))
                files[name] = {
                    "sha256": None if data is None else digest(data),
                    "content": None if data is None else data.decode("utf-8"),
                }
            return {
                "ok": True,
                "scope": source,
                "records": result,
                "redacted_count": redacted,
                "source_sha256": file_hash(
                    self.path(QUEUE if source == "queue" else UNMATCHED)
                ),
                "files": files,
            }

    def reference(
        self,
        scope,
        section=None,
        topic=None,
        field=None,
        purpose=None,
        approval_id=None,
    ):
        options = {
            "section": section,
            "topic": topic,
            "field": field,
            "purpose": purpose,
            "approval_id": approval_id,
        }
        allowed = {
            "public": {"section"},
            "topics": set(),
            "topic": {"topic", "approval_id"},
            "private": {"field", "purpose", "approval_id"},
        }
        if scope not in allowed or any(
            value is not None and key not in allowed[scope]
            for key, value in options.items()
        ):
            raise OpError("INVALID_SCOPE_ARGUMENT")
        with self.lock():
            if scope == "topics":
                directory = checked(self.root / PROJECTS, directory=True, missing=True)
                names = []
                if directory.exists():
                    for path in sorted(directory.iterdir()):
                        if path.suffix == ".md":
                            topic_path(path.stem)
                            checked(path)
                            names.append(path.stem)
                return {"ok": True, "scope": "topic_names", "topics": names}
            if scope == "public":
                name = PUBLIC
                title = section
            elif scope == "topic":
                approval(approval_id)
                name = topic_path(topic)
                title = None
            elif scope == "private":
                approval(approval_id)
                if not isinstance(purpose, str) or not purpose.strip():
                    raise OpError("PURPOSE_REQUIRED")
                if not field:
                    raise OpError("FIELD_REQUIRED")
                name = PRIVATE
                title = field
            else:
                raise OpError("INVALID_SCOPE")
            data = read_bytes(self.path(name))
            if data is None:
                raise OpError("MISSING_PATH")
            content = data.decode("utf-8")
            if title is not None:
                start, end = section_bounds(content, title)
                content = content[start:end]
            return {
                "ok": True,
                "scope": scope,
                "path": name,
                "section": title,
                "file_sha256": digest(data),
                "content": content,
            }

    def prepare(self, request, allow_private=False, approved_scope=None):
        with self.lock():
            if self.pending() is not None:
                raise OpError("PENDING_REVIEW")
            ids = valid_ids(request.get("processed_ids", []))
            unmatched_ids = valid_ids(request.get("reviewed_unmatched_ids", []))
            sources = self.migrate(
                QUEUE, ARCHIVE, *([UNMATCHED] if unmatched_ids else [])
            )
            queue = sources[QUEUE]
            unmatched = sources[UNMATCHED] if unmatched_ids else []
            selected = [row for row in queue if row["id"] in ids]
            reviewed = [row for row in unmatched if row["id"] in unmatched_ids]
            if len(selected) != len(ids) or len(reviewed) != len(unmatched_ids):
                raise OpError("STALE_IDS")
            writes = request.get("writes", [])
            if not isinstance(writes, list):
                raise OpError("INVALID_WRITES")
            staged = []
            seen = set()
            for write in writes:
                if not isinstance(write, dict):
                    raise OpError("INVALID_WRITES")
                name = formal_path(write.get("path"), private=allow_private)
                if name in seen:
                    raise OpError("DUPLICATE_TARGET")
                seen.add(name)
                before = write.get("before_sha256")
                if before is not None and (
                    not isinstance(before, str) or not HASH_RE.fullmatch(before)
                ):
                    raise OpError("INVALID_HASH")
                if "before_sha256" not in write or file_hash(self.path(name)) != before:
                    raise OpError("VERSION_CONFLICT")
                content = write.get("content")
                if not isinstance(content, str):
                    raise OpError("INVALID_CONTENT")
                if name == PRIVATE:
                    if not allow_private or not approved_scope or before is None:
                        raise OpError("PRIVATE_SCOPE_REQUIRED")
                    current = read_bytes(self.path(name)).decode("utf-8")
                    start, end = section_bounds(current, approved_scope)
                    a, b = section_bounds(content, approved_scope)
                    if a != 0 or b != len(content):
                        raise OpError("PRIVATE_SCOPE_MISMATCH")
                    if end < len(current) and not content.endswith("\n"):
                        raise OpError("PRIVATE_SCOPE_MISMATCH")
                    content = current[:start] + content + current[end:]
                    section_bounds(content, approved_scope)
                if name == RULES:
                    try:
                        validate_rules(json.loads(content))
                    except (ValueError, UnicodeError):
                        raise OpError("INVALID_RULES") from None
                staged.append(
                    {
                        "path": name,
                        "before_sha256": before,
                        "after_sha256": digest(content.encode()),
                        "content": content,
                    }
                )
            if not staged and not ids and not unmatched_ids:
                raise OpError("EMPTY_BATCH")
            tx = {
                "txid": uuid.uuid4().hex,
                "prepared_at": now(),
                "processed_ids": ids,
                "reviewed_unmatched_ids": unmatched_ids,
                "records": selected,
                "reviewed_records": reviewed,
                "writes": staged,
                "private_scope": approved_scope if allow_private else None,
                "confirmation": None,
            }
            atomic_write(self.path(PENDING), encode(tx))
            self.update_state(pending_status="prepared", pending_failure=None)
            return {
                "ok": True,
                "txid": tx["txid"],
                "status": "prepared",
                "processed_count": len(ids),
                "write_count": len(staged),
            }

    @staticmethod
    def confirmation_manifest(tx):
        return {
            "txid": tx["txid"],
            "processed_ids": tx["processed_ids"],
            "reviewed_unmatched_ids": tx["reviewed_unmatched_ids"],
            "records_sha256": digest(encode(tx["records"])),
            "reviewed_sha256": digest(encode(tx["reviewed_records"])),
            "private_scope": tx["private_scope"],
            "targets": [
                {k: row[k] for k in ("path", "before_sha256", "after_sha256")}
                for row in tx["writes"]
            ],
        }

    def check_transaction(self, tx):
        valid_ids(tx["processed_ids"])
        valid_ids(tx["reviewed_unmatched_ids"])
        if not GEN_RE.fullmatch(tx["txid"]):
            raise OpError("INVALID_PENDING")
        seen = set()
        for write in tx["writes"]:
            name = formal_path(write["path"], private=bool(tx["private_scope"]))
            if (
                name in seen
                or digest(write["content"].encode()) != write["after_sha256"]
            ):
                raise OpError("INVALID_PENDING")
            seen.add(name)
            if name == RULES:
                validate_rules(json.loads(write["content"]))
        for key, ids_key in (
            ("records", "processed_ids"),
            ("reviewed_records", "reviewed_unmatched_ids"),
        ):
            row_ids = valid_ids([row["id"] for row in tx[key]])
            if set(row_ids) != set(tx[ids_key]):
                raise OpError("INVALID_PENDING")

    def verify_versions(self, tx, recovering):
        for write in tx["writes"]:
            allowed = (
                (write["before_sha256"], write["after_sha256"])
                if recovering
                else (write["before_sha256"],)
            )
            if file_hash(self.path(write["path"])) not in allowed:
                raise OpError("VERSION_CONFLICT")
        queue = records(self.path(QUEUE))
        archive = records(self.path(ARCHIVE))
        unmatched = (
            records(self.path(UNMATCHED)) if tx["reviewed_unmatched_ids"] else []
        )
        stored_ids(queue)
        stored_ids(archive, archive=True)
        stored_ids(unmatched)
        q = {row["id"]: row for row in queue}
        a = {row["id"]: row for row in archive}
        u = {row["id"]: row for row in unmatched}
        for row in tx["records"]:
            if row["id"] in q:
                if q[row["id"]] != row:
                    raise OpError("RECORD_CONFLICT")
            elif not recovering or row["id"] not in a:
                raise OpError("STALE_IDS")
            if (
                row["id"] in a
                and {k: v for k, v in a[row["id"]].items() if k != "archived_at"} != row
            ):
                raise OpError("RECORD_CONFLICT")
        for row in tx["reviewed_records"]:
            if row["id"] in u and u[row["id"]] != row:
                raise OpError("RECORD_CONFLICT")
            if not recovering and row["id"] not in u:
                raise OpError("STALE_IDS")

    def apply_transaction(self, tx):
        self.verify_versions(tx, recovering=True)
        for write in tx["writes"]:
            path = self.path(write["path"])
            current = file_hash(path)
            if current == write["after_sha256"]:
                continue
            if current != write["before_sha256"]:
                raise OpError("VERSION_CONFLICT")
            atomic_write(path, write["content"].encode())
        self.verify_versions(tx, recovering=True)
        archive = records(self.path(ARCHIVE))
        archived = {row["id"] for row in archive}
        for row in tx["records"]:
            if row["id"] not in archived:
                archive.append(
                    {**row, "archived_at": tx["confirmation"]["confirmed_at"]}
                )
        if tx["records"]:
            atomic_write(self.path(ARCHIVE), rows_bytes(archive))
            queue = records(self.path(QUEUE))
            atomic_write(
                self.path(QUEUE),
                rows_bytes(
                    [row for row in queue if row.get("id") not in tx["processed_ids"]]
                ),
            )
        if tx["reviewed_unmatched_ids"]:
            unmatched = records(self.path(UNMATCHED))
            atomic_write(
                self.path(UNMATCHED),
                rows_bytes(
                    [
                        row
                        for row in unmatched
                        if row.get("id") not in tx["reviewed_unmatched_ids"]
                    ]
                ),
            )
        result = {**tx["confirmation"], "result": "committed", "completed_at": now()}
        atomic_write(self.path(CONFIRMATION), encode(result))
        self.update_state(
            pending_status=None,
            pending_failure=None,
            last_txid=tx["txid"],
        )
        self.path(PENDING).unlink()
        fsync_dir(self.path(PENDING).parent)
        return {
            "ok": True,
            "txid": tx["txid"],
            "status": "committed",
        }

    def commit(self, txid, approval_id=None, recover=False):
        if not isinstance(txid, str) or not GEN_RE.fullmatch(txid):
            raise OpError("INVALID_TXID")
        if not recover:
            approval(approval_id)
        with self.lock():
            tx = self.pending()
            if tx is None:
                previous = json_read(self.path(CONFIRMATION), {})
                if previous.get("manifest", {}).get("txid") == txid:
                    self.update_state(pending_status=None, pending_failure=None)
                    return {
                        "ok": True,
                        "txid": txid,
                        "status": "already_committed",
                    }
                raise OpError("NO_PENDING_REVIEW")
            if tx.get("txid") != txid:
                raise OpError("TXID_MISMATCH")
            try:
                self.check_transaction(tx)
                confirmation = tx.get("confirmation")
                if confirmation is None:
                    if recover:
                        raise OpError("UNCONFIRMED_REVIEW")
                    # Durably record the exact approved batch BEFORE checking/applying versions.
                    tx["confirmation"] = {
                        "approval_id": approval_id,
                        "confirmed_at": now(),
                        "manifest": self.confirmation_manifest(tx),
                        "result": "confirmed",
                    }
                    atomic_write(self.path(PENDING), encode(tx))
                    self.update_state(pending_status="confirmed", pending_failure=None)
                    self.verify_versions(tx, recovering=False)
                else:
                    approval(confirmation.get("approval_id"))
                    if parsed_time(
                        confirmation.get("confirmed_at")
                    ) is None or confirmation.get(
                        "manifest"
                    ) != self.confirmation_manifest(tx):
                        raise OpError("CONFIRMATION_MISMATCH")
                return self.apply_transaction(tx)
            except Exception as exc:
                code = exc.code if isinstance(exc, OpError) else "COMMIT_IO_ERROR"
                self.update_state(pending_failure=code)
                raise OpError(code) from None

    def clear_unmatched(self, ids, approval_id):
        approval(approval_id)
        valid_ids(ids)
        with self.lock():
            if self.pending() is not None:
                raise OpError("PENDING_REVIEW")
            rows = records(self.path(UNMATCHED))
            stored_ids(rows)
            if not set(ids).issubset({row["id"] for row in rows}):
                raise OpError("STALE_IDS")
            atomic_write(
                self.path(UNMATCHED),
                rows_bytes([row for row in rows if row["id"] not in ids]),
            )
            return {"ok": True, "cleared_count": len(ids)}

    def status(self):
        with self.lock():
            state = self.state()
            pending = self.pending()
            # Never forward arbitrary state strings or records to the model.
            return {
                "ok": True,
                "queue_count": len(records(self.path(QUEUE))),
                "unmatched_count": len(records(self.path(UNMATCHED))),
                "pending": None
                if pending is None
                else {
                    "txid": pending["txid"]
                    if GEN_RE.fullmatch(str(pending.get("txid", "")))
                    else None,
                    "confirmed": bool(pending.get("confirmation")),
                },
                "pending_failed": bool(state.get("pending_failure")),
                "maintenance_failed": bool(state.get("maintenance_failure")),
                "capture_paused": self.path(PAUSED).exists(),
                "rejected_candidates": state.get("rejected_candidates", 0)
                if isinstance(state.get("rejected_candidates", 0), int)
                else 0,
            }

    def prune_unmatched(self, rows):
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        valid = []
        invalid = 0
        for row in rows:
            date = parsed_time(row.get("time"))
            if date is None:
                invalid += 1
            elif date >= cutoff:
                valid.append((date, row))
        valid.sort(key=lambda pair: pair[0])
        result = [row for _, row in valid]
        sizes = [len(encode(row)) for row in result]
        total = sum(sizes)
        start = 0
        while len(result) - start > UNMATCHED_COUNT or total > UNMATCHED_BYTES:
            total -= sizes[start]
            start += 1
        return result[start:], invalid

    def maintain(self):
        """Local-only upkeep: expire the unmatched log and compact the archive.

        There is no network copy. Scheduling is optional and left to the host
        (cron/launchd/Task Scheduler); nothing here reaches outside the skill
        directory. Keeping the unmatched log bounded is a privacy measure, not
        just disk hygiene: it is an extra transcript that may contain secrets.
        """
        with self.lock():
            if self.pending() is not None:
                self.update_state(pending_failure="PENDING_REVIEW")
                raise OpError("PENDING_REVIEW")
            archive = records(self.path(ARCHIVE))
            # Do not expire the time/text needed for an explicit legacy migration.
            stored_ids(archive, archive=True)
            rows = records(self.path(UNMATCHED))
            kept, invalid = self.prune_unmatched(rows)
            if rows != kept:
                atomic_write(self.path(UNMATCHED), rows_bytes(kept))
            cutoff = datetime.now(timezone.utc) - timedelta(days=90)
            compacted = []
            for row in archive:
                if archive_tombstone(row):
                    compacted.append(row)
                    continue
                date = parsed_time(row.get("archived_at", row.get("time")))
                if date is None:
                    invalid += 1
                if date is None or date < cutoff:
                    tombstone = {"id": row["id"], "raw_expired": True}
                    if "legacy_id" in row:
                        tombstone["legacy_id"] = row["legacy_id"]
                    compacted.append(tombstone)
                else:
                    compacted.append(row)
            if archive != compacted:
                atomic_write(self.path(ARCHIVE), rows_bytes(compacted))
            self.diagnostic("INVALID_TIMESTAMP" if invalid else "MAINTENANCE")
            if invalid:
                self.update_state(maintenance_failure="INVALID_TIMESTAMP")
            else:
                self.update_state(maintenance_failure=None)
        if invalid:
            raise OpError("INVALID_TIMESTAMP")
        return {"ok": True, "status": "maintained"}


def capture(root, event):
    if not isinstance(event, dict) or not isinstance(event.get("text"), str):
        raise OpError("INVALID_EVENT")
    text = event["text"]
    session = event.get("session_id")
    message = event.get("message_id")
    if message is not None and (
        not isinstance(message, str)
        or not message
        or not isinstance(session, str)
        or not session
    ):
        raise OpError("INVALID_EVENT")
    if session is not None and not isinstance(session, str):
        raise OpError("INVALID_EVENT")
    if not text.strip():
        return {"ok": True, "status": "empty"}
    event_id = (
        digest(encode([session, message])) if message is not None else uuid.uuid4().hex
    )
    memory = Memory(root)
    # Checked before the lock and before any read or write: a paused capture
    # must leave no trace of the text it declined to store, not even a lock
    # file or a queue entry.
    if memory.path(PAUSED).exists():
        return {"ok": True, "status": "paused"}
    with memory.lock():
        # The marker may have appeared while this event waited for the lock.
        # This does not cancel an event already processing while holding it.
        if memory.path(PAUSED).exists():
            return {"ok": True, "status": "paused"}
        rules = validate_rules(json_read(memory.path(RULES)))
        queue = records(memory.path(QUEUE))
        unmatched = records(memory.path(UNMATCHED))
        archive = records(memory.path(ARCHIVE))
        if any(
            row.get("id") == event_id and "legacy_id" not in row
            for row in queue + unmatched + archive
        ):
            return {"ok": True, "status": "duplicate", "id": event_id}
        categories = match_rules(text, rules)
        raw = text.encode("utf-8")
        row = {"id": event_id, "time": now(), "text": text, "text_sha256": digest(raw)}
        if categories:
            row["rules"] = categories
            code = None
            if len(raw) > MATCHED_BYTES:
                code = "CANDIDATE_TOO_LARGE"
            elif (
                len(queue) >= QUEUE_COUNT
                or len(rows_bytes(queue)) + len(encode(row)) > QUEUE_BYTES
            ):
                code = "QUEUE_FULL"
            if code:
                memory.update_state(
                    rejected_candidates=memory.state().get("rejected_candidates", 0) + 1
                )
                memory.diagnostic(code)
                raise OpError(code)
            atomic_write(memory.path(QUEUE), rows_bytes(queue + [row]))
            count = len(queue) + 1
            return {
                "ok": True,
                "status": "queued",
                "id": event_id,
                "queue_count": count,
                "warning_threshold": 200
                if count >= 200
                else 80
                if count >= 80
                else None,
            }
        if len(raw) > UNMATCHED_TEXT_BYTES:
            marker = "\n[truncated]"
            row["text"] = (
                raw[: UNMATCHED_TEXT_BYTES - len(marker.encode())].decode(
                    "utf-8", errors="ignore"
                )
                + marker
            )
            row.update(truncated=True, original_bytes=len(raw))
        kept, invalid = memory.prune_unmatched(unmatched + [row])
        atomic_write(memory.path(UNMATCHED), rows_bytes(kept))
        if invalid:
            memory.diagnostic("INVALID_TIMESTAMP")
        return {"ok": True, "status": "unmatched", "id": event_id}


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    sub.add_parser("status")
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--source", choices=("queue", "unmatched"), default="queue")
    snapshot.add_argument("--public", action="store_true")
    snapshot.add_argument("--topic", action="append", default=[])
    snapshot.add_argument("--metadata-only", action="store_true")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--allow-private", action="store_true")
    prepare.add_argument("--approved-scope")
    commit = sub.add_parser("commit")
    commit.add_argument("--txid", required=True)
    commit.add_argument("--approval-id", required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("--txid", required=True)
    clear = sub.add_parser("clear-unmatched")
    clear.add_argument("--approval-id", required=True)
    reference = sub.add_parser("reference")
    reference.add_argument("scope", choices=("public", "topics", "topic", "private"))
    for option in ("section", "topic", "field", "purpose", "approval-id"):
        reference.add_argument("--" + option)
    sub.add_parser("maintain")
    args = parser.parse_args()
    memory = Memory(args.root)
    if args.command == "status":
        result = memory.status()
    elif args.command == "snapshot":
        result = memory.snapshot(
            args.source, args.public, args.topic, args.metadata_only
        )
    elif args.command == "prepare":
        result = memory.prepare(load_input(), args.allow_private, args.approved_scope)
    elif args.command in ("commit", "recover"):
        result = memory.commit(
            args.txid,
            getattr(args, "approval_id", None),
            recover=args.command == "recover",
        )
        try:
            result["maintenance"] = memory.maintain()
        except Exception as exc:
            result.update(
                ok=False,
                maintenance_error=(
                    exc.code if isinstance(exc, OpError) else "MAINTENANCE_IO_ERROR"
                ),
            )
            emit(result)
            return 1
    elif args.command == "clear-unmatched":
        result = memory.clear_unmatched(
            load_input().get("reviewed_ids"), args.approval_id
        )
    elif args.command == "reference":
        result = memory.reference(
            args.scope,
            args.section,
            args.topic,
            args.field,
            args.purpose,
            args.approval_id,
        )
    else:
        result = getattr(memory, args.command)()
    emit(result)


if __name__ == "__main__":
    sys.exit(run_safe(main))
