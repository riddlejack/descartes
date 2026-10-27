"""Storage for the append-only log: in memory, or a JSONL file shared by processes.

One line is one transaction (one mutation and the events it appended). A line is
committed when its newline is written and fsync'd. Writers hold an exclusive
`fcntl.flock` on `<log>.lock` for the whole read-validate-append step; readers
need no lock, because they fold only newline-terminated lines and the only
change to the file other than an append is the removal of an uncommitted torn
tail, which happens under the lock and lies past every reader's cursor.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from .errors import LogCorruptError, StorageError


def encode_line(tx: Any) -> bytes:
    text = json.dumps(tx, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8") + b"\n"


def decode_line(line: bytes, where: str) -> dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LogCorruptError(f"{where}: committed line is not JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise LogCorruptError(f"{where}: committed line is not a transaction object")
    return value


class Log(Protocol):
    """What the engine needs from a storage backend. Cursors are opaque integers."""

    def exclusive(self) -> Any: ...

    def read_from(self, cursor: int) -> tuple[list[dict[str, Any]], int]: ...

    def torn_tail(self, cursor: int) -> bytes: ...

    def discard_tail(self, cursor: int, torn: bytes) -> str: ...

    def append(self, tx: Any) -> None: ...


class MemoryLog:
    """The same contract in process memory (lines kept encoded, so a memory world
    and a file world fold exactly the same bytes)."""

    def __init__(self) -> None:
        self._lines: list[bytes] = []

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        yield

    def read_from(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        txs = [
            decode_line(line, f"memory log line {cursor + i + 1}")
            for i, line in enumerate(self._lines[cursor:])
        ]
        return txs, len(self._lines)

    def torn_tail(self, cursor: int) -> bytes:
        return b""

    def discard_tail(self, cursor: int, torn: bytes) -> str:
        return ""

    def append(self, tx: Any) -> None:
        self._lines.append(encode_line(tx))


class BytesLog:
    """A read-only log over given bytes (a sealed prefix of a file log): it folds exactly
    those bytes. A tail without its newline is refused, never skipped; every write is
    refused."""

    def __init__(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise StorageError("a bytes log needs bytes")
        if data and not data.endswith(b"\n"):
            raise StorageError("the log bytes end inside a line")
        self._data = data

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        raise StorageError("a bytes log is read-only")
        yield  # pragma: no cover

    def read_from(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        txs: list[dict[str, Any]] = []
        offset = cursor
        for line in self._data[cursor:].splitlines(keepends=True):
            txs.append(decode_line(line, f"log bytes at byte {offset}"))
            offset += len(line)
        return txs, len(self._data)

    def torn_tail(self, cursor: int) -> bytes:
        return b""

    def discard_tail(self, cursor: int, torn: bytes) -> str:
        raise StorageError("a bytes log is read-only")

    def append(self, tx: Any) -> None:
        raise StorageError("a bytes log is read-only")


class FileLog:
    """A JSONL log on disk, safe for several processes writing at once."""

    def __init__(self, path: str | os.PathLike[str], read_only: bool = False) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.read_only = read_only
        if not self.path.parent.is_dir():
            raise StorageError(f"log folder does not exist: {self.path.parent}")
        try:
            if read_only:  # a reader: opens nothing for writing, creates nothing
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            else:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.close(fd)
        except OSError as exc:
            raise StorageError(f"cannot open log {self.path}: {exc}") from exc

    def _refuse_if_read_only(self) -> None:
        if self.read_only:
            raise StorageError(f"{self.path} is open read-only")

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        self._refuse_if_read_only()
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise StorageError(f"cannot open lock file {self.lock_path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_bytes(self, cursor: int) -> bytes:
        try:
            with open(self.path, "rb") as f:
                f.seek(cursor)
                return f.read()
        except OSError as exc:
            raise StorageError(f"cannot read log {self.path}: {exc}") from exc

    def read_from(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        data = self._read_bytes(cursor)
        end = data.rfind(b"\n")
        if end < 0:
            return [], cursor
        txs: list[dict[str, Any]] = []
        offset = cursor
        for line in data[: end + 1].splitlines(keepends=True):
            txs.append(decode_line(line, f"{self.path} at byte {offset}"))
            offset += len(line)
        return txs, cursor + end + 1

    def torn_tail(self, cursor: int) -> bytes:
        """Bytes past the last committed line. Only meaningful under the exclusive
        lock, where no writer is mid-append: they are a crashed writer's leftovers.
        A newline among them means a committed line appeared while we held the
        lock, i.e. some writer is not taking it; that is refused, never cut."""
        tail = self._read_bytes(cursor)
        if b"\n" in tail:
            raise StorageError(
                f"{self.path} grew while the exclusive lock was held: a writer is not using "
                f"{self.lock_path}"
            )
        return tail

    def discard_tail(self, cursor: int, torn: bytes) -> str:
        """Move an uncommitted torn tail to a sidecar file and cut it from the log,
        so the next append starts on a line boundary. Returns the sidecar path."""
        self._refuse_if_read_only()
        sidecar = self.path.with_name(f"{self.path.name}.torn-{cursor}")
        if self.path.stat().st_size != cursor + len(torn):
            raise StorageError(f"{self.path} changed while its torn tail was being discarded")
        try:
            sidecar.write_bytes(torn)
            os.truncate(self.path, cursor)
        except OSError as exc:
            raise StorageError(f"cannot discard torn tail of {self.path}: {exc}") from exc
        return str(sidecar)

    def append(self, tx: Any) -> None:
        self._refuse_if_read_only()
        line = encode_line(tx)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        except OSError as exc:
            raise StorageError(f"cannot open log {self.path} for append: {exc}") from exc
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except OSError as exc:
            raise StorageError(f"append to {self.path} failed: {exc}") from exc
        finally:
            os.close(fd)
