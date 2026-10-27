"""The sealed release manifest, `release/manifest.json`, and its check.

At release (`finish_run`, after the `finished` fact) the run folder is sealed: every
regular file not excluded is listed with its relative path, size and SHA-256, sorted by
path; the register's log is recorded separately (length, digest, transactions). Any
symlink or special file (FIFO, socket, device) found while sealing refuses the seal: a
released run holds plain files only. The manifest is canonical JSON (sorted keys,
separators `,` and `:`, UTF-8, no trailing newline); its own SHA-256 goes back to the
caller and is never written into the log or the manifest.

`check` is pure reading: no lock is taken and nothing is written. Every read opens each
path component without following a symlink, as `safeio` does.

Patterns in EXCLUDED are globs on the path relative to the run folder: `*` and `?` stay
within one path component, and `**` spans any number of components (`dir/**` matches the
folder and everything in it; `**/x` matches `x` at any depth).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Final

from stemma.register import safeio
from stemma.register.wm.errors import LogCorruptError
from stemma.register.wm.log import decode_line

MANIFEST: Final = "release/manifest.json"
REGISTER_LOG: Final = "register/log.jsonl"
# Invariant: the manifest's format name; a new name whenever a field's meaning or the
# exclusion list changes.
FORMAT: Final = "stemma-release-manifest/1"
# Resource limit: bytes read per call while hashing (memory, not a judgment).
_CHUNK: Final = 1 << 20

# Never listed, never checked. Invariant for each: it holds no research content, or it is
# written by design after the release, so sealing it would fail every honest run.
EXCLUDED: Final[tuple[str, ...]] = (
    "events.jsonl",  # driver and hook events go on after release (the retrospective)
    "status.json",  # the driver's live status, rewritten while any session runs
    "status.html",  # the same, rendered for people
    "usage.jsonl",  # token accounting of sessions that end after release
    "usage.md",  # rendered again from usage.jsonl on every update
    ".stemma/**",  # driver state, session streams, settings and arrivals, written while
    #   sessions run, the retrospective writer's after release
    "**/*.lock",  # flock files: empty, made by any reader or writer at any time
    "store/index.sqlite*",  # derived search index (WAL): SQLite rewrites it and its
    #   -wal and -shm files on reads and checkpoints; store/text and receipts are sealed
    "register/index.sqlite*",  # derived index, rebuilt from the register log
    "register/calls/**",  # the guard's per-call identity stamps, one per tool call,
    #   the retrospective writer's after release
    "release/**",  # the manifest itself (it cannot hold its own digest) and earlier seals
    "retro.md",  # the retrospective writer, launched by finish_run, writes it after release
    "register/checks.jsonl",  # the retrospective writer's request, launch and end marks
    #   append after release
    ".stemma-tmp/**",  # each session's harness temp folder (command output, not record),
    #   the retrospective session's after release
    "register/tmp/**",  # safeio's temporary files, present only until renamed into place
    "**/*.tmp",  # atomic-write temporaries (tree.json.tmp and the like), renamed away
    "**/*.torn",  # sidecars where the next append moves a crashed writer's partial line;
    "**/*.torn-*",  #   that append may come after release
    "**/.DS_Store",  # macOS Finder writes it whenever someone opens the folder
)


class ManifestError(Exception):
    """The run folder cannot be sealed (a required file missing, an entry unreadable)."""


class UnsealableEntry(ManifestError):
    """A symlink or special file lies in the run folder: a release holds plain files."""


class LogError(Exception):
    """The register log cannot be read as committed JSON-object lines."""


class LogCorrupt(LogError):
    """A newline-terminated line of the register log is not a JSON object."""


class LogTorn(LogError):
    """The log's bytes end without a newline (a writer mid-append, or one that died).
    `records` holds the complete records before the tail."""

    def __init__(self, message: str, records: list[dict[str, Any]], tail: int) -> None:
        super().__init__(message)
        self.records = records
        self.tail_bytes = tail


# ------------------------------------------------------------------ the register log
def parse_log(data: bytes) -> list[dict[str, Any]]:
    """The records of a register log's bytes. A line that is not a JSON object raises
    LogCorrupt; bytes after the last newline raise LogTorn, never skipped silently."""
    committed = data.rfind(b"\n") + 1
    records: list[dict[str, Any]] = []
    offset = 0
    for line in data[:committed].splitlines(keepends=True):
        try:
            records.append(decode_line(line, f"{REGISTER_LOG} at byte {offset}"))
        except LogCorruptError as exc:
            raise LogCorrupt(str(exc)) from exc
        offset += len(line)
    if committed != len(data):
        tail = len(data) - committed
        raise LogTorn(f"{REGISTER_LOG} ends in {tail} bytes with no newline (a writer "
                      f"mid-append, or one that died); the {len(records)} complete "
                      f"records before them are intact", records, tail)
    return records


def read_log(run_dir: Path) -> list[dict[str, Any]]:
    """Every record of `register/log.jsonl`, read without a lock and without following a
    symlink (see parse_log for what is refused). A missing log raises FileNotFoundError."""
    try:
        data = safeio.read_bytes(run_dir, REGISTER_LOG)
    except (safeio.UnsafePath, NotADirectoryError) as exc:
        raise LogError(f"{REGISTER_LOG} is not a plain file: {exc}") from exc
    return parse_log(data)


# ------------------------------------------------------------------------- rules
@cache
def _pattern(glob: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out, i = out + "(?:/.*)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out)


def excluded(rel: str) -> bool:
    """Whether a path relative to the run folder is excluded from the seal."""
    return any(_pattern(g).fullmatch(rel) for g in EXCLUDED)


# -------------------------------------------------------------------------- walk
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_TYPES: Final = ((stat.S_ISREG, "file"), (stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"),
                 (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "character device"),
                 (stat.S_ISBLK, "block device"), (stat.S_ISDIR, "folder"))


def _type(mode: int) -> str:
    return next((name for test, name in _TYPES if test(mode)), "special file")


@dataclass(frozen=True)
class _Entry:
    rel: str
    dir_fd: int
    name: str
    kind: str  # "file", "symlink", "fifo", ...; never "folder" (folders are walked)

    def what(self) -> str:
        if self.kind == "symlink":
            return f"a symlink to {os.readlink(self.name, dir_fd=self.dir_fd)}"
        return f"a {self.kind}"


def _walk(run_dir: Path, errors: list[str]) -> Iterator[_Entry]:
    """Every entry under run_dir that is not a folder and not excluded, folders opened
    without following links (an excluded folder is not entered). An entry that cannot
    be read is reported in `errors`."""
    root = os.open(os.path.realpath(run_dir), _DIR_FLAGS)
    try:
        yield from _walk_fd(root, "", errors)
    finally:
        os.close(root)


def _walk_fd(fd: int, prefix: str, errors: list[str]) -> Iterator[_Entry]:
    try:
        names = sorted(os.listdir(fd))
    except OSError as exc:
        errors.append(f"unreadable: {prefix or './'} ({exc.strerror})")
        return
    for name in names:
        rel = f"{prefix}{name}"
        if excluded(rel):
            continue
        try:
            kind = _type(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            continue  # gone between listing and stat: it is simply not there
        except OSError as exc:
            errors.append(f"unreadable: {rel} ({exc.strerror})")
            continue
        if kind != "folder":
            yield _Entry(rel, fd, name, kind)
            continue
        try:
            sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
        except OSError as exc:  # replaced by a link or a file since the stat
            errors.append(f"unreadable: {rel}/ ({exc.strerror})")
            continue
        try:
            yield from _walk_fd(sub, f"{rel}/", errors)
        finally:
            os.close(sub)


def _read(e: _Entry) -> bytes | tuple[int, str]:
    """The bytes of the register log, or (size, SHA-256) of any other regular file;
    opened without following a link or blocking on a FIFO. OSError when the entry is no
    longer a regular file."""
    fd = os.open(e.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=e.dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{e.rel} is no longer a regular file")
        os.set_blocking(fd, True)
        if e.rel == REGISTER_LOG:
            chunks = []
            while chunk := os.read(fd, _CHUNK):
                chunks.append(chunk)
            return b"".join(chunks)
        h, size = hashlib.sha256(), 0
        while chunk := os.read(fd, _CHUNK):
            h.update(chunk)
            size += len(chunk)
        return size, h.hexdigest()
    finally:
        os.close(fd)


def _digest(e: _Entry) -> tuple[int, str, bytes | None]:
    got = _read(e)
    if isinstance(got, bytes):
        return len(got), hashlib.sha256(got).hexdigest(), got
    return got[0], got[1], None


# -------------------------------------------------------------------------- seal
def canonical(manifest: dict[str, Any]) -> bytes:
    """The manifest's bytes: sorted keys, no spaces, UTF-8, no trailing newline."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def seal(run_dir: Path, version: int, run_id: str | None = None) -> dict[str, Any]:
    """Write `release/manifest.json` for the run folder as it is now, for released draft
    `version` (run_id read from run.json when not given), and return what to record
    beside the release: the manifest's path and own SHA-256, the file count, the log
    record. An earlier manifest is kept as `release/manifest.<n>.json`. Raises
    UnsealableEntry for a symlink or special file, LogError for a torn or corrupt
    register log, and ManifestError for anything else that stops the seal."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ManifestError(f"cannot seal {run_dir}: version {version!r} is not a positive "
                            f"whole number")
    run_id = run_id if run_id is not None else _run_id(run_dir)
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    log: dict[str, Any] | None = None
    try:
        for e in _walk(run_dir, errors):
            if e.kind != "file":
                raise UnsealableEntry(f"cannot seal {run_dir}: {e.rel} is {e.what()}; a "
                                      f"released run holds plain files only")
            size, sha, data = _digest(e)
            files.append({"path": e.rel, "size": size, "sha256": sha})
            if data is not None:
                log = {"path": REGISTER_LOG, "bytes": size, "sha256": sha,
                       "transactions": len(parse_log(data))}
    except OSError as exc:
        raise ManifestError(f"cannot seal {run_dir}: {exc}") from exc
    if errors:
        raise ManifestError(f"cannot seal {run_dir}: " + "; ".join(errors))
    if log is None:
        raise ManifestError(f"cannot seal {run_dir}: {REGISTER_LOG} is missing")
    files.sort(key=lambda f: str(f["path"]))
    manifest = {
        "format": FORMAT,
        "run_id": run_id,
        "version": version,
        "released_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"),
        "excluded": list(EXCLUDED),
        "log": log,
        "files": files,
    }
    data = canonical(manifest)
    try:
        _keep_previous(run_dir)
        safeio.write_bytes(run_dir, MANIFEST, data)
    except (OSError, safeio.UnsafePath) as exc:
        raise ManifestError(f"cannot write {MANIFEST}: {exc}") from exc
    return {"manifest": MANIFEST, "manifest_sha256": hashlib.sha256(data).hexdigest(),
            "files": len(files), "log": log}


def _run_id(run_dir: Path) -> str:
    try:
        run_id = json.loads(safeio.read_bytes(run_dir, "run.json")).get("run_id")
    except (OSError, ValueError, AttributeError, safeio.UnsafePath) as exc:
        raise ManifestError(f"cannot seal {run_dir}: run.json is unreadable ({exc}); pass "
                            f"run_id") from exc
    if not isinstance(run_id, str) or not run_id:
        raise ManifestError(f"cannot seal {run_dir}: run.json has no run_id; pass run_id")
    return run_id


def _keep_previous(run_dir: Path) -> None:
    try:
        safeio.read_bytes(run_dir, MANIFEST)
    except FileNotFoundError:
        return
    n = 1
    while safeio.is_file(run_dir, f"release/manifest.{n}.json"):
        n += 1
    safeio.move(run_dir, MANIFEST, f"release/manifest.{n}.json")


# ------------------------------------------------------------------------- check
def check(run_dir: Path, manifest_sha256: str | None = None) -> list[str]:
    """Every difference between the run folder and its sealed manifest, one plain line
    each; an empty list when the folder matches. When `manifest_sha256` is given, a
    manifest whose own digest differs is a difference. Pure reading: no lock, no write."""
    try:
        data = safeio.read_bytes(run_dir, MANIFEST)
    except FileNotFoundError:
        return [f"{MANIFEST} is missing: the run was never sealed, or its seal was removed"]
    except (safeio.UnsafePath, NotADirectoryError, OSError) as exc:
        return [f"{MANIFEST} is not a plain file: {exc}"]
    out: list[str] = []
    actual = hashlib.sha256(data).hexdigest()
    if manifest_sha256 is not None and actual != manifest_sha256:
        out.append(f"{MANIFEST}: its SHA-256 is {actual}, not the pinned {manifest_sha256}")
    try:
        manifest = json.loads(data)
        sealed = {str(f["path"]): f for f in manifest["files"]}
        log = manifest["log"]
        sealed_log = (int(log["bytes"]), str(log["sha256"]), int(log["transactions"]))
    except (ValueError, KeyError, TypeError) as exc:
        return [*out, f"{MANIFEST} is not a readable manifest ({type(exc).__name__}: {exc})"]
    if manifest.get("format") != FORMAT or manifest.get("excluded") != list(EXCLUDED):
        out.append(f"{MANIFEST} was sealed under other rules ({manifest.get('format')}) than "
                   f"this build checks ({FORMAT})")
    errors: list[str] = []
    seen: set[str] = set()
    try:
        for e in _walk(run_dir, errors):
            seen.add(e.rel)
            try:
                line = _compare(e, sealed.get(e.rel), sealed_log)
            except OSError as exc:  # gone or replaced while being read
                line = f"unreadable: {e.rel} ({exc})"
            if line:
                out.append(line)
    except OSError as exc:
        return [*out, f"cannot read the run folder {run_dir}: {exc}"]
    out.extend(errors)
    for rel, f in sorted(sealed.items()):
        if rel not in seen and not excluded(rel):
            out.append(f"register log: {rel} is missing (sealed at {sealed_log[0]} bytes)"
                       if rel == REGISTER_LOG else
                       f"removed: {rel} (sealed with {f.get('size')} bytes, missing now)")
    return out


def _compare(e: _Entry, f: dict[str, Any] | None,
             sealed_log: tuple[int, str, int]) -> str | None:
    if e.kind != "file":
        return f"not a plain file: {e.rel} is {e.what()}" + (
            "" if f is None else ", sealed as a plain file")
    if f is None:
        return f"added: {e.rel} (not in the seal)"
    size, sha, data = _digest(e)
    if data is not None:
        return _compare_log(data, sealed_log)
    if size == f.get("size") and sha == f.get("sha256"):
        return None
    return f"changed: {e.rel} ({f.get('size')} bytes sealed, {size} now; contents differ)"


def _compare_log(data: bytes, sealed_log: tuple[int, str, int]) -> str | None:
    want, sha, txs = sealed_log
    if len(data) == want and hashlib.sha256(data).hexdigest() == sha:
        return None
    if len(data) < want:
        return (f"register log: {REGISTER_LOG} is shorter than when sealed ({len(data)} "
                f"bytes now, {want} sealed)")
    if hashlib.sha256(data[:want]).hexdigest() != sha:
        return f"register log: the {want} bytes of {REGISTER_LOG} sealed at release changed"
    try:
        later = f"{len(parse_log(data)) - txs} transactions"
    except LogError as exc:
        later = f"bytes that are not whole records ({exc})"
    return (f"register log: {len(data) - want} bytes appended to {REGISTER_LOG} after the "
            f"seal ({later}), outside the sealed log")
