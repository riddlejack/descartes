"""N1 in the driver: the host asks the second family about cited claims with verified
Claude verdicts, records the answers, starts a third look on a disagreement, and carries
on loudly when Nightshift is unavailable."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from driverkit import write_task

from stemma.driver import events, runner
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run, second_family_setting
from stemma.driver.runner import Driver
from stemma.register import checks_api
from stemma.register import second_family as sf
from stemma.register.store import Caller, Register

TEXT = "Acme Widgets reported revenue of 1.2bn in fiscal 2025, up from 1.0bn."
LEAD = Caller("stemma:lead", None, "s-lead")
WORKER = Caller("stemma:worker", "a1", "s-lead")


class Ev:
    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        return quote in TEXT

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        return TEXT


def seeded(cfg: DriverConfig, tmp: Path, limits: Limits, monkeypatch: pytest.MonkeyPatch,
           **task: Any) -> tuple[Path, str]:
    run = create_run(cfg, write_task(tmp, **task), limits=limits)
    reg = Register(run, evidence=Ev())
    monkeypatch.setattr("stemma.register.store.open_register",
                        lambda rd: Register(rd, evidence=Ev()))
    (run / "criteria.md").write_text("- C1: a\n")
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x"})
    cid = reg.file_claim(WORKER, {
        "statement": "Acme Widgets' fiscal 2025 revenue was 1.2bn.", "tier": "observed",
        "value": {"number": 1.2e9, "unit": "USD", "period": "FY2025", "kind": "level",
                  "as_written": "1.2bn"},
        "support": [{"receipt_id": "r1", "quote": "revenue of 1.2bn in fiscal 2025"}],
        "branch_id": "b1", "entities": []})["claim_id"]
    (run / "draft").mkdir()
    (run / "draft/answer.v1.md").write_text(f"# A\n\nRevenue 1.2bn [[c:{cid[:12]}]].\n")
    reg.submit_draft(LEAD, 1)
    req = next(r for r in checks_api.pending_checks(run) if r.prompt == "doc:r1")
    checks_api.mark_launched(run, req.request_id, "s-claude", "claude-opus-5-5", "high")
    reg.record_claim_verdict(Caller("stemma:claim-checker", None, "s-claude"), cid, "holds", "ok")
    checks_api.mark_ended(run, req.request_id, 0, "completed")
    checks_api.mark_verified(run, "s-claude", "claude-opus-5-5", "high")
    return run, cid


def drive(d: Driver, until: float = 10.0) -> None:
    end = time.monotonic() + until
    d._second_family_step()
    while d._sf_futures and time.monotonic() < end:
        time.sleep(0.02)
        d._second_family_step()


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


def test_the_host_asks_records_and_starts_a_third_look(cfg: DriverConfig, tmp: Path,
                                                      limits: Limits,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    run, cid = seeded(cfg, tmp, limits, monkeypatch, second_family=True)
    asked: list[str] = []

    def asker(model: str, msgs: list[dict[str, str]]) -> tuple[str, str, dict[str, Any]]:
        asked.append(model)
        assert "1.2bn" in msgs[1]["content"]
        return ('{"verdict": "does_not_support", "reason": "the period is FY2024"}',
                "nvidia/nemotron-3-ultra-550b-a55b", {"provider": "nightshift"})

    monkeypatch.setattr(runner, "make_asker", lambda rd: asker)
    d = Driver(cfg, run, checks_api, limits)
    drive(d)
    assert asked == [sf.PRIMARY_MODEL]
    ans = kinds(run, "second_family_answered")
    assert ans[0]["claim_id"] == cid and ans[0]["disagrees"] is True
    assert any(r.prompt == f"recheck:{cid}" for r in checks_api.pending_checks(run))
    drive(d)  # asked once only
    assert asked == [sf.PRIMARY_MODEL]


def test_unavailable_nightshift_is_loud_and_the_run_carries_on(
        cfg: DriverConfig, tmp: Path, limits: Limits, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _cid = seeded(cfg, tmp, limits, monkeypatch, second_family=True)

    def refused(_rd: Path) -> Any:
        raise sf.SecondFamilyUnavailable("provider_auth_failed: HTTP 401")

    monkeypatch.setattr(runner, "make_asker", refused)
    d = Driver(cfg, run, checks_api, limits)
    drive(d)
    assert kinds(run, "second_family_unavailable")[0]["reason"].startswith("provider_auth")
    assert d.state["second_family_unavailable"]
    reg = Register(run, evidence=Ev())
    assert reg.second_family_record()["unavailable"]
    assert not [f for f in reg.finish_failures(1) if "second family" in f]
    drive(d)  # not asked again in this run
    assert len(kinds(run, "second_family_unavailable")) == 1


def test_switched_off_nothing_is_asked(cfg: DriverConfig, tmp: Path, limits: Limits,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    run, _cid = seeded(cfg, tmp, limits, monkeypatch, second_family=False)
    monkeypatch.setattr(runner, "make_asker", lambda rd: pytest.fail("asked while off"))
    d = Driver(cfg, run, checks_api, limits)
    drive(d)
    assert not kinds(run, "second_family_asked")


def test_the_setting_defaults_to_on_when_nightshift_is_configured() -> None:
    assert second_family_setting(None, {"METERLESS_RUNTIME_TOKEN": "x"})["enabled"] is True
    assert second_family_setting(None, {})["enabled"] is False
    off = second_family_setting(False, {"METERLESS_RUNTIME_TOKEN": "x"})
    assert off["enabled"] is False and off["source"] == "task"


def test_without_nightshift_settings_the_real_asker_is_unavailable(tmp: Path,
                                                                  monkeypatch: pytest.MonkeyPatch
                                                                  ) -> None:
    for k in ("METERLESS_RUNTIME_TOKEN", "METERLESS_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    (tmp / ".stemma").mkdir()
    with pytest.raises(sf.SecondFamilyUnavailable, match="nightshift_not_configured"):
        runner.make_asker(tmp)


def test_the_token_never_reaches_a_session(cfg: DriverConfig, tmp: Path, limits: Limits,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver.launch import driver_env

    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "rt-secret-token-77")
    run = create_run(cfg, write_task(tmp), limits=limits)
    assert json.loads((run / "run.json").read_text())["second_family"]["enabled"] is True
    assert "rt-secret-token-77" not in json.dumps(driver_env(cfg, run))


def test_an_unexpected_failure_is_terminal_and_never_asked_again(
        cfg: DriverConfig, tmp: Path, limits: Limits, monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex review of N1, #3: five steps, one request.
    run, cid = seeded(cfg, tmp, limits, monkeypatch, second_family=True)
    calls: list[str] = []

    def broken(model: str, msgs: list[dict[str, str]]) -> Any:
        calls.append(model)
        raise AttributeError("'NoneType' object has no attribute 'get'")

    monkeypatch.setattr(runner, "make_asker", lambda rd: broken)
    d = Driver(cfg, run, checks_api, limits)
    for _ in range(5):
        drive(d)
    assert len(calls) == 2  # the primary, then the fallback, once
    reg = Register(run, evidence=Ev())
    rec = reg._live_facts(f"claim:stemma:{cid}", "second_family")[-1]["value"]
    assert rec["verdict"] is None and "malformed response" in rec["failed"]
    assert reg.second_family_due() == []
