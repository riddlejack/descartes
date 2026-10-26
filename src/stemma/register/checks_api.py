"""Check requests: the register says which checking session is due, the driver starts it.

Stored in `register/checks.jsonl` (tool-only, append-only, locked). A request is
idempotent per (role, prompt): asking twice returns the first request. The driver marks
each request launched (with the session it started and the model and effort it
resolved) and ended (with the session's exit code and outcome).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.register import jsonl, roles


class UnknownCheck(Exception):
    """A launch or end was recorded for a request that does not exist."""


@dataclass(frozen=True)
class CheckRequest:
    request_id: str
    role: str  # short role name, e.g. "claim-checker"
    prompt: str  # id-only, e.g. "claim:<claim_id>"
    event: str  # the register event that called for it
    created_at: str
    round: int = 1  # the same check asked again (a branch re-proposed) is a new round


@dataclass(frozen=True)
class CheckState:
    request: CheckRequest
    session_id: str | None
    model: str | None
    effort: str | None
    launched_at: str | None
    exit_code: int | None
    outcome: str | None
    ended_at: str | None

    @property
    def status(self) -> str:
        if self.ended_at is not None:
            return "ended"
        if self.launched_at is not None:
            return "running"
        return "pending"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_path(run_dir: Path) -> Path:
    return run_dir / "register" / "checks.jsonl"


def request_id_for(role: str, prompt: str, round_no: int = 1) -> str:
    return hashlib.sha256(f"{role}\n{prompt}\n{round_no}".encode()).hexdigest()[:16]


def _states(records: list[dict[str, Any]]) -> dict[str, CheckState]:
    out: dict[str, CheckState] = {}
    for r in records:
        if r["kind"] in ("revoked", "verified", "release_blocked"):
            continue
        rid = r["request_id"]
        if r["kind"] == "request":
            if rid not in out:
                req = CheckRequest(rid, r["role"], r["prompt"], r["event"], r["at"],
                                   r.get("round", 1))
                out[rid] = CheckState(req, None, None, None, None, None, None, None)
        elif r["kind"] == "launched":
            s = out[rid]
            out[rid] = CheckState(s.request, r["session_id"], r["model"], r["effort"], r["at"],
                                  None, None, None)
        elif r["kind"] == "ended":
            s = out[rid]
            out[rid] = CheckState(s.request, s.session_id, s.model, s.effort, s.launched_at,
                                  r["exit_code"], r["outcome"], r["at"])
    return out


def states(run_dir: Path) -> dict[str, CheckState]:
    return _states(jsonl.read_all(log_path(run_dir)))


def request_check(run_dir: Path, role: str, prompt: str, event: str,
                  round_no: int = 1) -> CheckRequest:
    """Ask for a check; idempotent per (role, prompt, round)."""
    role = roles.short(role)
    if f"{roles.PLUGIN}:{role}" not in roles.CHECKING_ROLES:
        raise ValueError(f"{role} is not a checking role")
    rid = request_id_for(role, prompt, round_no)
    log = log_path(run_dir)
    with jsonl.exclusive(log):
        existing = _states(jsonl.read_all(log)).get(rid)
        if existing is not None:
            return existing.request
        at = _now()
        jsonl.append_unlocked(log, [{"kind": "request", "request_id": rid, "role": role,
                                     "prompt": prompt, "event": event, "at": at,
                                     "round": round_no}])
    return CheckRequest(rid, role, prompt, event, at, round_no)


def pending_checks(run_dir: Path) -> list[CheckRequest]:
    return [s.request for s in states(run_dir).values() if s.status == "pending"]


def _mark(run_dir: Path, record: dict[str, Any], need: frozenset[str]) -> None:
    log = log_path(run_dir)
    with jsonl.exclusive(log):
        s = _states(jsonl.read_all(log)).get(record["request_id"])
        if s is None:
            raise UnknownCheck(record["request_id"])
        if s.status not in need:
            raise UnknownCheck(f"{record['request_id']} is {s.status}, not {sorted(need)}")
        jsonl.append_unlocked(log, [record])


def mark_launched(run_dir: Path, request_id: str, session_id: str, model: str,
                  effort: str, relaunch: bool = False) -> None:
    """Record a launch; `relaunch` allows it for a check whose session never ended."""
    need = frozenset({"pending", "running"}) if relaunch else frozenset({"pending"})
    _mark(run_dir, {"kind": "launched", "request_id": request_id, "session_id": session_id,
                    "model": model, "effort": effort, "at": _now()}, need)


def mark_ended(run_dir: Path, request_id: str, exit_code: int, outcome: str) -> None:
    _mark(run_dir, {"kind": "ended", "request_id": request_id, "exit_code": exit_code,
                    "outcome": outcome, "at": _now()}, frozenset({"running"}))


def for_prompt(run_dir: Path, role: str, prompt: str) -> list[CheckState]:
    """Every round of one check, oldest first."""
    role = roles.short(role)
    found = [s for s in states(run_dir).values()
             if s.request.role == role and s.request.prompt == prompt]
    return sorted(found, key=lambda s: s.request.round)


def revoke(run_dir: Path, session_id: str, reason: str) -> None:
    """The driver found this session ran on a model or effort other than run.json's:
    it loses verdict authority, now and for what it already recorded."""
    if not session_id or not reason.strip():
        raise ValueError("a revocation names the session and the reason")
    jsonl.append(log_path(run_dir), {"kind": "revoked", "request_id": "", "at": _now(),
                                     "session_id": session_id, "reason": reason})


def mark_verified(run_dir: Path, session_id: str, model: str, effort: str) -> None:
    """The driver checked this ended session's resolved model and effort against
    run.json and they matched: its verdicts, frames and reviews may count."""
    jsonl.append(log_path(run_dir), {"kind": "verified", "request_id": "", "at": _now(),
                                     "session_id": session_id, "model": model,
                                     "effort": effort})


def verified_sessions(run_dir: Path) -> frozenset[str]:
    records = jsonl.read_all(log_path(run_dir))
    revoked = {r["session_id"] for r in records if r["kind"] == "revoked"}
    return frozenset(r["session_id"] for r in records
                     if r["kind"] == "verified" and r["session_id"] not in revoked)


def block_release(run_dir: Path, reason: str) -> None:
    """The driver could not complete something the release depends on (a revocation):
    no release may happen, and one that happened is withdrawn."""
    jsonl.append(log_path(run_dir), {"kind": "release_blocked", "request_id": "",
                                     "at": _now(), "reason": reason})


def release_blocks(run_dir: Path) -> list[str]:
    return [r["reason"] for r in jsonl.read_all(log_path(run_dir))
            if r["kind"] == "release_blocked"]


def revoked_sessions(run_dir: Path) -> frozenset[str]:
    return frozenset(r["session_id"] for r in jsonl.read_all(log_path(run_dir))
                     if r["kind"] == "revoked")


def state_for_session(run_dir: Path, session_id: str) -> CheckState | None:
    """The check a live (not revoked) session was launched for."""
    if session_id in revoked_sessions(run_dir):
        return None
    for s in states(run_dir).values():
        if s.session_id == session_id:
            return s
    return None


def draft_version_for_session(run_dir: Path, session_id: str) -> str | None:
    """The draft version a final reviewer's session was started on, if any."""
    s = state_for_session(run_dir, session_id)
    if s is None or not s.request.prompt.startswith("draft:"):
        return None
    version = s.request.prompt.split(":", 1)[1]
    return version if version.isdigit() else None
