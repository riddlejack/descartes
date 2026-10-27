"""The lead is told its remaining running time at recorded fractions of lead_max_s (a
resource limit), since a lead stopped at the limit leaves no deliverable; the last notice
says to submit and call finish_run. A lead that has finished is told nothing."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from driverkit import Fake, write_task

from stemma.driver import events
from stemma.driver.config import ConfigError, DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.register import checks_api


def notices(run: Path) -> list[dict[str, Any]]:
    log = run / ".stemma/arrivals.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    return [r for r in rows if str(r.get("name", "")).startswith("lead_time_")]


def test_both_notices_reach_a_running_lead(cfg: DriverConfig, tmp: Path, fake: Fake,
                                           limits: Limits) -> None:
    # The lead runs 3 s of a 6 s limit: past 0.25 and 0.4, short of the limit.
    lim = dataclasses.replace(limits, lead_max_s=6.0, lead_notice_fractions=(0.25, 0.4))
    fake.configure({"lead": {"sleep_s": 3.0}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    start(cfg, run, checks_api, lim, released=lambda _r: False)
    got = notices(run)
    assert [n["name"] for n in got] == ["lead_time_0.25", "lead_time_0.4"]
    assert "plan to submit" in got[0]["text"] and "of your 0 minutes" in got[0]["text"]
    assert "Submit your draft now and call finish_run" in got[1]["text"]
    sent = [e for e in events.read(run) if e["event"] == "lead_time_notice"]
    assert [e["fraction"] for e in sent] == [0.25, 0.4]
    assert all(e["used_s"] >= e["fraction"] * 6.0 for e in sent)
    assert not [e for e in events.read(run) if e["event"] == "session_stopped_by_limit"]


def test_a_lead_finished_before_the_notices_is_told_nothing(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, lead_max_s=60.0, lead_notice_fractions=(0.5, 0.8))
    fake.configure({"lead": {"sleep_s": 0.2}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    assert start(cfg, run, checks_api, lim, released=lambda _r: True) == 0
    assert notices(run) == []


def test_a_released_run_is_told_nothing_while_its_lead_still_runs(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    # finish_run accepted, the lead still running its last turn: no notice is due.
    lim = dataclasses.replace(limits, lead_max_s=4.0, lead_notice_fractions=(0.2, 0.4))
    fake.configure({"lead": {"sleep_s": 2.5}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    start(cfg, run, checks_api, lim, released=lambda _r: True)
    assert notices(run) == []


def test_a_lead_stopped_at_the_limit_was_told_twice_first(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, lead_max_s=2.0, lead_notice_fractions=(0.3, 0.6))
    fake.configure({"lead": {"sleep_s": 30.0}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    start(cfg, run, checks_api, lim, released=lambda _r: False)
    assert [n["name"] for n in notices(run)] == ["lead_time_0.3", "lead_time_0.6"]
    assert [e for e in events.read(run) if e["event"] == "session_stopped_by_limit"]


@pytest.mark.parametrize("bad", [(0.0,), (1.0,), (0.5, 1.2), (True,), ("0.5",)])
def test_fractions_are_between_zero_and_one(limits: Limits, bad: Any) -> None:
    with pytest.raises(ConfigError):
        dataclasses.replace(limits, lead_notice_fractions=bad)


def test_fractions_survive_run_json(limits: Limits) -> None:
    lim = dataclasses.replace(limits, lead_notice_fractions=(0.8, 0.6))
    again = Limits.from_json(json.loads(json.dumps(lim.to_json())))
    assert again.lead_notice_fractions == (0.6, 0.8)
