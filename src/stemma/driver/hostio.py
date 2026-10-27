"""The driver's writes inside a run folder, none of which follows a planted symlink.

Whole-file writes go through `stemma.register.safeio` (temporary files in the tool-only
`register/tmp/`, renamed into place relative to directory descriptors opened
`O_NOFOLLOW`). Appends (session streams) and the driver's lock open their file the same
way: every folder on the path a real folder, the last component opened `O_NOFOLLOW`.
Both are safe when several processes make the same new folders or file at once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import IO, Any

from stemma.register import safeio
from stemma.register.safeio import UnsafePath

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Resource limit: creates of one file tried before giving up, when each answers ENOENT
# (the macOS race in `_create`; one retry has sufficed in every stress run).
_CREATE_TRIES = 20

__all__ = ["CreateFailed", "UnsafePath", "open_nofollow", "write_bytes", "write_json",
           "write_text"]


class CreateFailed(Exception):
    """A file could not be created: every try answered 'no such file'."""


def write_text(run_dir: Path, rel: str, text: str) -> None:
    safeio.write_text(run_dir, rel, text)


def write_bytes(run_dir: Path, rel: str, data: bytes) -> None:
    safeio.write_bytes(run_dir, rel, data)


def write_json(run_dir: Path, rel: str, data: Any) -> None:
    safeio.write_text(run_dir, rel, json.dumps(data, indent=1, sort_keys=True,
                                               ensure_ascii=False) + "\n")


def open_nofollow(run_dir: Path, rel: str, flags: int) -> int:
    """A descriptor for run_dir/rel opened with `flags | O_CREAT | O_NOFOLLOW`, every
    folder on the way created if missing and refused if it is a symlink."""
    parts = list(PurePosixPath(rel).parts)
    if not parts or PurePosixPath(rel).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"{rel} is not a plain path inside the run folder")
    fd = os.open(os.path.realpath(run_dir), _DIR_FLAGS)
    try:
        for name in parts[:-1]:
            try:
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass  # another process made it first: open what is there, below
                # Whoever made it, it is opened without following a link: a link or a
                # file swapped in at the name is ELOOP / ENOTDIR, refused here.
                try:
                    nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise UnsafePath(f"{rel}: {name} is not a plain folder") from exc
            except OSError as exc:
                raise UnsafePath(f"{rel}: {name} is not a plain folder") from exc
            os.close(fd)
            fd = nxt
        return _create(fd, parts[-1], flags, rel)
    finally:
        os.close(fd)


def _create(fd_dir: int, name: str, flags: int, rel: str) -> int:
    """`name` in `fd_dir` opened with `flags | O_CREAT | O_NOFOLLOW`.

    On macOS, openat(dir_fd, name, O_CREAT) can answer ENOENT while other processes
    create the same new file (the race W3 found in datalayer/, 27 Oct); an open by path
    does not. The create is tried again: once a racer has made the file, it opens."""
    for _ in range(_CREATE_TRIES):
        try:
            return os.open(name, flags | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=fd_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:  # ELOOP: the file is a link
            raise UnsafePath(f"{rel} is not a plain file") from exc
    raise CreateFailed(f"{rel}: creating it answered 'no such file' {_CREATE_TRIES} times; "
                       f"its folder may have been removed")


def open_append(run_dir: Path, rel: str) -> IO[bytes]:
    return os.fdopen(open_nofollow(run_dir, rel, os.O_WRONLY | os.O_APPEND), "ab")
