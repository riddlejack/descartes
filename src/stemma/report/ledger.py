"""The claim register, read without writing, from exactly the bytes that count.

`read_log` reads `register/log.jsonl` through `safefs` (confined, bounded, read-only)
and refuses a torn or corrupt line with the register's own `parse_log`. `snapshot`
opens the register with W2's `Register.from_sealed`, which folds exactly the log bytes
given, takes requests, verifications, revocations and release blocks from exactly the
check-record bytes given, takes refused corrections from the bytes given, and refuses
every write.

**Only sealed bytes decide anything.** When the run carries a release manifest, the
bytes given are the sealed prefixes of the log and of `register/checks.jsonl` (each
length and SHA-256 from the manifest, which must match): a record appended after the
seal is never read, so it can set no verdict, release or status. A run without a
manifest is read whole; verify calls it UNSEALED and never a pass.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from stemma.register import checks_api
from stemma.register.manifest import LogError, parse_log
from stemma.register.store import Register
from stemma.register.wm import WorldModel, WorldModelError
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


def fold(data: bytes) -> Any:
    """World Model's fold of exactly these log bytes (read-only "bytes" storage)."""
    try:
        return WorldModel({"storage": "bytes", "data": data, "namespace": "register"})
    except (WorldModelError, RecursionError) as exc:
        raise RunReadError(f"{LOG} does not fold: {exc}") from exc


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


@dataclass(frozen=True)
class Checks:
    """What the check records that count say about sessions and the release, read by
    the register's own functions over exactly those records."""

    run_dir: Path
    records: list[dict[str, Any]]
    authenticated: bool  # True when the records are a sealed prefix

    @property
    def revoked(self) -> frozenset[str]:
        return checks_api.revoked_sessions(self.run_dir, records=self.records)

    @property
    def blocks(self) -> list[str]:
        return checks_api.release_blocks(self.run_dir, records=self.records)


def _parse(data: bytes, rel: str, need_kind: bool = True) -> list[dict[str, Any]]:
    try:
        return checks_api.parse_records(data, need_kind=need_kind)
    except (ValueError, RecursionError) as exc:
        raise RunReadError(f"{rel}: {exc}") from exc


def _whole_records(data: bytes) -> bytes:
    """An unsealed side file up to its last newline (a writer may be mid-append)."""
    return data[:data.rfind(b"\n") + 1]


@dataclass
class Snapshot:
    log: Log
    register: Any
    checks: Checks
    manifest: dict[str, Any] | None = None
    appended_log_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def transactions(self) -> int:
        return len(self.log.lines)


def snapshot(run_dir: Path) -> Snapshot:
    log = read_log(run_dir)
    sealed = read_manifest(run_dir)
    man = sealed[0] if sealed else None
    appended = 0
    notes: list[str] = []
    raw_checks = safefs.maybe(safefs.read_bytes, run_dir, CHECKS, SIDE_MAX_BYTES) or b""
    raw_refused = safefs.maybe(safefs.read_bytes, run_dir, REFUSED, SIDE_MAX_BYTES) or b""
    if man is not None:
        seal = sealed_prefix(man, "log")
        if seal is None:
            raise RunReadError(f"{MANIFEST} does not seal the register's log")
        prefix = cut_to_seal(log.data, seal, LOG)
        appended = len(log.data) - len(prefix)
        log = Log(prefix, prefix.splitlines(keepends=True))  # nothing after the seal
        checks_seal = sealed_prefix(man, "checks")
        if checks_seal is None:
            raise RunReadError(f"{MANIFEST} does not seal {CHECKS}")
        checks_bytes = cut_to_seal(raw_checks, checks_seal, CHECKS)
        refused_bytes = raw_refused  # a sealed file: the seal check covers it whole
        authenticated = True
    else:
        checks_bytes = _whole_records(raw_checks)
        refused_bytes = _whole_records(raw_refused)
        authenticated = False
    checks = Checks(run_dir, _parse(checks_bytes, CHECKS), authenticated)
    _parse(refused_bytes, REFUSED, need_kind=False)  # refused here by name, if malformed
    try:
        reg = Register.from_sealed(run_dir, log=log.data, checks=checks_bytes,
                                   refused=refused_bytes)
    except (WorldModelError, ValueError, RecursionError) as exc:
        raise RunReadError(f"the register cannot be read from its bytes: {exc}") from exc
    return Snapshot(log, reg, checks, man, appended, notes)
