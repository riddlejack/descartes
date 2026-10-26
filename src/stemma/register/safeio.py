"""Host writes and reads inside a run folder that no planted symlink can redirect.

The MCP server and the hooks run outside the Bash sandbox, so a symlink an agent made
(`tree.md -> CLAUDE.md`) would otherwise turn a host write into a write anywhere. Every
host write here walks the path from the run folder one component at a time with
directory file descriptors opened `O_NOFOLLOW`, refuses any component that is a symlink
or not a directory, writes the bytes to a temporary file inside the tool-only
`register/tmp/`, and renames it into place relative to those descriptors. A rename
replaces whatever name is at the target (a symlink included) and never follows it.
Reads of agent-written files open the final component `O_NOFOLLOW` the same way.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class UnsafePath(Exception):
    """A path inside the run folder goes through a symlink or out of the folder."""


def _parts(rel: str | PurePosixPath) -> list[str]:
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts:
        raise UnsafePath(f"{rel} is not a path inside the run folder")
    parts = list(p.parts)
    if any(x in ("", ".", "..") for x in parts):
        raise UnsafePath(f"{rel} is not a plain path inside the run folder")
    return parts


@contextmanager
def _dir_fd(run_dir: Path, parts: list[str], create: bool) -> Iterator[int]:
    """A descriptor for run_dir/parts..., every component a real directory."""
    fd = os.open(os.path.realpath(run_dir), _DIR_FLAGS)
    try:
        for name in parts:
            try:
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o755, dir_fd=fd)
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:  # ELOOP / ENOTDIR: a symlink or a file in the way
                raise UnsafePath(f"{'/'.join(parts)}: {name} is not a plain folder") from exc
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def write_bytes(run_dir: Path, rel: str | PurePosixPath, data: bytes) -> None:
    """Atomically replace run_dir/rel with `data`, following no symlink."""
    parts = _parts(rel)
    name = parts[-1]
    with _dir_fd(run_dir, ["register", "tmp"], create=True) as tmp_fd, \
            _dir_fd(run_dir, parts[:-1], create=True) as dst_fd:
        tmp_name = f"{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                     dir_fd=tmp_fd)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp_name, name, src_dir_fd=tmp_fd, dst_dir_fd=dst_fd)
        except OSError:
            os.unlink(tmp_name, dir_fd=tmp_fd)
            raise


def write_text(run_dir: Path, rel: str | PurePosixPath, text: str) -> None:
    write_bytes(run_dir, rel, text.encode("utf-8"))


def read_bytes(run_dir: Path, rel: str | PurePosixPath) -> bytes:
    """The bytes of a regular file run_dir/rel, refusing any symlink on the way."""
    parts = _parts(rel)
    try:
        with _dir_fd(run_dir, parts[:-1], create=False) as dfd:
            try:
                # O_NONBLOCK: opening a planted FIFO must not hang the server.
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=dfd)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise
                raise UnsafePath(f"{rel} is not a plain file") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise UnsafePath(f"{rel} is not a plain file")
                os.set_blocking(fd, True)
                chunks = []
                while chunk := os.read(fd, 1 << 20):
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)
    except FileNotFoundError:
        raise FileNotFoundError(f"{rel} does not exist") from None


def read_text(run_dir: Path, rel: str | PurePosixPath) -> str:
    return read_bytes(run_dir, rel).decode("utf-8")


def is_file(run_dir: Path, rel: str | PurePosixPath) -> bool:
    """True only for a regular file reached without any symlink."""
    try:
        read_bytes(run_dir, rel)
    except (FileNotFoundError, UnsafePath, NotADirectoryError):
        return False
    return True
