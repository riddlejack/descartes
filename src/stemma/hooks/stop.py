"""The lead's Stop hook: a lead does not end its session while its run is unreleased.

Run 3 (27 Oct) ended because the lead, with no source work of its own (D6), dispatched
three background workers and ended its turn to wait; in headless mode that ends the
session, and the harness killed the two workers still running. This hook answers the
harness's Stop event for the lead's own session only (matched by the session id the
driver recorded): until `finish_run` is accepted, the run is frame-only, or the lead's
time limit has passed, it refuses to let the lead stop. Before refusing, it waits
inside the hook (bounded) for the work in flight to move, so a waiting lead costs a
turn per change rather than a turn per second, and its refusal names what is in flight
and how to wait for it. Any other session's Stop, and any failure here, lets the
session stop: the driver resumes a lead that ended anyway.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from stemma.register import checks_api, jsonl

STATE: Final = ".stemma/driver-state.json"
HOOK_STATE: Final = ".stemma/stop-hook.json"
# Resource limit: seconds the hook waits for the work in flight to move before it
# refuses the stop (below the hook's own timeout in hooks.json, 300 s).
WAIT_S: Final = 240.0
# Resource limit: seconds between looks while waiting.
POLL_S: Final = 5.0
# Resource limit: refusals in a row with nothing in the run changing, after which the
# lead may stop and the driver's resume takes over (about two hours of waiting).
MAX_IDLE_REFUSALS: Final = 30


@dataclass(frozen=True)
class StopDecision:
    block: bool
    reason: str
    detail: dict[str, Any]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _usage_stopped(run_dir: Path) -> bool:
    state = _read_json(run_dir / STATE)
    return isinstance(state, dict) and bool(state.get("usage_limited"))


def _lead_started(run_dir: Path) -> float | None:
    for e in jsonl.read_all(run_dir / "events.jsonl"):
        if e.get("event") == "session_launched" and e.get("role") == "lead":
            at = str(e.get("at", ""))
            try:
                return datetime.fromisoformat(at).timestamp()
            except ValueError:
                return None
    return None


def _has(run_dir: Path, branch: str, name: str) -> bool:
    root = run_dir / "branches"
    return any(p.is_file() for p in (*root.glob(f"{branch}/{name}"),
                                     *root.glob(f"**/sub/{branch}/{name}")))


def in_flight(run_dir: Path) -> dict[str, list[str]]:
    """What the run is waiting on, from exact facts on disk: branches ordered but with
    no record yet, branches with a record still in progress, branches proposed and
    awaiting their verdict, and checks the driver has not finished."""
    from stemma.register.store import Register

    tree = Register.read_only(run_dir).tree()
    open_branches = sorted({str(n["branch_id"]) for n in tree if n.get("branch_id")
                            and n.get("status") in ("open", "in_progress")
                            and _has(run_dir, str(n["branch_id"]), "order.md")})
    no_record = [b for b in open_branches if not _has(run_dir, b, "record.md")]
    working = [b for b in open_branches if b not in no_record]
    awaiting = sorted({str(n["branch_id"]) for n in tree if n.get("branch_id")
                       and n.get("status") == "proposed_done"})
    checks = sorted(f"{s.request.role} {s.request.prompt}"
                    for s in checks_api.states(run_dir).values() if s.status != "ended")
    return {"branches_without_record": no_record, "branches_in_progress": working,
            "branches_awaiting_verdict": awaiting, "checks_running_or_pending": checks}


def progress_signature(run_dir: Path) -> tuple[Any, ...]:
    """What changes when work moves: every branch file, the register and checks logs."""
    sig: list[Any] = []
    for rel in ("register/log.jsonl", "register/checks.jsonl"):
        try:
            sig.append([rel, os.stat(run_dir / rel).st_size])
        except OSError:
            sig.append([rel, None])
    root = run_dir / "branches"
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                sig.append([str(p.relative_to(run_dir)), st.st_size, st.st_mtime_ns])
    return tuple(tuple(x) for x in sig)


def reason_text(flight: dict[str, list[str]]) -> str:
    lines = [("Stemma: the run is not released, so the lead does not end its session "
              "(finish_run releases it). Workers you started run in the background and "
              "report back to you when they finish; ending your turn would end them.")]
    labels = {"branches_without_record": "Branches ordered, no record yet",
              "branches_in_progress": ("Branches with a record not yet proposed "
                                       "(propose_branch_done once its worker has finished)"),
              "branches_awaiting_verdict": "Branches awaiting their reviewer's verdict",
              "checks_running_or_pending": "Checks the host is running or will start"}
    busy = False
    for key, label in labels.items():
        if flight[key]:
            busy = True
            lines.append(f"{label}: {', '.join(flight[key])}.")
    waitable = [f"branch:{b}" for b in flight["branches_awaiting_verdict"]]
    if busy:
        wait = (f"await_checks({json.dumps(waitable)}, max_wait_s=300) for the verdicts; "
                if waitable else "")
        lines.append(f"Wait for them: {wait}a worker's report reaches you when it "
                     "finishes. Re-dispatch a worker for a branch with no record and no "
                     "worker running. Then carry on: the draft, its reviews, finish_run.")
    else:
        lines.append("Nothing is in flight: carry on (dispatch, draft, submit_draft, "
                     "gate_status, finish_run).")
    return "\n".join(lines)


def decide(hook: dict[str, Any], run_dir: Path, *, clock: Callable[[], float] = time.time,
           sleep: Callable[[float], None] = time.sleep, wait_s: float = WAIT_S) -> StopDecision:
    """Whether the session may stop, and if not, why (the text the lead is given)."""
    session = str(hook.get("session_id") or "")
    state = _read_json(run_dir / STATE)
    lead = (state.get("lead") or {}) if isinstance(state, dict) else {}
    if not session or session != lead.get("session_id"):
        return StopDecision(False, "", {"why": "not the lead's session"})
    if _usage_stopped(run_dir):
        # The supervisor stops launching checks at a usage stop and recovers the run
        # after the reset: waiting here would only burn the lead's time (review #3).
        return StopDecision(False, "", {"why": "a usage stop: the supervisor recovers"})
    run = _read_json(run_dir / "run.json") or {}
    if run.get("stop_after") == "frame":
        return StopDecision(False, "", {"why": "frame-only run"})
    from stemma.register.store import Register

    if Register.read_only(run_dir).release_state() == "released":
        return StopDecision(False, "", {"why": "released"})
    from stemma.driver.leadtime import lead_time_left

    limit = float((run.get("limits") or {}).get("lead_max_s", 24 * 3600.0))
    # The lead's cumulative execution time, as the driver counts it: pauses between
    # sessions (usage waits) are not work (review #2).
    t0 = clock()
    left = lead_time_left(run_dir, limit, now=t0)
    limit_at = t0 + left  # from the account once: scanning time is not added back (#6)
    if left <= 0:
        return StopDecision(False, "", {"why": "the lead's time limit has passed"})
    previous = _read_json(run_dir / HOOK_STATE) or {}
    launch = int(lead.get("launches", 0))
    if int(previous.get("launch", launch)) != launch:
        previous = {}  # a new driver-authorised launch starts its idle count afresh (#7)
    before = progress_signature(run_dir)
    flight = in_flight(run_dir)
    if any(flight.values()):
        deadline = min(t0 + wait_s, limit_at)
        while clock() < deadline and progress_signature(run_dir) == before:
            if _usage_stopped(run_dir):
                return StopDecision(False, "", {"why": "a usage stop: the supervisor recovers"})
            sleep(max(0.0, min(POLL_S, deadline - clock())))  # never past the deadline (#6)
        flight = in_flight(run_dir)
    if _usage_stopped(run_dir):  # a stop that arrived during the last sleep (#3)
        return StopDecision(False, "", {"why": "a usage stop: the supervisor recovers"})
    if clock() >= limit_at:
        return StopDecision(False, "", {"why": "the lead's time limit has passed"})
    after = progress_signature(run_dir)
    last = tuple(tuple(x) for x in previous.get("signature") or ())
    idle = int(previous.get("idle", 0)) + 1 if after == last else 0
    if idle >= MAX_IDLE_REFUSALS:
        return StopDecision(False, "", {"why": f"{idle} refusals with nothing changing"})
    from stemma.driver import hostio

    hostio.write_json(run_dir, HOOK_STATE, {"signature": [list(x) for x in after],
                                            "idle": idle, "launch": launch,
                                            "refusals": int(previous.get("refusals", 0)) + 1})
    return StopDecision(True, reason_text(flight), {"in_flight": flight, "idle": idle})
