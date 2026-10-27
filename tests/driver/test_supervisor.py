"""The automatic resume after a usage stop (`run_to_end`), Codex review findings #1 and
#2: one supervisor owns the run through its waits, a resume is rechecked against what is
persisted, and a lead stopped by its time limit, or with no time left, is never resumed."""

from __future__ import annotations

import dataclasses
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from driverkit import Fake, write_task

from stemma.driver import events, hostio
from stemma.driver.config import STATE_FILE, DriverConfig, Limits
from stemma.driver.errors import DriverBusy
from stemma.driver.runfolder import create_run
from stemma.driver.runner import OUTCOME_CODES, lead_time_used, resume, run_to_end, start
from stemma.register import checks_api, jsonl

HERE = Path(__file__).resolve().parent


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


def automatic(run: Path) -> list[dict[str, Any]]:
    return [e for e in kinds(run, "run_resumed") if e.get("automatic")]


def _limited(fake: Fake, lead_sleep_s: float = 0.3) -> None:
    fake.configure({"lead": {"sleep_s": lead_sleep_s, "rate_limited": True},
                    "blind-framer": {"rate_limited": True}})


def test_a_lead_stopped_by_its_time_limit_is_not_resumed(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    # A checker hits the usage limit while the lead runs past its time limit.
    lim = dataclasses.replace(limits, lead_max_s=1.0, usage_wait_unknown_s=0.0)
    fake.configure({"lead": {"sleep_s": 30}, "blind-framer": {"rate_limited": True}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    waited: list[float] = []
    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim,
                      sleep=waited.append)
    assert code == OUTCOME_CODES["stopped_by_limit"]
    assert waited == [] and kinds(run, "usage_wait") == [] and automatic(run) == []
    assert kinds(run, "session_stopped_by_limit")[0]["session"] == "lead"
    ended = kinds(run, "driver_finished")
    assert ended[-1]["outcome"] == "stopped_by_limit"


def test_the_resumed_lead_gets_only_its_remaining_time(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, lead_max_s=2.5, usage_wait_unknown_s=0.0,
                              max_usage_resumes=3)
    _limited(fake, lead_sleep_s=0.5)
    run = create_run(cfg, write_task(tmp), limits=lim)

    def sleep(_s: float) -> None:  # the limit resets; the lead now works past its budget
        fake.configure({"lead": {"sleep_s": 30}})

    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim, sleep=sleep)
    assert code == OUTCOME_CODES["stopped_by_limit"]
    (wait,) = kinds(run, "usage_wait")
    (auto,) = automatic(run)
    assert 0.0 < wait["lead_run_s"] < 2.5 and wait["lead_max_s"] == 2.5
    assert auto["lead_budget_s"] == pytest.approx(2.5 - wait["lead_run_s"], abs=0.05)
    stops = kinds(run, "session_stopped_by_limit")
    assert [s["session"] for s in stops] == ["lead-r1"]
    assert stops[0]["limit_s"] == pytest.approx(auto["lead_budget_s"], abs=0.001)
    assert lead_time_used(run) == pytest.approx(2.5, abs=1.0)


def test_no_lead_time_left_refuses_the_resume(cfg: DriverConfig, tmp: Path, fake: Fake,
                                              limits: Limits) -> None:
    lim = dataclasses.replace(limits, lead_max_s=60.0, usage_wait_unknown_s=0.0)
    _limited(fake)
    run = create_run(cfg, write_task(tmp), limits=lim)

    def sleep(_s: float) -> None:  # recorded lead time uses up the budget meanwhile
        t0 = "2099-01-01T00:00:00.000Z"
        t1 = "2099-01-01T00:01:00.000Z"
        jsonl.append(run / "events.jsonl",
                     {"event": "session_launched", "role": "lead", "session": "x", "at": t0},
                     {"event": "session_ended", "role": "lead", "session": "x", "at": t1})

    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim, sleep=sleep)
    assert code == OUTCOME_CODES["stopped_by_limit"]
    (refused,) = kinds(run, "usage_resume_refused")
    assert refused["reason"].endswith("no time remains")
    assert refused["lead_run_s"] >= 60.0 and refused["lead_remaining_s"] <= 0
    assert refused["lead_time_from"].startswith("events.jsonl")
    assert len(kinds(run, "usage_wait")) == 1 and automatic(run) == []


@pytest.mark.parametrize("outcome", ["driver_stopped", "released", "release_withdrawn"])
def test_an_outcome_changed_during_the_wait_is_not_resumed(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits, outcome: str) -> None:
    lim = dataclasses.replace(limits, usage_wait_unknown_s=0.0)
    _limited(fake)
    run = create_run(cfg, write_task(tmp), limits=lim)

    def sleep(_s: float) -> None:
        state = json.loads((run / STATE_FILE).read_text())
        hostio.write_json(run, STATE_FILE, {**state, "outcome": outcome})

    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim, sleep=sleep)
    assert code == OUTCOME_CODES[outcome]
    (refused,) = kinds(run, "usage_resume_refused")
    assert refused["outcome"] == outcome and automatic(run) == []


def test_no_other_driver_runs_the_run_while_a_supervisor_waits(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, usage_wait_unknown_s=0.0, max_usage_resumes=1)
    _limited(fake)
    run = create_run(cfg, write_task(tmp), limits=lim)
    refused: list[str] = []

    def sleep(_s: float) -> None:
        for attempt in (lambda: resume(cfg, run, checks_api, lim),
                        lambda: start(cfg, run, checks_api, lim),
                        lambda: run_to_end(cfg, run, resume_first=True, api=checks_api,
                                           limits=lim)):
            with pytest.raises(DriverBusy) as exc:
                attempt()
            refused.append(str(exc.value))

    assert run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim,
                      sleep=sleep) == OUTCOME_CODES["usage_limited"]
    assert len(refused) == 3 and all("another driver" in r for r in refused)
    assert len(kinds(run, "usage_wait")) == 1 and len(automatic(run)) == 1


def test_two_supervisors_race_for_one_resume(cfg: DriverConfig, tmp: Path, fake: Fake,
                                             limits: Limits) -> None:
    lim = dataclasses.replace(limits, usage_wait_unknown_s=0.5, max_usage_resumes=1)
    _limited(fake)
    run = create_run(cfg, write_task(tmp), limits=lim)
    assert start(cfg, run, checks_api, lim) == OUTCOME_CODES["usage_limited"]
    fields = {"runs_dir": str(cfg.runs_dir), "claude_bin": str(cfg.claude_bin),
              "claude_config": str(cfg.claude_config), "python": sys.executable,
              "plugin_src": str(cfg.plugin_src)}
    sys.path.insert(0, str(HERE))
    try:
        import supervisor_race_worker as worker
    finally:
        sys.path.remove(str(HERE))
    ctx = mp.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=worker.run, args=(fields, str(run), i, barrier, results))
             for i in range(2)]
    for p in procs:
        p.start()
    deadline = time.monotonic() + 120  # resource limit: the race's own time
    got = [results.get(timeout=max(1.0, deadline - time.monotonic())) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    assert sorted(kind for _me, kind, _v in got) == ["busy", "code"]
    assert [v for _me, kind, v in got if kind == "code"] == [OUTCOME_CODES["usage_limited"]]
    assert len(kinds(run, "usage_wait")) == 1
    assert len(automatic(run)) == 1
    assert len(kinds(run, "usage_resumes_used_up")) == 1


def test_lead_time_used_counts_an_unended_launch_to_the_last_event(tmp: Path) -> None:
    run = tmp / "r"
    run.mkdir()
    jsonl.append(run / "events.jsonl",
                 {"event": "session_launched", "role": "lead", "session": "lead",
                  "at": "2099-01-01T00:00:00.000Z"},
                 {"event": "session_ended", "role": "lead", "session": "lead",
                  "at": "2099-01-01T00:00:10.000Z"},
                 {"event": "session_launched", "role": "claim-checker", "session": "c",
                  "at": "2099-01-01T00:00:11.000Z"},
                 {"event": "session_launched", "role": "lead", "session": "lead-r1",
                  "at": "2099-01-01T00:00:20.000Z"},
                 {"event": "session_ended", "role": "claim-checker", "session": "c",
                  "at": "2099-01-01T00:00:25.000Z"})
    assert lead_time_used(run) == pytest.approx(15.0)
