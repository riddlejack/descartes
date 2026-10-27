"""Read-only, confined, bounded reads of a run folder.

Every read here:
- takes a path relative to the run folder, checked before anything is opened (plain
  segments only: no absolute path, no `.` or `..`, no empty segment);
- walks to it one folder at a time with descriptors opened `O_NOFOLLOW | O_DIRECTORY`,
  so a symlink anywhere on the way is refused, never followed;
- opens the file `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` and refuses anything but a
  regular file (a FIFO, device or socket is never read);
- reads at most a stated number of bytes, refusing a larger file before reading it.
Nothing here creates, locks or writes a file. A refusal is an `UnsafeRead`, which is a
`RunReadError`, so every caller turns it into a named failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from stemma.report.errors import RunReadError

# Resource limits: bytes of one file read whole; bytes of one JSON file; bytes hashed in
# one file; regular files, folder depth and bytes walked in one run folder.
FILE_MAX_BYTES: Final = 256 * 1024 * 1024
JSON_MAX_BYTES: Final = 64 * 1024 * 1024
HASH_MAX_BYTES: Final = 1024 * 1024 * 1024
WALK_MAX_FILES: Final = 250_000
WALK_MAX_DEPTH: Final = 32
WALK_MAX_BYTES: Final = 16 * 1024 * 1024 * 1024
# Resource limit: bytes read at a time while hashing.
CHUNK: Final = 1 << 20
# Logical invariant: what one path segment inside a run may be.
SEGMENT: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+=~,-]{0,254}")
_DIR: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class UnsafeRead(RunReadError):
    """A path is not a plain path inside the run, goes through a link, names something
    other than a regular file, or is larger than a resource limit."""


class Missing(UnsafeRead):
    """The path names nothing."""


def parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        raise UnsafeRead(f"{rel!r} is not a path inside the run folder")
    segs = rel.split("/")
    if any(s in ("", ".", "..") or not SEGMENT.fullmatch(s) for s in segs):
        raise UnsafeRead(f"{rel!r} is not a plain path inside the run folder")
    return segs


@contextmanager
def _folder(root: Path, segs: list[str], rel: str) -> Iterator[int]:
    try:
        fd = os.open(os.path.realpath(root), _DIR)
    except OSError as exc:
        raise UnsafeRead(f"{root} is not a readable folder") from exc
    try:
        for name in segs:
            try:
                nxt = os.open(name, _DIR, dir_fd=fd)
            except FileNotFoundError as exc:
                raise Missing(f"{rel} does not exist") from exc
            except OSError as exc:  # ELOOP / ENOTDIR: a link or a file in the way
                raise UnsafeRead(f"{rel}: {name} is not a plain folder") from exc
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_regular(root: Path, rel: str, cap: int) -> Iterator[tuple[int, int]]:
    """(descriptor, size) of the regular file root/rel, refused above `cap` bytes."""
    segs = parts(rel)
    with _folder(root, segs[:-1], rel) as dfd:
        try:
            fd = os.open(segs[-1], _FILE, dir_fd=dfd)
        except FileNotFoundError as exc:
            raise Missing(f"{rel} does not exist") from exc
        except OSError as exc:
            raise UnsafeRead(f"{rel} is a link or not a plain file") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafeRead(f"{rel} is not a plain file")
            if st.st_nlink != 1:  # a hard link can name a file outside the run
                raise UnsafeRead(f"{rel} has more than one name (a hard link)")
            if st.st_size > cap:
                raise UnsafeRead(f"{rel} is {st.st_size:,} bytes, more than the {cap:,} "
                                 f"read (a resource limit)")
            os.set_blocking(fd, True)
            yield fd, st.st_size
        finally:
            os.close(fd)


def read_bytes(root: Path, rel: str, cap: int = FILE_MAX_BYTES) -> bytes:
    with open_regular(root, rel, cap) as (fd, _size):
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, CHUNK):
            total += len(chunk)
            if total > cap:  # the file grew while it was read
                raise UnsafeRead(f"{rel} is larger than the {cap:,} bytes read")
            chunks.append(chunk)
        return b"".join(chunks)


def read_text(root: Path, rel: str, cap: int = FILE_MAX_BYTES) -> str:
    try:
        return read_bytes(root, rel, cap).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsafeRead(f"{rel} is not UTF-8 text") from exc


def read_json(root: Path, rel: str, cap: int = JSON_MAX_BYTES) -> Any:
    data = read_bytes(root, rel, cap)
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise UnsafeRead(f"{rel} is not JSON a reader can take "
                         f"({type(exc).__name__})") from exc


def maybe(fn: Any, *args: Any) -> Any:
    """`fn(*args)`, or None when the path names nothing."""
    try:
        return fn(*args)
    except Missing:
        return None


def sha256(root: Path, rel: str, cap: int = HASH_MAX_BYTES) -> tuple[str, int]:
    """(SHA-256, size) of a regular file, streamed."""
    with open_regular(root, rel, cap) as (fd, _size):
        h = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, CHUNK):
            total += len(chunk)
            if total > cap:
                raise UnsafeRead(f"{rel} is larger than the {cap:,} bytes hashed")
            h.update(chunk)
        return h.hexdigest(), total


def is_regular(root: Path, rel: str) -> bool:
    try:
        with open_regular(root, rel, HASH_MAX_BYTES):
            return True
    except UnsafeRead:
        return False


def listdir(root: Path, rel: str) -> list[str]:
    """Names in the folder root/rel (empty when it does not exist)."""
    try:
        with _folder(root, parts(rel) if rel else [], rel or ".") as fd:
            return sorted(os.listdir(fd))
    except Missing:
        return []


def walk(root: Path) -> tuple[dict[str, int], list[str]]:
    """Every regular file under root as {relative path: size}, and every problem met:
    a link, a special file, a name that is not a plain segment, or a limit reached.
    Iterative; never follows a link."""
    files: dict[str, int] = {}
    problems: list[str] = []
    total = 0
    stack: list[tuple[str, int]] = [("", 0)]
    while stack:
        rel, depth = stack.pop()
        if depth > WALK_MAX_DEPTH:
            problems.append(f"{rel}: deeper than {WALK_MAX_DEPTH} folders (a resource limit)")
            continue
        try:
            with _folder(root, parts(rel) if rel else [], rel or ".") as fd:
                for name in sorted(os.listdir(fd)):
                    child = f"{rel}/{name}" if rel else name
                    if not SEGMENT.fullmatch(name):
                        problems.append(f"{child!r}: not a plain file name")
                        continue
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append((child, depth + 1))
                    elif stat.S_ISREG(st.st_mode):
                        files[child] = st.st_size
                        total += st.st_size
                    elif stat.S_ISLNK(st.st_mode):
                        problems.append(f"{child}: a symbolic link")
                    else:
                        problems.append(f"{child}: not a regular file")
                    if len(files) > WALK_MAX_FILES or total > WALK_MAX_BYTES:
                        problems.append(f"more than {WALK_MAX_FILES:,} files or "
                                        f"{WALK_MAX_BYTES:,} bytes (a resource limit)")
                        return files, problems
        except UnsafeRead as exc:
            problems.append(str(exc))
    return files, problems
