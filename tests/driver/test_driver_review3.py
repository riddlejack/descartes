"""The Codex recheck's driver items on a6929ff, fake binary.

(1) a revocation that cannot be completed blocks the release and the run says why;
(3) a release that a later revocation undermines is withdrawn, and the outcome follows;
    checking sessions are verified at their end before their records count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from driverkit import Fake, write_task

from stemma.driver import events
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import OUTCOME_CODES, start
from stemma.register import checks_api


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


def test_failed_revocation_blocks_the_release(cfg: DriverConfig, tmp: Path, fake: Fake,
                                              limits: Limits) -> None:
    fake.configure({"lead": {"sleep_s": 0.3}, "blind-framer": {"transcript_effort": "low",
                                                                "sleep_s": 0.3}})
    run = create_run(cfg, write_task(tmp), limits=limits)

    def broken_revoke(_run: Path, _sid: str, _why: str) -> None:
        raise OSError("register unavailable")

    code = start(cfg, run, checks_api, limits, released=lambda _r: True,
                 revoke=broken_revoke)
    # Even with the register claiming a release, the run is not released.
    assert code == OUTCOME_CODES["revocation_failed"]
    assert kinds(run, "revoke_failed")
    assert any("register unavailable" in r for r in checks_api.release_blocks(run))
    assert kinds(run, "driver_finished")[-1]["outcome"] == "revocation_failed"


def test_checking_sessions_are_verified_at_their_end(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                     limits: Limits) -> None:
    fake.configure({"lead": {"sleep_s": 0.3}, "blind-framer": {"sleep_s": 0.2}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits)
    framer = next(c for c in fake.calls() if c["role"] == "blind-framer")
    assert framer["session_id"] in checks_api.verified_sessions(run)
    assert kinds(run, "session_verified")


def test_wrong_model_session_is_never_verified(cfg: DriverConfig, tmp: Path, fake: Fake,
                                               limits: Limits) -> None:
    fake.configure({"lead": {"sleep_s": 0.3},
                    "blind-framer": {"transcript_effort": "low", "sleep_s": 0.3}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits, revoke=lambda *_a: None)
    framers = [c["session_id"] for c in fake.calls() if c["role"] == "blind-framer"]
    assert not set(framers) & checks_api.verified_sessions(run)


def test_reviews_start_before_a_claim_backlog() -> None:
    # Walking-skeleton run: 192 claim checks queued ahead of the three draft reviews.
    from dataclasses import dataclass

    from stemma.driver.runner import launch_order

    @dataclass
    class Req:
        request_id: str
        role: str
        prompt: str
        event: str
        created_at: str

    reqs = [Req(f"c{i}", "claim-checker", f"claim:{i}", "file_claim", f"t{i:03d}")
            for i in range(5)]
    reqs += [Req("d1", "deciders-eye-reviewer", "draft:1", "submit_draft", "t900"),
             Req("b1", "branch-reviewer", "branch:b1", "propose_branch_done", "t950"),
             Req("r1", "retrospective-writer", "retro:run", "finish_run", "t999")]
    order = [r.request_id for r in launch_order(reqs)]
    assert order[:2] == ["b1", "d1"] and order[2:7] == [f"c{i}" for i in range(5)]
    assert order[-1] == "r1"


def test_usage_limit_stops_launching_and_resume_reruns_the_checks(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    # Walking-skeleton run: the account's five-hour limit refused every session; the
    # driver went on launching ~90 queued checks into failures, and resume would have
    # skipped them because they had "ended".
    from stemma.driver.runner import resume

    fake.configure({"lead": {"sleep_s": 0.5, "rate_limited": True},
                    "blind-framer": {"rate_limited": True}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    for i in range(5):
        checks_api.request_check(run, "claim-checker", "claim:" + f"{i:x}" * 64, "file_claim")
    code = start(cfg, run, checks_api, limits)
    assert code == OUTCOME_CODES["usage_limited"]
    assert kinds(run, "usage_limit_hit")
    claim_launches = [c for c in fake.calls() if c["role"] == "claim-checker"]
    assert len(claim_launches) < 5  # it stopped launching once the limit was seen
    # After the reset, resume relaunches the lead and every check that never did its job.
    fake.configure({"lead": {"sleep_s": 0.3}})
    resume(cfg, run, checks_api, limits)
    states = checks_api.states(run).values()
    assert all(s.status == "ended" and s.outcome == "completed" for s in states), [
        (s.request.prompt, s.status, s.outcome) for s in states]
    assert kinds(run, "check_relaunch")


def _usage_stopped_run(cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits,
                       n_claims: int) -> Path:
    """A run the account's usage limit stopped, with `n_claims` claim checks queued."""
    fake.configure({"lead": {"sleep_s": 0.5, "rate_limited": True},
                    "blind-framer": {"rate_limited": True}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    for i in range(n_claims):
        checks_api.request_check(run, "claim-checker", "claim:" + f"{i:x}" * 64, "file_claim")
    assert start(cfg, run, checks_api, limits) == OUTCOME_CODES["usage_limited"]
    return run


def test_resume_relaunches_within_the_concurrency_limit(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    # Walking-skeleton resume: 138 checks relaunched at once; under that load the MCP
    # server was still starting at init and 73 sessions failed the canary.
    from stemma.driver.runner import resume

    fake.configure({"lead": {"sleep_s": 2.0}, "claim-checker": {"exit_code": 1}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    for i in range(6):
        checks_api.request_check(run, "claim-checker", "claim:" + f"{i:x}" * 64, "file_claim")
    start(cfg, run, checks_api, limits)
    assert sum(s.outcome == "failed" for s in checks_api.states(run).values()) == 6
    before = len(fake.calls())
    fake.configure({"lead": {"sleep_s": 1.0}, "claim-checker": {"sleep_s": 0.3}})
    resume(cfg, run, checks_api, limits)
    checks = [c for c in fake.calls()[before:] if c["role"] != "lead"]
    spans = [(c["start"], c["end"]) for c in checks]
    most = max(sum(1 for a, b in spans if a <= t < b) for t, _ in spans)
    assert most <= limits.max_concurrent_checks
    states = checks_api.states(run).values()
    assert all(s.outcome == "completed" for s in states)


def test_resume_skips_a_round_a_later_round_replaced(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    # Walking-skeleton resume relaunched a round-1 branch review whose branch was already
    # in round 2; the register rightly refused its verdict.
    from stemma.driver.runner import resume

    run = _usage_stopped_run(cfg, tmp, fake, limits, 0)
    prompt = "claim:" + "a" * 64
    old = checks_api.request_check(run, "claim-checker", prompt, "file_claim", round_no=1)
    checks_api.mark_launched(run, old.request_id, "s-old", "m", "e")
    checks_api.mark_ended(run, old.request_id, 1, "failed")
    new = checks_api.request_check(run, "claim-checker", prompt, "file_claim", round_no=2)
    fake.configure({"lead": {"sleep_s": 0.5}})
    resume(cfg, run, checks_api, limits)
    st = checks_api.states(run)
    assert st[old.request_id].outcome == "failed"
    assert st[new.request_id].outcome == "completed"
    skipped = kinds(run, "check_not_relaunched")
    assert [e["request_id"] for e in skipped] == [old.request_id]
    assert "round 2 replaced round 1" in skipped[0]["reason"]


def test_a_check_stopped_by_its_canary_is_launched_again_up_to_its_attempts(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    fake.configure({"lead": {"sleep_s": 2.0}, "blind-framer": {"init_overrides": {
        "mcp_servers": [{"name": "plugin:stemma:stemma", "status": "pending",
                         "source": "plugin"}]}}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits)
    framers = [c for c in fake.calls() if c["role"] == "blind-framer"]
    assert len(framers) == limits.max_check_attempts
    assert len(kinds(run, "check_relaunch")) == limits.max_check_attempts - 1
    given_up = kinds(run, "check_not_relaunched")
    assert len(given_up) == 1 and given_up[0]["previous_outcome"] == "canary_failed"


def test_a_queued_retry_is_not_launched_once_a_newer_round_exists(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    # Codex review of 15437e8: the round was checked when the retry was queued, not when
    # it launched, so a retry queued before round 2 was asked for still ran.
    import time

    from stemma.driver.runner import Driver

    fake.configure({"claim-checker": {"sleep_s": 0.1}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    prompt = "claim:" + "c" * 64
    old = checks_api.request_check(run, "claim-checker", prompt, "file_claim", round_no=1)
    checks_api.mark_launched(run, old.request_id, "s-old", "m", "e")
    checks_api.mark_ended(run, old.request_id, 1, "failed")
    d = Driver(cfg, run, checks_api, limits)
    d.relaunch_unended_checks()
    assert old.request_id in d.relaunch_due  # queued while it was the latest round
    new = checks_api.request_check(run, "claim-checker", prompt, "file_claim", round_no=2)
    while d.step():
        time.sleep(limits.poll_interval_s)
    st = checks_api.states(run)
    assert st[new.request_id].outcome == "completed"
    assert st[old.request_id].outcome == "failed"  # never launched again
    assert len([c for c in fake.calls() if c["role"] == "claim-checker"]) == 1
    skipped = kinds(run, "check_not_relaunched")
    assert [e["request_id"] for e in skipped] == [old.request_id]
