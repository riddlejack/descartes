"""Codex review of claude/token-accounting: accounting observes the run and never stops,
slows or changes it. Each finding reproduced on the fake harness."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from driverkit import Fake, write_task

from stemma.driver import events, usage
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import Driver, resume, start
from stemma.register import checks_api


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


def a_run(cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> Path:
    fake.configure({"lead": {"sleep_s": 0.3}, "blind-framer": {"sleep_s": 0.1}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits)
    return run


# --- 1. A torn tail or a bad record never blocks the driver or a resume ----------------

def test_a_torn_tail_and_bad_records_are_skipped_and_counted(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    run = a_run(cfg, tmp, fake, limits)
    good = (run / "usage.jsonl").read_text()
    (run / "usage.jsonl").write_text(good + "not json\n" + json.dumps({"kind": 1}) + "\n"
                                     + '{"kind": "session", "role": "le')  # torn: no newline
    d = Driver(cfg, run, checks_api, limits)  # raised before the fix
    assert d.usage_summary["skipped_lines"] == 2
    assert kinds(run, "usage_records_skipped")[-1]["lines"] == 2
    fake.configure({"lead": {"sleep_s": 0.2}})
    resume(cfg, run, checks_api, limits, reason="Resume the synthetic test.")  # resume is not blocked either
    assert (run / "usage.jsonl.torn").read_text().startswith('{"kind": "session"')
    assert len(usage.read_records(run)) > len(good.splitlines())


def test_a_record_of_an_unexpected_shape_is_counted_not_fatal() -> None:
    s = usage.summary([{"kind": "session", "role": "lead", "session": "x",
                        "tokens": {"input_tokens": "many"}}])
    assert s["skipped_records"] == 1 and s["by_role"] == {}


# --- 2. A held lock never stalls scheduling; the write is deferred -------------------

def test_a_held_usage_lock_defers_the_write_and_never_blocks(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "LOCK_WAIT_S", 0.2)
    fake.configure({"lead": {"sleep_s": 0.5}, "blind-framer": {"sleep_s": 0.1}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    fd = os.open(run / "usage.jsonl.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another holder that never lets go
    try:
        t0 = time.monotonic()
        code = start(cfg, run, checks_api, limits)
        took = time.monotonic() - t0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert took < 15  # not held up by the lock (before the fix: blocked forever)
    assert kinds(run, "usage_write_deferred")
    assert kinds(run, "driver_finished")[-1]["exit_code"] == code
    assert not usage.read_records(run)
    # 3. Once the lock is free, a resume recovers what was never written.
    fake.configure({"lead": {"sleep_s": 0.2}})
    resume(cfg, run, checks_api, limits, reason="Resume the synthetic test.")
    recs = usage.read_records(run)
    recovered = {r["session"] for r in recs if r.get("recovered")}
    assert "lead" in recovered and any(n.startswith("check-") for n in recovered)


# --- 3. A launch the driver died before recording is recovered on resume -------------

def test_resume_recovers_usage_the_driver_never_wrote(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    run = a_run(cfg, tmp, fake, limits)
    before = usage.read_records(run)
    lead_tokens = next(r["tokens"] for r in before if r["role"] == "lead")
    (run / "usage.jsonl").write_text("")  # as if the driver died before writing
    fake.configure({"lead": {"sleep_s": 0.2}})
    resume(cfg, run, checks_api, limits, reason="Resume the synthetic test.")
    recs = usage.read_records(run)
    rec_lead = next(r for r in recs if r["session"] == "lead")
    assert rec_lead["recovered"] and rec_lead["tokens"] == lead_tokens
    assert rec_lead["harness"]["cost_estimate_usd"] == 0.25  # from the saved stream
    assert len([r for r in recs if r["session"] == "lead"]) == 1  # counted once


# --- 4. One bad value skips that field, not the session; failures are alerts --------

def test_a_non_numeric_value_skips_only_that_field(tmp_path: Path) -> None:
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-26T07:00:00.000Z",
                             "message": {"id": "m1", "model": "claude-opus-5-5",
                                         "usage": {"input_tokens": 3, "output_tokens": "lots",
                                                   "cache_read_input_tokens": 9}}}) + "\n")
    tally = usage.tally(usage.read_messages(t), [("framing", float("-inf"))])
    assert tally["tokens"]["input_tokens"] == 3 and tally["tokens"]["cache_read_input_tokens"] == 9
    assert tally["tokens"]["output_tokens"] == 0
    assert tally["anomalies"] == ["message m1: output_tokens is 'lots', not a count; skipped"]


def test_an_accounting_failure_is_an_alert_on_the_status_page(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits,
        monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_path: Path) -> list[usage.Message]:
        raise OSError("transcript unreadable")

    monkeypatch.setattr(usage, "read_messages", broken)
    run = a_run(cfg, tmp, fake, limits)
    st = json.loads((run / "status.json").read_text())
    assert any(a["event"] == "usage_not_recorded" for a in st["alerts"])


# --- 5. The run's exit code never depends on its accounting --------------------------

def test_a_failed_usage_report_leaves_the_exit_code_alone(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits,
        monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(usage, "write_md", broken)
    fake.configure({"lead": {"sleep_s": 0.3}, "blind-framer": {"sleep_s": 0.1}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    code = start(cfg, run, checks_api, limits)  # raised before the fix
    assert code == kinds(run, "driver_finished")[-1]["exit_code"]
    assert kinds(run, "usage_not_recorded")[-1]["what"] == "usage.md"
