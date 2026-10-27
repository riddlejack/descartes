"""The lead's Stop hook (run 3's failure): the lead may not stop before its run is
released; other sessions always may; failures let the session stop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stemma.hooks import stop
from stemma.register import checks_api, jsonl
from stemma.register.store import Caller, Register

LEAD = Caller("stemma:lead", None, "seed")


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    (d / ".stemma").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"run_id": "r", "limits": {"lead_max_s": 3600}}))
    (d / ".stemma/driver-state.json").write_text(json.dumps(
        {"run_id": "r", "lead": {"session_id": "s-lead"}}))
    jsonl.append(d / "events.jsonl", {"event": "session_launched", "role": "lead",
                                      "at": "2099-01-01T00:00:00.000Z"})
    Register(d)  # the register folder and log
    return d


class Clock:
    def __init__(self) -> None:
        self.t = 4070908800.0  # 2099-01-01, the lead's launch

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def hook(session: str = "s-lead") -> dict[str, Any]:
    return {"hook_event_name": "Stop", "session_id": session, "stop_hook_active": False}


def test_other_sessions_always_stop(run_dir: Path) -> None:
    for s in ("s-checker", "", "s-worker"):
        assert not stop.decide(hook(s), run_dir).block


def test_the_lead_with_nothing_in_flight_is_told_to_carry_on(run_dir: Path) -> None:
    c = Clock()
    d = stop.decide(hook(), run_dir, clock=c, sleep=c.sleep)
    assert d.block and "Nothing is in flight" in d.reason
    assert c.t == 4070908800.0  # it did not wait


def _branch(run_dir: Path, bid: str, status: str = "in_progress") -> None:
    reg = Register(run_dir)
    reg.tree_upsert(LEAD, {"id": f"n-{bid}", "kind": "question", "statement": "q",
                           "branch_id": bid, "status": status})
    (run_dir / "branches" / bid).mkdir(parents=True, exist_ok=True)
    (run_dir / "branches" / bid / "order.md").write_text("order")


def test_the_lead_waits_for_work_in_flight_then_is_told_what_it_is(run_dir: Path) -> None:
    _branch(run_dir, "b1")
    checks_api.request_check(run_dir, "blind-framer", "frame:own", "run_start")
    c = Clock()
    d = stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=60)
    assert d.block and c.t - 4070908800.0 >= 60  # waited the whole bound, nothing moved
    assert "Branches ordered, no record yet: b1." in d.reason
    assert "blind-framer frame:own" in d.reason
    assert "ending your turn would end them" in d.reason


def test_the_wait_ends_as_soon_as_work_moves(run_dir: Path) -> None:
    _branch(run_dir, "b1")
    c = Clock()

    def sleep(s: float) -> None:
        c.sleep(s)
        (run_dir / "branches/b1/record.md").write_text("the worker's record")

    d = stop.decide(hook(), run_dir, clock=c, sleep=sleep, wait_s=200)
    assert d.block and c.t - 4070908800.0 <= stop.POLL_S
    assert "once its worker has finished): b1." in d.reason


def test_awaiting_verdicts_are_named_for_await_checks(run_dir: Path) -> None:
    _branch(run_dir, "b2", status="in_progress")
    reg = Register(run_dir)
    reg.wm.refresh()
    node = next(n for n in reg.tree() if n["id"] == "n-b2")
    reg._put_node({**node, "status": "proposed_done"},
                  {"kind": "t", "at": "2099-01-01T00:00:00.000Z", "by": "t"})
    c = Clock()
    d = stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=1)
    assert 'await_checks(["branch:b2"]' in d.reason


def test_the_lead_stops_at_its_time_limit_and_in_a_frame_only_run(run_dir: Path) -> None:
    c = Clock()
    c.t += 3600
    assert not stop.decide(hook(), run_dir, clock=c, sleep=c.sleep).block
    run = json.loads((run_dir / "run.json").read_text())
    (run_dir / "run.json").write_text(json.dumps({**run, "stop_after": "frame"}))
    assert not stop.decide(hook(), run_dir).block


def test_the_wait_never_runs_past_the_time_limit_and_then_lets_go(run_dir: Path) -> None:
    # Codex review #6: from 30 s before the limit, the hook sleeps to the limit exactly
    # (never a whole poll past it) and then lets the lead stop.
    _branch(run_dir, "b1")
    c = Clock()
    c.t += 3600 - 30
    d = stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=240)
    assert not d.block and d.detail["why"] == "the lead's time limit has passed"
    assert c.t == 4070908800.0 + 3600


def test_usage_waits_are_not_counted_as_lead_time(run_dir: Path) -> None:
    # Codex review #2: run 50 s, a 3000 s usage wait, then a resumed launch.
    from stemma.driver.leadtime import lead_time_used

    jsonl.append(run_dir / "events.jsonl",
                 {"event": "session_ended", "role": "lead", "session": "lead",
                  "at": "2099-01-01T00:00:50.000Z"},
                 {"event": "session_launched", "role": "lead", "session": "lead-r1",
                  "at": "2099-01-01T00:50:50.000Z"})
    first = json.loads((run_dir / "events.jsonl").read_text().splitlines()[0])
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    lines[0] = json.dumps({**first, "session": "lead"})
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")
    c = Clock()
    c.t += 3050 + 10  # 10 s into the resumed launch
    assert lead_time_used(run_dir, now=c.t) == pytest.approx(60.0)
    run = json.loads((run_dir / "run.json").read_text())
    (run_dir / "run.json").write_text(json.dumps({**run, "limits": {"lead_max_s": 100}}))
    _branch(run_dir, "b1")
    d = stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=5)
    assert d.block  # 40 s of lead time remain, though 3060 s have passed


def test_a_usage_stop_lets_the_lead_stop_for_the_supervisor(run_dir: Path) -> None:
    # Codex review #3, before and during the wait.
    _branch(run_dir, "b1")
    state = json.loads((run_dir / ".stemma/driver-state.json").read_text())
    (run_dir / ".stemma/driver-state.json").write_text(json.dumps({**state,
                                                                   "usage_limited": True}))
    assert not stop.decide(hook(), run_dir).block
    (run_dir / ".stemma/driver-state.json").write_text(json.dumps(state))
    c = Clock()

    def sleep(s: float) -> None:
        c.sleep(s)
        (run_dir / ".stemma/driver-state.json").write_text(
            json.dumps({**state, "usage_limited": True}))

    d = stop.decide(hook(), run_dir, clock=c, sleep=sleep, wait_s=200)
    assert not d.block and "usage stop" in d.detail["why"] and c.t - 4070908800.0 <= 5


def test_a_new_launch_starts_its_idle_count_afresh(run_dir: Path) -> None:
    # Codex review #7.
    _branch(run_dir, "b1")
    c = Clock()
    for _ in range(stop.MAX_IDLE_REFUSALS):
        stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=1)
    assert not stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=1).block
    state = json.loads((run_dir / ".stemma/driver-state.json").read_text())
    state["lead"]["launches"] = 2  # the driver resumed the lead
    (run_dir / ".stemma/driver-state.json").write_text(json.dumps(state))
    assert stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=1).block


def test_endless_idle_refusals_end(run_dir: Path) -> None:
    _branch(run_dir, "b1")
    c = Clock()
    seen = [stop.decide(hook(), run_dir, clock=c, sleep=c.sleep, wait_s=1).block
            for _ in range(stop.MAX_IDLE_REFUSALS + 1)]
    assert seen[:stop.MAX_IDLE_REFUSALS] == [True] * stop.MAX_IDLE_REFUSALS
    assert seen[-1] is False


def _entry(run_dir: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-m", "stemma.hooks", "stop"], input=stdin,
                          capture_output=True, text=True, check=False, timeout=60,
                          env={"PATH": os.environ["PATH"], "STEMMA_RUN_DIR": str(run_dir)})


def test_the_entry_point_blocks_the_lead_with_its_reason(run_dir: Path) -> None:
    r = _entry(run_dir, json.dumps(hook()))
    assert r.returncode == 2 and "Nothing is in flight" in r.stderr
    assert jsonl.read_all(run_dir / "events.jsonl")[-1]["event"] == "lead_stop_refused"
    r = _entry(run_dir, json.dumps(hook("s-other")))
    assert r.returncode == 0


@pytest.mark.parametrize("stdin", ["not json", "[1]", ""])
def test_a_broken_stop_hook_lets_the_session_stop(run_dir: Path, stdin: str) -> None:
    r = _entry(run_dir, stdin)
    assert r.returncode == 0 and "stop hook failed" in r.stderr


def test_the_plugin_registers_the_stop_hook() -> None:
    hooks = json.loads((Path(__file__).resolve().parents[2] / "plugin/hooks/hooks.json")
                       .read_text())["hooks"]
    cmd = hooks["Stop"][0]["hooks"][0]
    assert cmd["command"].endswith("-m stemma.hooks stop") and cmd["timeout"] > stop.WAIT_S


# --- Codex re-check of 299f381 ---------------------------------------------------------

def test_a_usage_stop_during_the_last_sleep_lets_the_lead_stop(run_dir: Path) -> None:
    # Re-check #3: the stop lands while the hook sleeps for the last time.
    _branch(run_dir, "b1")
    state = json.loads((run_dir / ".stemma/driver-state.json").read_text())
    c = Clock()

    def sleep(s: float) -> None:
        c.sleep(s)
        if c.t - 4070908800.0 >= 10:  # the final sleep of a 10 s wait
            (run_dir / ".stemma/driver-state.json").write_text(
                json.dumps({**state, "usage_limited": True}))

    d = stop.decide(hook(), run_dir, clock=c, sleep=sleep, wait_s=10)
    assert not d.block and "usage stop" in d.detail["why"]


def test_scanning_time_is_not_added_to_the_time_left(run_dir: Path) -> None:
    # Re-check #6: every clock read costs 1 s (a slow scan); 20 s of lead time left.
    run = json.loads((run_dir / "run.json").read_text())
    (run_dir / "run.json").write_text(json.dumps({**run, "limits": {"lead_max_s": 20}}))
    _branch(run_dir, "b1")
    t = [4070908800.0]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    def sleep(s: float) -> None:
        t[0] += s

    d = stop.decide(hook(), run_dir, clock=clock, sleep=sleep, wait_s=240)
    assert not d.block
    assert t[0] - 4070908800.0 <= 20 + 2  # the account's 20 s, not 20 plus the scans
