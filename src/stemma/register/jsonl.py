"""Append-only JSON-lines files shared by several processes.

Every writer takes an exclusive `fcntl.flock` on a lock file beside the log, appends
whole lines, flushes and fsyncs, then releases. Readers never lock: a reader sees only
whole lines, because a partial trailing line (a writer mid-append) is left for the
next read. A writer that finds a torn last line (a writer that died mid-append) moves
it to `<log>.torn` before appending, so the next record never fuses with it. Files are
opened `O_NOFOLLOW`: a symlink planted at a log's name is refused, never followed.
Used for `events.jsonl` (driver and hooks) and the register's own records.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class LogCorrupt(Exception):
    """A complete line in an append-only log is not a JSON object."""


def lock_path(log: Path) -> Path:
    return log.with_name(log.name + ".lock")


@contextmanager
def exclusive(log: Path) -> Iterator[None]:
    """Hold the log's exclusive lock for the duration of the block."""
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(log), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _encode(record: dict[str, Any]) -> bytes:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if "\n" in line:  # invariant: json.dumps escapes newlines; one record is one line
        raise ValueError("record encodes to more than one line")
    return (line + "\n").encode("utf-8")


def _drop_torn_tail(fd: int, log: Path) -> None:
    size = os.fstat(fd).st_size
    if size == 0:
        return
    os.lseek(fd, max(0, size - (1 << 20)), os.SEEK_SET)
    tail = os.read(fd, 1 << 20)
    if tail.endswith(b"\n"):
        return
    cut = tail.rfind(b"\n")
    keep = size - len(tail) + cut + 1 if cut >= 0 else 0
    if cut < 0 and size > len(tail):
        raise LogCorrupt(f"{log}: the last line is longer than a record can be")
    torn = tail[cut + 1:] if cut >= 0 else tail
    side = os.open(str(log) + ".torn", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                   0o644)
    try:
        os.write(side, torn + b"\n")
    finally:
        os.close(side)
    os.ftruncate(fd, keep)


def append_unlocked(log: Path, records: list[dict[str, Any]]) -> None:
    """Append records; the caller must already hold `exclusive(log)`."""
    data = b"".join(_encode(r) for r in records)
    fd = os.open(log, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        _drop_torn_tail(fd, log)
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short write to {log}: {written} of {len(data)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)


def append(log: Path, *records: dict[str, Any]) -> None:
    """Append records under the log's exclusive lock."""
    with exclusive(log):
        append_unlocked(log, list(records))


def read_from(log: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Return the complete records after byte `offset`, and the offset after them."""
    if not log.exists():
        return [], offset
    fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    records: list[dict[str, Any]] = []
    for n, raw in enumerate(data[: end + 1].splitlines()):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LogCorrupt(f"{log}: line {n + 1} after offset {offset} is not JSON") from exc
        if not isinstance(rec, dict):
            raise LogCorrupt(f"{log}: line {n + 1} after offset {offset} is not an object")
        records.append(rec)
    return records, offset + end + 1


def read_all(log: Path) -> list[dict[str, Any]]:
    return read_from(log, 0)[0]
