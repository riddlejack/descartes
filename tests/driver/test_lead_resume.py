"""A lead whose session ends before finish_run is accepted is resumed (run 3), bounded,
with a notice naming the work in flight and the workers the harness killed."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from driverkit import Fake, write_task

from stemma.driver import events
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import Driver, start
from stemma.register import checks_api


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


def test_an_ended_lead_is_resumed_a_bounded_number_of_times(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_lead_resumes=2)
    fake.configure({"lead": {"sleep_s": 0.1}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    code = start(cfg, run, checks_api, lim, released=lambda _r: False)
    assert code == 1  # ended_without_release once the resumes are used up
    resumed = kinds(run, "lead_resumed_after_end")
    assert [e["resume"] for e in resumed] == [1, 2]
    assert kinds(run, "lead_not_resumed")[0]["resumes"] == 2
    leads = [c for c in fake.calls() if c["role"] == "lead"]
    assert len(leads) == 3 and all(f"--resume={leads[0]['session_id']}" in c["argv"]
                                   for c in leads[1:])
    notices = [json.loads(x) for x in (run / ".stemma/arrivals.jsonl").read_text().splitlines()
               if json.loads(x).get("name", "").startswith("lead_resumed_")]
    assert len(notices) == 2 and "ended before finish_run" in notices[0]["text"]


def test_a_released_run_is_not_resumed(cfg: DriverConfig, tmp: Path, fake: Fake,
                                       limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_lead_resumes=2)
    fake.configure({"lead": {"sleep_s": 0.1}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    assert start(cfg, run, checks_api, lim, released=lambda _r: True) == 0
    assert not kinds(run, "lead_resumed_after_end")


def test_killed_workers_are_named_in_the_notice(cfg: DriverConfig, tmp: Path,
                                                limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp), limits=limits)
    stream = run / ".stemma/sessions/lead.stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "system", "subtype": "task_started", "task_id": "t1",
         "description": "bas-record branch"},
        {"type": "system", "subtype": "task_started", "task_id": "t2",
         "description": "valuation branch"},
        {"type": "system", "subtype": "task_started", "task_id": "t3",
         "description": "proposal branch"},
        {"type": "system", "subtype": "task_updated", "task_id": "t3",
         "patch": {"status": "completed"}},
        {"type": "system", "subtype": "task_updated", "task_id": "t1",
         "patch": {"status": "killed"}},
        {"type": "system", "subtype": "task_updated", "task_id": "t2",
         "patch": {"status": "killed"}},
    ]
    stream.write_text("".join(json.dumps(x) + "\n" for x in lines))
    d = Driver(cfg, run, checks_api, limits)
    assert d.killed_workers("lead") == ["bas-record branch", "valuation branch"]


# --- Codex review of 6782794 ---------------------------------------------------------

def _ran(run: Path, seconds: float) -> None:
    """Record a lead session that ran `seconds`, as the driver's events would."""
    from stemma.register import jsonl

    jsonl.append(run / "events.jsonl",
                 {"event": "session_launched", "role": "lead", "session": "lead",
                  "at": "2099-01-01T00:00:00.000Z", "source": "driver"},
                 {"event": "session_ended", "role": "lead", "session": "lead",
                  "at": f"2099-01-01T00:00:{int(seconds):02d}.000Z", "source": "driver"})


def test_a_resumed_lead_gets_only_its_remaining_time(cfg: DriverConfig, tmp: Path,
                                                     fake: Fake, limits: Limits) -> None:
    # Review #1: 100 s limit, 59 s already run: the resumed launch is stopped at 41 s
    # of its own, never given the full 100 again.
    lim = dataclasses.replace(limits, lead_max_s=100.0)
    run = create_run(cfg, write_task(tmp), limits=lim)
    _ran(run, 59)
    d = Driver(cfg, run, checks_api, lim)
    d.state["lead"].update({"session_id": "sid-1", "launches": 1})
    fake.configure({"lead@resume": {"sleep_s": 0.2}})
    d.start_lead(resume=True)
    assert d.lead_budget_s is not None and 40.0 <= d.lead_budget_s <= 41.0
    while d.live:
        d.step()


def test_a_usage_stop_while_the_notice_is_built_stops_the_resume(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits,
        monkeypatch: Any) -> None:
    # Review #4: the rejection lands between the decision and the launch.
    lim = dataclasses.replace(limits, max_lead_resumes=2)
    run = create_run(cfg, write_task(tmp), limits=lim)
    d = Driver(cfg, run, checks_api, lim)
    d.state["lead"].update({"session_id": "sid-1", "launches": 1, "running": False,
                            "outcome": "completed", "name": "lead"})
    import stemma.hooks.stop as stop_mod

    def flight_then_reject(_run: Path) -> dict[str, list[str]]:
        d._note_usage_stop(None)  # type: ignore[arg-type]  # a reader thread, meanwhile
        return {}

    monkeypatch.setattr(stop_mod, "in_flight", flight_then_reject)
    d._resume_lead_if_ended()
    assert not [c for c in fake.calls() if c["role"] == "lead"]
    assert kinds(run, "lead_not_resumed")[0]["reason"].startswith("a usage stop")
    persisted = json.loads((run / ".stemma/driver-state.json").read_text())
    assert persisted["usage_limited"] is True  # persisted at once, for the Stop hook


def test_a_lead_stopped_on_purpose_is_not_resumed(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                  limits: Limits) -> None:
    # Review #5: SIGTERM sent to the lead's own process (not by the driver).
    import os
    import signal
    import threading
    import time

    lim = dataclasses.replace(limits, max_lead_resumes=2)
    fake.configure({"lead": {"sleep_s": 30}})
    run = create_run(cfg, write_task(tmp), limits=lim)

    def cancel() -> None:
        end = time.time() + 20
        while time.time() < end:
            lead = [c for c in fake.calls() if c["role"] == "lead"]
            st = run / ".stemma/driver-state.json"
            if lead and st.exists() and json.loads(st.read_text())["lead"].get("running"):
                break
            time.sleep(0.05)
        time.sleep(0.5)
        import subprocess

        out = subprocess.run(["pgrep", "-f", f"run:{run.name}"], capture_output=True,
                             text=True, check=False).stdout.split()
        for pid in out:
            os.kill(int(pid), signal.SIGTERM)

    t = threading.Thread(target=cancel)
    t.start()
    code = start(cfg, run, checks_api, lim, released=lambda _r: False)
    t.join()
    assert code == 9
    assert not kinds(run, "lead_resumed_after_end")
    st = json.loads((run / ".stemma/driver-state.json").read_text())
    assert st["lead"]["outcome"] == "cancelled" and st["outcome"] == "lead_cancelled"


def test_workers_started_and_never_ended_are_named(cfg: DriverConfig, tmp: Path,
                                                   limits: Limits) -> None:
    # Review #8: the lead crashed before any task_updated record.
    run = create_run(cfg, write_task(tmp), limits=limits)
    stream = run / ".stemma/sessions/lead.stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "system", "subtype": "task_started", "task_id": "t1", "description": "a"},
        {"type": "system", "subtype": "task_started", "task_id": "t2", "description": "b"},
        {"type": "system", "subtype": "task_started", "task_id": "t3", "description": "c"},
        {"type": "system", "subtype": "task_notification", "task_id": "t3",
         "status": "completed"},
        {"type": "system", "subtype": "task_updated", "task_id": "t2",
         "patch": {"status": "killed"}},
    ]
    stream.write_text("".join(json.dumps(x) + "\n" for x in lines))
    d = Driver(cfg, run, checks_api, limits)
    assert d.worker_ends("lead") == (["b"], ["a"])


def test_the_old_usage_stop_is_cleared_on_disk_before_the_lead_launches(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits, monkeypatch: Any) -> None:
    # Re-check A: the resumed lead's Stop hook must not read the old stop.
    from stemma.driver import runner

    run = create_run(cfg, write_task(tmp), limits=limits)
    st = {"run_id": run.name, "lead": {"session_id": "sid-1", "launches": 1,
                                        "running": False, "outcome": "rate_limited"},
          "artifacts": {}, "check_attempts": {}, "usage_limited": True,
          "outcome": "usage_limited"}
    (run / ".stemma/driver-state.json").write_text(json.dumps(st))
    seen: list[bool] = []
    real = runner.Driver.start_lead

    def spy(self: Any, resume: bool) -> None:
        seen.append(json.loads((run / ".stemma/driver-state.json").read_text())
                    ["usage_limited"])
        real(self, resume)

    monkeypatch.setattr(runner.Driver, "start_lead", spy)
    fake.configure({"lead@resume": {"sleep_s": 0.1}})
    runner.resume(cfg, run, checks_api, limits, reason="Resume the synthetic test.")
    assert seen == [False]


def test_crash_downtime_is_not_charged_to_the_lead(cfg: DriverConfig, tmp: Path,
                                                   limits: Limits) -> None:
    # Re-check B: launch at 0, last activity at 10, no end recorded, resume at 1000.
    from stemma.driver import leadtime
    from stemma.register import jsonl

    run = create_run(cfg, write_task(tmp), limits=limits)
    (run / "events.jsonl").write_text("")
    jsonl.append(run / "events.jsonl",
                 {"event": "session_launched", "role": "lead", "session": "lead",
                  "at": "2099-01-01T00:00:00.000Z"},
                 {"event": "artifact_landed", "path": "intake.md",
                  "at": "2099-01-01T00:00:10.000Z"})
    rec = leadtime.close_orphaned_lead(run)
    assert rec is not None and rec["at"] == "2099-01-01T00:00:10.000Z"
    jsonl.append(run / "events.jsonl", {"event": "run_resumed",
                                        "at": "2099-01-01T00:16:40.000Z"})
    assert leadtime.lead_time_used(run) == 10.0
    assert leadtime.close_orphaned_lead(run) is None  # closed once


def test_a_rejection_being_persisted_blocks_the_launch(cfg: DriverConfig, tmp: Path,
                                                       limits: Limits) -> None:
    # Re-check #4: a reader thread has marked its session rate-limited and waits for the
    # lock to persist it; the launch decision must already see it.
    from types import SimpleNamespace

    from stemma.driver.runner import Live

    run = create_run(cfg, write_task(tmp), limits=limits)
    d = Driver(cfg, run, checks_api, limits)
    assert d._launch_allowed()
    sess = SimpleNamespace(rate_limited={"status": "rejected"},
                           spec=SimpleNamespace(request_id="r1", role="claim-checker"))
    d.live["check-x"] = Live(sess)  # type: ignore[arg-type]
    assert not d._launch_allowed()
