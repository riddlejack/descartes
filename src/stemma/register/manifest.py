"""The sealed release manifest, `release/manifest.json`, and its check.

At release (`finish_run`, after the `finished` fact) the run folder is sealed: every
regular file is listed with its relative path, size and SHA-256, sorted by path, except
the operational files named one by one in EXCLUDED and the two driver-owned folders in
EXCLUDED_FOLDERS. No pattern, suffix or other folder is excluded, so nothing an agent can
name escapes the seal. Two append-only records are also sealed as prefixes: the register's
log (`log`, also listed in `files`) and the check records (`checks`, kept out of `files`:
they grow after release, and `after_seal` names what came later), each with its length,
the SHA-256 of those bytes and its number of records. New identity stamps may appear in
`register/calls/`; sealed ones may not change. Any symlink or special file (FIFO, socket, device) found
while sealing refuses the seal: a released run holds plain files only. Under `release/`
only the manifest and earlier manifests kept by exact name may exist.

The manifest is canonical JSON (sorted keys, separators `,` and `:`, UTF-8, no trailing
newline); its own SHA-256 goes back to the caller and is never written into the log.

`check` is pure reading: no lock is taken and nothing is written. Every read opens each
path component without following a symlink, as `safeio` does.
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
from pathlib import Path
from typing import Any, Final

from stemma.register import safeio
from stemma.register.wm.errors import LogCorruptError
from stemma.register.wm.log import decode_line

MANIFEST: Final = "release/manifest.json"
REGISTER_LOG: Final = "register/log.jsonl"
CHECKS_LOG: Final = "register/checks.jsonl"
# Invariant: the manifest's format name; a new name whenever a field's meaning or the
# exclusion list changes.
FORMAT: Final = "stemma-release-manifest/2"
# Resource limit: bytes read per call while hashing (memory, not a judgment).
_CHUNK: Final = 1 << 20
# Invariant: the names an earlier manifest is kept under (`seal` numbers them 1, 2, ...).
_KEPT: Final = re.compile(r"release/manifest\.([1-9][0-9]*)\.json")

# Exact run-relative paths never listed and never checked. Invariant for each: it holds
# no research content, or it is written by design after the release, so sealing it would
# fail every honest run.
EXCLUDED: Final[tuple[str, ...]] = (
    "release/manifest.json",  # the manifest itself: it cannot hold its own digest
    "events.jsonl",  # driver and hook events go on after release (the retrospective)
    "events.jsonl.lock",  # flock files: empty, made by any reader or writer at any time
    "events.jsonl.torn",  # where the next append moves a crashed writer's partial line
    "status.json",  # the driver's live status, rewritten while any session runs
    "status.html",  # the same, rendered for people
    "usage.jsonl",  # token accounting of sessions that end after release
    "usage.jsonl.lock",
    "usage.jsonl.torn",
    "usage.md",  # rendered again from usage.jsonl on every update
    "retro.md",  # the retrospective writer, launched by finish_run, writes it after release
    "tree.json.tmp",  # the tree's atomic-write temporary, renamed away at once
    "register/register.lock",  # the register's writers' lock
    "register/log.jsonl.lock",  # World Model's log lock
    "register/checks.jsonl.lock",
    "register/checks.jsonl.torn",  # checks.jsonl grows after release (see `checks`)
    "register/index.sqlite",  # derived index, rebuilt from the register log
    "register/index.sqlite.tmp",  # its rebuild's temporary and that temporary's journal
    "register/index.sqlite.tmp-journal",
    "store/index.sqlite",  # the evidence search index (WAL): SQLite rewrites these on
    "store/index.sqlite-wal",  # reads and checkpoints; derived from the sealed
    "store/index.sqlite-shm",  # store/text/ and store/receipts/
    "store/index.sqlite-journal",
)
# Whole folders excluded. Invariant: each is owned by the driver, per session, and no
# agent can write it (both are tool-only: the guards and the sandbox deny every agent
# write under them): `.stemma/` holds the driver's state, the run plugin, each session's
# settings and stream (the retrospective writer's after release) and its lock;
# `.stemma-tmp/` holds each session's harness temp folder (CLAUDE_CODE_TMPDIR).
EXCLUDED_FOLDERS: Final[tuple[str, ...]] = (".stemma", ".stemma-tmp")
# A folder whose sealed files must stay, unchanged, while new files may appear in it.
# Invariant: the guard writes one identity stamp per tool call (`<tool_use_id>.json`),
# the retrospective writer's after release, and never changes or removes one.
GROWING_FOLDERS: Final[tuple[str, ...]] = ("register/calls",)


class ManifestError(Exception):
    """The run folder cannot be sealed (a required file missing, an entry unreadable)."""


class UnsealableEntry(ManifestError):
    """A symlink, special file or stray release file lies in the run folder."""


class LogError(Exception):
    """An append-only record cannot be read as committed JSON-object lines."""


class LogCorrupt(LogError):
    """A newline-terminated line of the record is not a JSON object."""


class LogTorn(LogError):
    """The record's bytes end without a newline (a writer mid-append, or one that died).
    `records` holds the complete records before the tail."""

    def __init__(self, message: str, records: list[dict[str, Any]], tail: int) -> None:
        super().__init__(message)
        self.records = records
        self.tail_bytes = tail


# ------------------------------------------------------------------ the register log
def parse_log(data: bytes, where: str = REGISTER_LOG) -> list[dict[str, Any]]:
    """The records of an append-only record's bytes (the register log by default). A
    line that is not a JSON object raises LogCorrupt; bytes after the last newline raise
    LogTorn, never skipped silently."""
    committed = data.rfind(b"\n") + 1
    records: list[dict[str, Any]] = []
    offset = 0
    for line in data[:committed].splitlines(keepends=True):
        try:
            records.append(decode_line(line, f"{where} at byte {offset}"))
        except LogCorruptError as exc:
            raise LogCorrupt(str(exc)) from exc
        offset += len(line)
    if committed != len(data):
        tail = len(data) - committed
        raise LogTorn(f"{where} ends in {tail} bytes with no newline (a writer mid-append, "
                      f"or one that died); the {len(records)} complete records before them "
                      f"are intact", records, tail)
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
def excluded(rel: str) -> bool:
    """Whether a path relative to the run folder is excluded from the seal."""
    return rel in EXCLUDED or any(rel == d or rel.startswith(d + "/")
                                  for d in EXCLUDED_FOLDERS)


def _growing(rel: str) -> bool:
    return any(rel.startswith(d + "/") for d in GROWING_FOLDERS)


def _stray_release(rel: str) -> bool:
    """A path under release/ other than the manifest or an earlier manifest kept by name."""
    return (rel == "release" or rel.startswith("release/")) and rel != MANIFEST \
        and not _KEPT.fullmatch(rel)


def _rules() -> list[str]:
    """The `excluded` field: every exact path, then each excluded folder as `name/`."""
    return [*EXCLUDED, *(f"{d}/" for d in EXCLUDED_FOLDERS)]


def _growing_rules() -> list[str]:
    return [f"{d}/" for d in GROWING_FOLDERS]


# -------------------------------------------------------------------------- walk
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_TYPES: Final = ((stat.S_ISREG, "file"), (stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"),
                 (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "character device"),
                 (stat.S_ISBLK, "block device"), (stat.S_ISDIR, "folder"))
_PREFIXED: Final = (REGISTER_LOG, CHECKS_LOG)


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
    """The bytes of an append-only record, or (size, SHA-256) of any other regular file;
    opened without following a link or blocking on a FIFO. OSError when the entry is no
    longer a regular file."""
    fd = os.open(e.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=e.dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{e.rel} is no longer a regular file")
        os.set_blocking(fd, True)
        if e.rel in _PREFIXED:
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
    beside the release: the manifest's path and own SHA-256, the file count, and the
    `log` and `checks` records. An earlier manifest is kept as
    `release/manifest.<n>.json` and sealed. Raises UnsealableEntry for a symlink, special
    file or stray file under release/, LogError for a torn or corrupt register log or
    check record, and ManifestError for anything else that stops the seal; nothing is
    moved or written then."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ManifestError(f"cannot seal {run_dir}: version {version!r} is not a positive "
                            f"whole number")
    run_id = run_id if run_id is not None else _run_id(run_dir)
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    prefixes: dict[str, dict[str, Any]] = {}
    try:
        for e in _walk(run_dir, errors):
            if e.kind != "file":
                raise UnsealableEntry(f"cannot seal {run_dir}: {e.rel} is {e.what()}; a "
                                      f"released run holds plain files only")
            if _stray_release(e.rel):
                raise UnsealableEntry(f"cannot seal {run_dir}: {e.rel} is in release/, "
                                      f"which holds only the manifests")
            size, sha, data = _digest(e)
            if e.rel != CHECKS_LOG:  # sealed as a prefix only: it grows after release
                files.append({"path": e.rel, "size": size, "sha256": sha})
            if data is not None:
                prefixes[e.rel] = {"path": e.rel, "bytes": size, "sha256": sha,
                                   "records": len(parse_log(data, e.rel))}
        previous = _previous(run_dir)
    except OSError as exc:
        raise ManifestError(f"cannot seal {run_dir}: {exc}") from exc
    if errors:
        raise ManifestError(f"cannot seal {run_dir}: " + "; ".join(errors))
    for rel in _PREFIXED:
        if rel not in prefixes:
            raise ManifestError(f"cannot seal {run_dir}: {rel} is missing")
    if previous is not None:
        files.append(previous[1])
    files.sort(key=lambda f: str(f["path"]))
    log, checks = prefixes[REGISTER_LOG], prefixes[CHECKS_LOG]
    manifest = {
        "format": FORMAT,
        "run_id": run_id,
        "version": version,
        "released_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"),
        "excluded": _rules(),
        "growing": _growing_rules(),
        "log": {"path": REGISTER_LOG, "bytes": log["bytes"], "sha256": log["sha256"],
                "transactions": log["records"]},
        "checks": checks,
        "files": files,
    }
    data = canonical(manifest)
    try:
        if previous is not None:
            safeio.move(run_dir, MANIFEST, previous[0])
        safeio.write_bytes(run_dir, MANIFEST, data)
    except (OSError, safeio.UnsafePath) as exc:
        raise ManifestError(f"cannot write {MANIFEST}: {exc}") from exc
    return {"manifest": MANIFEST, "manifest_sha256": hashlib.sha256(data).hexdigest(),
            "files": len(files), "log": manifest["log"], "checks": checks}


def _run_id(run_dir: Path) -> str:
    try:
        run_id = json.loads(safeio.read_bytes(run_dir, "run.json")).get("run_id")
    except (OSError, ValueError, AttributeError, safeio.UnsafePath) as exc:
        raise ManifestError(f"cannot seal {run_dir}: run.json is unreadable ({exc}); pass "
                            f"run_id") from exc
    if not isinstance(run_id, str) or not run_id:
        raise ManifestError(f"cannot seal {run_dir}: run.json has no run_id; pass run_id")
    return run_id


def _previous(run_dir: Path) -> tuple[str, dict[str, Any]] | None:
    """Where an existing manifest will be kept, and its entry; None when there is none."""
    try:
        old = safeio.read_bytes(run_dir, MANIFEST)
    except FileNotFoundError:
        return None
    except (safeio.UnsafePath, NotADirectoryError) as exc:
        raise OSError(f"{MANIFEST} is not a plain file: {exc}") from exc
    n = 1
    while safeio.is_file(run_dir, f"release/manifest.{n}.json"):
        n += 1
    keep = f"release/manifest.{n}.json"
    return keep, {"path": keep, "size": len(old), "sha256": hashlib.sha256(old).hexdigest()}


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
        log, checks = manifest["log"], manifest["checks"]
        prefixes = {REGISTER_LOG: (int(log["bytes"]), str(log["sha256"]),
                                   int(log["transactions"])),
                    CHECKS_LOG: (int(checks["bytes"]), str(checks["sha256"]),
                                 int(checks["records"]))}
    except (ValueError, KeyError, TypeError) as exc:
        return [*out, f"{MANIFEST} is not a readable manifest ({type(exc).__name__}: {exc})"]
    if manifest.get("format") != FORMAT or manifest.get("excluded") != _rules() \
            or manifest.get("growing") != _growing_rules():
        out.append(f"{MANIFEST} was sealed under other rules ({manifest.get('format')}) than "
                   f"this build checks ({FORMAT})")
    errors: list[str] = []
    seen: set[str] = set()
    try:
        for e in _walk(run_dir, errors):
            seen.add(e.rel)
            try:
                line = (_compare_prefix(e, prefixes[e.rel]) if e.rel in prefixes
                        else _compare(e, sealed.get(e.rel)))
            except OSError as exc:  # gone or replaced while being read
                line = f"unreadable: {e.rel} ({exc})"
            if line:
                out.append(line)
    except OSError as exc:
        return [*out, f"cannot read the run folder {run_dir}: {exc}"]
    out.extend(errors)
    for rel in sorted(set(sealed) | set(prefixes)):
        if rel not in seen and not excluded(rel):
            out.append(f"{_who(rel)}{rel} is missing (sealed at {prefixes[rel][0]} bytes)"
                       if rel in prefixes else
                       f"removed: {rel} (sealed with {sealed[rel].get('size')} bytes, "
                       f"missing now)")
    return out


def after_seal(run_dir: Path) -> list[str]:
    """What `register/checks.jsonl` gained after the seal, one plain line per record
    (the retrospective writer's request, launch and end, a later revocation): reported,
    never a difference. Empty when nothing was added, or the run is not sealed or its
    check records are unreadable (then `check` names the problem). Pure reading."""
    try:
        sealed = json.loads(safeio.read_bytes(run_dir, MANIFEST))["checks"]
        want = int(sealed["bytes"])
        data = safeio.read_bytes(run_dir, CHECKS_LOG)
        later = parse_log(data[want:], CHECKS_LOG) if len(data) > want else []
    except (OSError, ValueError, KeyError, TypeError, safeio.UnsafePath, LogError):
        return []
    return [f"{CHECKS_LOG}: after the seal: {json.dumps(r, sort_keys=True)}" for r in later]


def _who(rel: str) -> str:
    return "register log: " if rel == REGISTER_LOG else "check records: "


def _compare(e: _Entry, f: dict[str, Any] | None) -> str | None:
    if e.kind != "file":
        return f"not a plain file: {e.rel} is {e.what()}" + (
            "" if f is None else ", sealed as a plain file")
    if f is None:
        if _stray_release(e.rel):
            return f"added: {e.rel} (release/ holds only the manifests)"
        if _growing(e.rel) and "/" not in e.rel.split("/", 2)[-1]:
            return None  # a new identity stamp, from a tool call after the seal
        return f"added: {e.rel} (not in the seal)"
    size, sha, _data = _digest(e)
    if size == f.get("size") and sha == f.get("sha256"):
        return None
    return f"changed: {e.rel} ({f.get('size')} bytes sealed, {size} now; contents differ)"


def _compare_prefix(e: _Entry, sealed: tuple[int, str, int]) -> str | None:
    """The register log must be exactly as sealed; the check records may only have grown
    past their sealed bytes, by whole records (`after_seal` names them)."""
    rel = e.rel
    if e.kind != "file":
        return f"not a plain file: {rel} is {e.what()}, sealed as a plain file"
    _size, _sha, raw = _digest(e)
    data = raw if raw is not None else b""
    want, sha, records = sealed
    who = _who(rel)
    if len(data) == want and hashlib.sha256(data).hexdigest() == sha:
        return None
    if len(data) < want:
        return (f"{who}{rel} is shorter than when sealed ({len(data)} bytes now, {want} "
                f"sealed)")
    if hashlib.sha256(data[:want]).hexdigest() != sha:
        return f"{who}the {want} bytes of {rel} sealed at release changed"
    try:
        n = len(parse_log(data, rel)) - records
    except LogError as exc:
        return f"{who}{rel} has bytes after the seal that are not whole records ({exc})"
    if rel == CHECKS_LOG:
        return None  # check records go on after release: the retrospective, revocations
    return (f"{who}{len(data) - want} bytes appended to {rel} after the seal ({n} "
            f"transactions), outside the sealed log")
