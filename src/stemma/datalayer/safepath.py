"""Host writes into the run's tool-only `datalayer/` folder that no symlink can redirect.

The MCP server runs outside the Bash sandbox, so every host write must be
path-contained and follow no symlink (CONTRACTS section 6). The folder is walked from
the run folder one component at a time with directory descriptors opened `O_NOFOLLOW`
(the method of part B's `register/safeio.py`); log lines are appended through such a
descriptor with the file itself opened `O_NOFOLLOW`. SQLite opens its own files by
path, so before each connection the folder walk is repeated and the database file and
its journals are checked not to be links; the folder is tool-only, so an agent cannot
plant one between the check and the open.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from stemma.datalayer.errors import DataLayerError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class UnsafePath(DataLayerError):
    """A data-layer path goes through a symlink, or out of the run folder."""

    code = "unsafe_path"


class AppendFailed(DataLayerError):
    """A log line could not be appended: its file could not be created."""

    code = "append_failed"


# Resource limit: creates of one log file tried before giving up, when each answers
# ENOENT (the macOS race in `_open_append`; one retry has sufficed in every stress run).
_CREATE_TRIES = 20


def _parts(rel: str | PurePosixPath) -> list[str]:
    p = PurePosixPath(rel)
    parts = list(p.parts)
    if p.is_absolute() or not parts or any(x in ("", ".", "..") for x in parts):
        raise UnsafePath(f"{rel} is not a plain path inside the run folder")
    return parts


def _open_dir(root: Path, parts: list[str], create: bool) -> int:
    fd = os.open(os.path.realpath(root), _DIR_FLAGS)
    try:
        for name in parts:
            try:
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass  # another writer made it first; the open below checks what it is
                try:
                    nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise UnsafePath(f"{'/'.join(parts)}: {name} is not a plain folder") from exc
            except OSError as exc:  # ELOOP or ENOTDIR: a link or a file in the way
                raise UnsafePath(f"{'/'.join(parts)}: {name} is not a plain folder") from exc
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def ensure_dir(root: Path, rel: str | PurePosixPath) -> Path:
    """Create root/rel if needed, every component a real folder; return its path."""
    os.close(_open_dir(root, _parts(rel), create=True))
    return Path(os.path.realpath(root)).joinpath(*_parts(rel))


def _open_append(fd_dir: int, name: str, rel: str | PurePosixPath) -> int:
    """`name` in `fd_dir`, opened to append (created if missing), following no link.

    On macOS, openat(dir_fd, name, O_CREAT) can answer ENOENT while other processes
    create the same new file (W3's flaky concurrent sync, 27 Oct); an open by path does
    not. The create is tried again: once a racer has made the file, the open succeeds."""
    for _ in range(_CREATE_TRIES):
        try:
            return os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                           0o644, dir_fd=fd_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:  # ELOOP: the file is a link
            raise UnsafePath(f"{rel} cannot be opened without following a link") from exc
    raise AppendFailed(f"{rel}: creating it answered 'no such file' {_CREATE_TRIES} times; "
                       f"its folder may have been removed")


def append_line(root: Path, rel: str | PurePosixPath, line: str) -> None:
    parts = _parts(rel)
    fd_dir = _open_dir(root, parts[:-1], create=True)
    try:
        fd = _open_append(fd_dir, parts[-1], rel)
        try:
            data = (line + "\n").encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
    finally:
        os.close(fd_dir)


def check_sqlite_file(root: Path, rel: str | PurePosixPath) -> Path:
    """The SQLite file's path after checking the folders and the files are not links."""
    parts = _parts(rel)
    folder = ensure_dir(root, PurePosixPath(*parts[:-1])) if len(parts) > 1 else Path(
        os.path.realpath(root))
    path = folder / parts[-1]
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        try:
            mode = os.lstat(candidate).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise UnsafePath(f"{candidate.name} is not a plain file")
    return path


def write_file(root: Path, rel: str | PurePosixPath, data: bytes) -> None:
    """Atomically replace root/rel with `data`, following no symlink."""
    import secrets

    parts = _parts(rel)
    fd_dir = _open_dir(root, parts[:-1], create=True)
    try:
        tmp = f".{parts[-1]}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=fd_dir)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, parts[-1], src_dir_fd=fd_dir, dst_dir_fd=fd_dir)
    finally:
        os.close(fd_dir)


def read_file(root: Path, rel: str | PurePosixPath) -> bytes | None:
    """root/rel's bytes, opened without following a link; None if it does not exist."""
    parts = _parts(rel)
    try:
        fd_dir = _open_dir(root, parts[:-1], create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd_dir)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UnsafePath(f"{rel} cannot be opened without following a link") from exc
        try:
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(fd_dir)
