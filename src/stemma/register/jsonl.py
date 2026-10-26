"""Append-only JSON-lines files shared by several processes.

Every writer takes an exclusive `fcntl.flock` on a lock file beside the log, appends
whole lines, flushes and fsyncs, then releases. Readers never lock: a reader sees only
whole lines, because a partial trailing line (a writer mid-append) is left for the
next read. Used for `events.jsonl` (driver and hooks) and the register's own records.
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
    fd = os.open(lock_path(log), os.O_RDWR | os.O_CREAT, 0o644)
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


def append_unlocked(log: Path, records: list[dict[str, Any]]) -> None:
    """Append records; the caller must already hold `exclusive(log)`."""
    data = b"".join(_encode(r) for r in records)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
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
    with open(log, "rb") as f:
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
