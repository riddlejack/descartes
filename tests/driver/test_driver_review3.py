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
