"""The claim register, read without writing: from the log's bytes, in memory.

`read_log` reads `register/log.jsonl` through `safefs` (confined, bounded, read-only)
and refuses an unterminated tail: a log must end with a newline, because the register's
own reader silently leaves trailing bytes unread. `snapshot` folds the transactions with
World Model's own fold, in memory, and returns a `Register` over that world. The
register's read methods then give the same claim records, verdicts and gate the run saw,
and nothing opens the run's log for writing or takes its lock.

**Only sealed bytes decide anything.** When the run carries a release manifest, the
snapshot reads the log's sealed prefix only (its length and SHA-256 from the manifest,
which must match): a transaction appended after the seal is never folded, so it can set
no verdict, release or status. `register/checks.jsonl` (revocations, verifications,
release blocks) is read the same way when the manifest seals its prefix ("checks"); a
manifest without that field leaves the file unauthenticated, and the snapshot says so.
Both side files the register reads (`checks.jsonl`, `refused_corrections.jsonl`) go
through `safefs` and a strict parser here, never the register's unbounded reader.

STOP-GAP: the snapshot starts from W2's public `Register.read_only(run_dir)`, which
refuses every write, but `read_only` folds the whole log file and reads checks.jsonl and
refused_corrections.jsonl with the register's unbounded reader, while a release must be
read from its sealed bytes only. So `_memory_world` folds the sealed bytes in memory
(through `WorldModel._log`) and `_read_only_register` puts that world and three side-file
reads on the instance. Replace both once `read_only` can take the sealed bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from stemma.register.manifest import LogError, parse_log
from stemma.report import safefs
from stemma.report.errors import RunReadError

LOG: Final = "register/log.jsonl"
CHECKS: Final = "register/checks.jsonl"
REFUSED: Final = "register/refused_corrections.jsonl"
MANIFEST: Final = "release/manifest.json"
# Resource limits: bytes of the register's log, of each side file, and of the manifest.
LOG_MAX_BYTES: Final = 512 * 1024 * 1024
SIDE_MAX_BYTES: Final = 256 * 1024 * 1024
MANIFEST_MAX_BYTES: Final = 64 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Log:
    data: bytes
    lines: list[bytes]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _check_terminated(data: bytes, rel: str) -> None:
    if data and not data.endswith(b"\n"):
        tail = len(data) - (data.rfind(b"\n") + 1)
        raise RunReadError(f"{rel} ends with {tail:,} bytes after its last newline (an "
                           f"unterminated tail)")


def read_log(run_dir: Path) -> Log:
    data = safefs.read_bytes(run_dir, LOG, LOG_MAX_BYTES)
    try:  # the register's own parser: a torn or corrupt line refused
        parse_log(data)
    except LogError as exc:
        raise RunReadError(str(exc)) from exc
    _check_terminated(data, LOG)
    return Log(data, data.splitlines(keepends=True))


def read_manifest(run_dir: Path) -> tuple[dict[str, Any], str] | None:
    """The release manifest and its own SHA-256, or None when the run is unsealed."""
    raw = safefs.maybe(safefs.read_bytes, run_dir, MANIFEST, MANIFEST_MAX_BYTES)
    if raw is None:
        return None
    try:
        man = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise RunReadError(f"{MANIFEST} is not JSON") from exc
    if not isinstance(man, dict):
        raise RunReadError(f"{MANIFEST} is not a manifest")
    return man, hashlib.sha256(raw).hexdigest()


def sealed_prefix(man: dict[str, Any], key: str) -> tuple[int, str] | None:
    """(length, SHA-256) the manifest seals for a file's prefix ("log" or "checks")."""
    rec = man.get(key)
    if rec is None:
        return None
    size, sha = (rec.get("bytes"), rec.get("sha256")) if isinstance(rec, dict) else (None, None)
    if (not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not isinstance(sha, str) or not _SHA.fullmatch(sha)):
        raise RunReadError(f"{MANIFEST}: {key} is not {{path, bytes, sha256}}")
    return size, sha


def cut_to_seal(data: bytes, seal: tuple[int, str], rel: str) -> bytes:
    """The sealed prefix of `data`; a prefix that differs from the seal is refused."""
    size, sha = seal
    if len(data) < size or hashlib.sha256(data[:size]).hexdigest() != sha:
        raise RunReadError(f"{rel} does not match its seal: the first {size:,} bytes changed "
                           f"or are missing")
    prefix = data[:size]
    _check_terminated(prefix, rel)
    return prefix


def _records(data: bytes, rel: str, kinded: bool = True) -> list[dict[str, Any]]:
    """Every complete line as a JSON object (a line that is not one is refused); a torn
    tail is left unread, as the register's own reader leaves it."""
    out: list[dict[str, Any]] = []
    end = data.rfind(b"\n") + 1
    for n, raw in enumerate(data[:end].splitlines(), start=1):
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise RunReadError(f"{rel}: line {n} is not JSON") from exc
        if not isinstance(rec, dict) or (kinded and not isinstance(rec.get("kind"), str)):
            raise RunReadError(f"{rel}: line {n} is not a record"
                               + (" with a kind" if kinded else ""))
        out.append(rec)
    return out


@dataclass(frozen=True)
class Checks:
    """What `register/checks.jsonl` says about sessions and the release."""

    records: list[dict[str, Any]]
    authenticated: bool  # True when its bytes are a sealed prefix

    def _sessions(self, kind: str) -> set[str]:
        out = set()
        for r in self.records:
            if r["kind"] == kind:
                if not isinstance(r.get("session_id"), str):
                    raise RunReadError(f"{CHECKS}: a {kind} record names no session")
                out.add(r["session_id"])
        return out

    @property
    def revoked(self) -> frozenset[str]:
        return frozenset(self._sessions("revoked"))

    @property
    def verified(self) -> frozenset[str]:
        return frozenset(self._sessions("verified") - self._sessions("revoked"))

    @property
    def blocks(self) -> list[str]:
        out = []
        for r in self.records:
            if r["kind"] == "release_blocked":
                if not isinstance(r.get("reason"), str):
                    raise RunReadError(f"{CHECKS}: a release block gives no reason")
                out.append(r["reason"])
        return out


def _memory_world(lines: list[bytes]) -> Any:
    """STOP-GAP (see the module's note): World Model's fold over these lines, in memory."""
    from stemma.register.wm import WorldModel, WorldModelError
    from stemma.register.wm.model import MEMORY_AS_FILE_ENV

    if os.environ.get(MEMORY_AS_FILE_ENV):
        raise RunReadError(f"{MEMORY_AS_FILE_ENV} is set; it would turn the in-memory "
                           f"reading into a file")
    wm = WorldModel({"storage": "memory", "namespace": "register"})
    wm._log._lines = list(lines)  # type: ignore[attr-defined]
    try:
        wm.refresh()
    except (WorldModelError, RecursionError) as exc:
        raise RunReadError(f"{LOG} does not fold: {exc}") from exc
    return wm


def _read_only_register(run_dir: Path, wm: Any, checks: Checks,
                        refused: list[dict[str, Any]]) -> Any:
    """W2's `Register.read_only` (no lock, no write; every write method refused), with
    its world and its three side-file reads replaced by what was read here from the
    sealed bytes (STOP-GAP, see the module's note). The log was checked to be a plain,
    bounded file through safefs before `read_only` opens it."""
    from stemma.register.store import Register

    reg = Register.read_only(run_dir)
    reg.wm = wm
    revoked, verified = checks.revoked, checks.verified

    def refused_by(cid: str) -> frozenset[str]:
        return frozenset(str(r.get("session_id")) for r in refused if r.get("claim_id") == cid)

    reg._revoked = lambda: revoked  # type: ignore[method-assign]
    reg._verified = lambda: verified  # type: ignore[method-assign]
    reg._sessions_with_refused_correction = refused_by  # type: ignore[method-assign]
    return reg


@dataclass
class Snapshot:
    log: Log
    world: Any
    register: Any
    checks: Checks
    manifest: dict[str, Any] | None = None
    appended_log_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def transactions(self) -> int:
        return len(self.log.lines)

    @contextmanager
    def gate_reads(self) -> Iterator[None]:
        """While the register's gate is read: its one direct read of checks.jsonl
        (release blocks) answered from the records read here."""
        from stemma.register import checks_api

        saved = checks_api.release_blocks
        blocks = self.checks.blocks
        checks_api.release_blocks = lambda _run_dir: list(blocks)  # type: ignore[assignment]
        try:
            yield
        finally:
            checks_api.release_blocks = saved


def snapshot(run_dir: Path) -> Snapshot:
    log = read_log(run_dir)
    sealed = read_manifest(run_dir)
    man = sealed[0] if sealed else None
    appended = 0
    notes: list[str] = []
    if man is not None:
        seal = sealed_prefix(man, "log")
        if seal is None:
            raise RunReadError(f"{MANIFEST} does not seal the register's log")
        prefix = cut_to_seal(log.data, seal, LOG)
        appended = len(log.data) - len(prefix)
        log = Log(prefix, prefix.splitlines(keepends=True))  # nothing after the seal
    raw_checks = safefs.maybe(safefs.read_bytes, run_dir, CHECKS, SIDE_MAX_BYTES) or b""
    checks_seal = sealed_prefix(man, "checks") if man is not None else None
    if checks_seal is not None:
        checks = Checks(_records(cut_to_seal(raw_checks, checks_seal, CHECKS), CHECKS), True)
    else:
        checks = Checks(_records(raw_checks, CHECKS), False)
        if man is not None:
            notes.append(f"{CHECKS} is not sealed by this manifest, so the revocations, "
                         f"verifications and release blocks in it are not authenticated")
    refused = _records(safefs.maybe(safefs.read_bytes, run_dir, REFUSED, SIDE_MAX_BYTES)
                       or b"", REFUSED, kinded=False)
    wm = _memory_world(log.lines)
    return Snapshot(log, wm, _read_only_register(run_dir, wm, checks, refused), checks, man,
                    appended, notes)
