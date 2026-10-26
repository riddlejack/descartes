"""The driver's writes inside a run folder, none of which follows a planted symlink.

Whole-file writes go through `stemma.register.safeio` (temporary files in the tool-only
`register/tmp/`, renamed into place relative to directory descriptors opened
`O_NOFOLLOW`). Appends (session streams) and the driver's lock open their file the same
way: every folder on the path a real folder, the last component opened `O_NOFOLLOW`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import IO, Any

from stemma.register import safeio
from stemma.register.safeio import UnsafePath

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

__all__ = ["UnsafePath", "open_nofollow", "write_bytes", "write_json", "write_text"]


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
                os.mkdir(name, 0o755, dir_fd=fd)
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise UnsafePath(f"{rel}: {name} is not a plain folder") from exc
            os.close(fd)
            fd = nxt
        try:
            return os.open(parts[-1], flags | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise UnsafePath(f"{rel} is not a plain file") from exc
    finally:
        os.close(fd)


def open_append(run_dir: Path, rel: str) -> IO[bytes]:
    return os.fdopen(open_nofollow(run_dir, rel, os.O_WRONLY | os.O_APPEND), "ab")
