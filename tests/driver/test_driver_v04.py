"""Driver v0.4: task.json's scope, criteria, options, roles and stop_after; the frame-only
stop (CONTRACTS v0.4 section 9)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from driverkit import Fake, write_task

from stemma.driver import events
from stemma.driver.__main__ import main
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.errors import SwitchInvalid, TaskInvalid
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.driver.task import load_task
from stemma.register import checks_api, criteria
from stemma.register.store import Caller, Register

LEAD = Caller("stemma:lead", None, "s-lead")


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


# --- task.json ------------------------------------------------------------------------

def test_task_defaults(tmp: Path) -> None:
    t = load_task(write_task(tmp))
    assert (t.scope, t.criteria, t.options, t.stop_after) == ("question_only", (), None, "full")
    assert {(v["model"], v["effort"]) for v in t.switches["s5_models"].values()} == {
        ("claude-opus-5-5", "high")}


def test_task_fields_and_roles(tmp: Path) -> None:
    t = load_task(write_task(tmp, scope="broader_decision",
                             criteria=["Says  whether\nthe target is met", "Names the count"],
                             options=["fund A", "fund B"], stop_after="frame",
                             roles={"lead": {"effort": "max"},
                                    "claim-checker": {"model": "claude-fable-5-1"}}))
    assert t.criteria == ("Says whether the target is met", "Names the count")
    assert t.options == ("fund A", "fund B") and t.stop_after == "frame"
    m = t.switches["s5_models"]
    assert m["lead"] == {"model": "claude-opus-5-5", "effort": "max"}
    assert m["claim-checker"] == {"model": "claude-fable-5-1", "effort": "high"}
    assert m["worker"] == {"model": "claude-opus-5-5", "effort": "high"}


@pytest.mark.parametrize("extra", [
    {"scope": "everything"}, {"criteria": "one"}, {"criteria": [""]}, {"criteria": ["a", "a"]},
    {"criteria": ["x"] * 13}, {"criteria": ["x" * 1001]}, {"options": ["only one"]},
    {"options": [1, 2]}, {"stop_after": "tree"}, {"unknown": 1},
])
def test_task_refuses_bad_fields(tmp: Path, extra: dict[str, Any]) -> None:
    with pytest.raises(TaskInvalid):
        load_task(write_task(tmp, **extra))


@pytest.mark.parametrize("extra", [
    {"roles": {"lead": {"model": "claude-haiku-4-5"}}},
    {"roles": {"intern": {"effort": "low"}}},
    {"roles": {"lead": {"effort": "max"}}, "switches": {"s5_models": {"lead": {"effort": "low"}}}},
])
def test_roles_are_checked_like_s5(tmp: Path, extra: dict[str, Any]) -> None:
    with pytest.raises(SwitchInvalid):
        load_task(write_task(tmp, **extra))


def test_create_run_writes_criteria_and_records_the_fields(cfg: DriverConfig, tmp: Path,
                                                           limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp, criteria=["Says whether the target is met"],
                                     options=["a", "b"], roles={"lead": {"effort": "max"}}),
                     limits=limits)
    assert criteria.read(run) == {"C1": "Says whether the target is met"}
    assert criteria.from_task(run)
    rj = json.loads((run / "run.json").read_text())
    assert rj["criteria"] == {"source": "task", "file": "criteria.md", "count": 1}
    assert rj["scope"] == "question_only" and rj["options"] == ["a", "b"]
    assert rj["stop_after"] == "full" and rj["models"]["lead"]["effort"] == "max"
    assert (run / "task/options.md").read_text().endswith("- a\n- b\n")
    # The settings of every session deny writing the user's criteria.
    from stemma.driver.runner import Driver
    from stemma.driver.settings import stemma_settings_extra, tools_for_role

    d = Driver(cfg, run, checks_api, limits)
    extra = stemma_settings_extra(d.run_dir, "lead", "run:x", tools_for_role("lead", d.tools),
                                  cfg.claude_config)
    assert f"Write(/{run / 'criteria.md'})" in extra["permissions"]["deny"]
    assert str(run / "criteria.md") in extra["sandbox"]["filesystem"]["denyWrite"]


def test_without_criteria_the_lead_writes_them(cfg: DriverConfig, tmp: Path,
                                               limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp), limits=limits)
    assert not (run / "criteria.md").exists() and not criteria.from_task(run)
    assert json.loads((run / "run.json").read_text())["criteria"]["source"] == "lead"


# --- frame-only ------------------------------------------------------------------------

def seed_frame(run: Path, *, respond: bool, verify: bool = True) -> None:
    """What the lead and the blind framer would have done through their tools."""
    reg = Register(run)
    (run / "criteria.md").write_text("- C1: a\n")  # the lead's, at intake
    reg.tree_upsert(LEAD, {"id": "q1", "kind": "question", "statement": "Today's count?"})
    (run / "framer").mkdir(exist_ok=True)
    checks_api.request_check(run, "blind-framer", "frame:own", "run_start")
    req = checks_api.pending_checks(run)[0]
    checks_api.mark_launched(run, req.request_id, "s-f1", "claude-opus-5-5", "high")
    reg.record_frame(Caller("stemma:blind-framer", None, "s-f1"), "own", "own frame")
    checks_api.mark_ended(run, req.request_id, 0, "completed")
    (run / "frame.md").write_text("frame")
    reg.request_frame_critique(LEAD)
    req = next(r for r in checks_api.pending_checks(run) if r.prompt == "frame:critique")
    checks_api.mark_launched(run, req.request_id, "s-f2", "claude-opus-5-5", "high")
    reg.record_frame(Caller("stemma:blind-framer", None, "s-f2"), "critique", "critique")
    checks_api.mark_ended(run, req.request_id, 0, "completed")
    if verify:  # the driver confirmed the critique session's model and effort
        checks_api.mark_verified(run, "s-f2", "claude-opus-5-5", "high")
    if respond:
        (run / "framer/lead-response.md").write_text("answered")


def test_frame_only_ends_when_the_frame_is_done(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                limits: Limits) -> None:
    fast = dataclasses.replace(limits, frame_settle_s=0.3)
    fake.configure({"lead": {"write_files": {"intake.md": "restated", "criteria.md": "- C1: a\n",
                                             "framer/lead-response.md": "answered"},
                             "sleep_s": 30}})
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=fast)
    seed_frame(run, respond=False)
    assert start(cfg, run, checks_api, fast) == 8
    st = json.loads((run / ".stemma/driver-state.json").read_text())
    assert st["outcome"] == "frame_complete" and st["frame_complete"]
    assert st["lead"]["outcome"] == "frame_complete"  # stopped, not left running
    assert kinds(run, "frame_complete")
    status = json.loads((run / "status.json").read_text())
    assert status["outcome"] == "frame_complete"
    # The lead could start no worker, and its first call carries the frame-only notice.
    lead_settings = json.loads((run / ".stemma/settings/lead.json").read_text())
    assert "Agent" in lead_settings["permissions"]["deny"]
    log = [json.loads(x) for x in (run / ".stemma/arrivals.jsonl").read_text().splitlines()]
    assert log[0]["kind"] == "notice" and "stops after the frame" in log[0]["text"]
    # No draft review or claim check was ever asked for.
    assert {s.request.role for s in checks_api.states(run).values()} == {"blind-framer"}


def test_frame_only_without_a_response_does_not_claim_completion(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    fast = dataclasses.replace(limits, frame_settle_s=0.1)
    fake.configure({"lead": {"write_files": {"intake.md": "i", "criteria.md": "- C1: a\n"}}})
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=fast)
    seed_frame(run, respond=False)
    code = start(cfg, run, checks_api, fast)
    assert code == 1  # ended_without_release: the lead stopped before answering the critique
    st = json.loads((run / ".stemma/driver-state.json").read_text())
    assert st["frame_missing"] == ["framer/lead-response.md does not exist"]
    assert not kinds(run, "frame_complete")


def test_frame_only_waits_for_a_verified_critique(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                 limits: Limits) -> None:
    fast = dataclasses.replace(limits, frame_settle_s=0.1)
    fake.configure({"lead": {"write_files": {"intake.md": "i", "criteria.md": "- C1: a\n",
                                             "framer/lead-response.md": "answered"}}})
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=fast)
    seed_frame(run, respond=False, verify=False)
    assert start(cfg, run, checks_api, fast) == 1
    st = json.loads((run / ".stemma/driver-state.json").read_text())
    assert st["frame_missing"] == [("the critique's session s-f2 is not verified (its model "
                                   "and effort not yet confirmed, or revoked)")]
    assert not kinds(run, "frame_complete")


def test_frame_completion_is_rechecked_after_every_session_ends(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver.runner import Driver

    fast = dataclasses.replace(limits, frame_settle_s=0.3)
    fake.configure({"lead": {"write_files": {"intake.md": "restated", "criteria.md": "- C1: a\n",
                                             "framer/lead-response.md": "answered"},
                             "sleep_s": 30}})
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=fast)
    seed_frame(run, respond=False)
    real_end = Driver._end

    def end(self: Driver, name: str, live: Any) -> None:
        real_end(self, name, live)
        if name == "lead":  # verification at a session's end revokes the critique's session
            checks_api.revoke(run, "s-f2", "transcript shows another model")

    monkeypatch.setattr(Driver, "_end", end)
    assert start(cfg, run, checks_api, fast) == 1  # ended_without_release, not frame_complete
    st = json.loads((run / ".stemma/driver-state.json").read_text())
    assert st["outcome"] == "ended_without_release" and not st["frame_complete"]
    assert st["frame_missing"] == ["the blind framer's critique is not recorded"]  # revoked
    assert st["lead"]["outcome"] == "frame_complete"  # it was stopped for the frame's end
    assert not kinds(run, "frame_complete")
    (nc,) = kinds(run, "frame_not_complete")
    assert nc["missing"] == st["frame_missing"]
    assert json.loads((run / "status.json").read_text())["outcome"] == "ended_without_release"


def test_criteria_are_in_the_frame_settle_signature(cfg: DriverConfig, tmp: Path,
                                                    limits: Limits) -> None:
    from stemma.driver.runner import FRAME_FILES, Driver

    assert "criteria.md" in FRAME_FILES
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=limits)
    d = Driver(cfg, run, checks_api, limits)
    before = d._frame_signature()
    (run / "criteria.md").write_text("- C1: a different criterion, just before the poll\n")
    assert d._frame_signature() != before


def test_a_full_run_ignores_the_frame(cfg: DriverConfig, tmp: Path, fake: Fake,
                                      limits: Limits) -> None:
    fake.configure({"lead": {"write_files": {"intake.md": "i", "criteria.md": "- C1: a\n"}}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    seed_frame(run, respond=True)
    assert start(cfg, run, checks_api, limits) == 1
    assert not kinds(run, "frame_complete")
    lead_settings = json.loads((run / ".stemma/settings/lead.json").read_text())
    assert "Agent" not in lead_settings["permissions"]["deny"]


def test_cli_frame_only_flag(cfg: DriverConfig, tmp: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    def fake_start(_cfg: DriverConfig, run_dir: Path) -> int:
        seen["run"] = json.loads((run_dir / "run.json").read_text())
        return 8

    def fake_run(_cfg: DriverConfig, run_dir: Path, *, resume_first: bool) -> int:
        assert not resume_first
        return fake_start(_cfg, run_dir)

    monkeypatch.setattr("stemma.driver.__main__.run_to_end", fake_run)
    monkeypatch.setattr("stemma.driver.__main__.DriverConfig.from_env",
                        staticmethod(lambda _p=None: cfg))
    assert main(["start", "--frame-only", str(write_task(tmp))]) == 8
    assert seen["run"]["stop_after"] == "frame"


# --- D8: a branch's re-review resumes its reviewer --------------------------------------

def _review_round(run: Path, rnd: int) -> Any:
    return checks_api.request_check(run, "branch-reviewer", "branch:b1", "propose_branch_done",
                                    round_no=rnd)


def test_a_branch_re_review_resumes_the_previous_reviewer(cfg: DriverConfig, tmp: Path,
                                                          fake: Fake, limits: Limits) -> None:
    from stemma.driver.runner import Driver

    run = create_run(cfg, write_task(tmp), limits=limits)
    r1 = _review_round(run, 1)
    checks_api.mark_launched(run, r1.request_id, "s-rev1", "claude-opus-5-5", "high")
    checks_api.mark_ended(run, r1.request_id, 0, "completed")
    checks_api.mark_verified(run, "s-rev1", "claude-opus-5-5", "high")
    r2 = _review_round(run, 2)
    d = Driver(cfg, run, checks_api, limits)
    d._launch_check(r2, relaunch=False)
    while d.live:
        d.step()
    call = [c for c in fake.calls() if c["role"] == "branch-reviewer"][-1]
    assert "--resume=s-rev1" in call["argv"]
    assert kinds(run, "reviewer_resumed")[0]["session_id"] == "s-rev1"
    st = checks_api.states(run)[r2.request_id]
    assert st.session_id == "s-rev1" and st.status == "ended"
    assert "s-rev1" in checks_api.verified_sessions(run)  # verified again after its round


@pytest.mark.parametrize("why", ["unverified", "revoked", "running"])
def test_a_reviewer_that_cannot_be_trusted_is_not_resumed(cfg: DriverConfig, tmp: Path,
                                                          fake: Fake, limits: Limits,
                                                          why: str) -> None:
    from stemma.driver.runner import Driver

    run = create_run(cfg, write_task(tmp), limits=limits)
    r1 = _review_round(run, 1)
    checks_api.mark_launched(run, r1.request_id, "s-rev1", "claude-opus-5-5", "high")
    if why != "running":
        checks_api.mark_ended(run, r1.request_id, 0, "completed")
    if why != "unverified":
        checks_api.mark_verified(run, "s-rev1", "claude-opus-5-5", "high")
    if why == "revoked":
        checks_api.revoke(run, "s-rev1", "wrong effort")
    d = Driver(cfg, run, checks_api, limits)
    assert d._resumable_reviewer(_review_round(run, 2)) is None


# --- Automatic resume after a usage stop -----------------------------------------------

def _limited(fake: Fake, resets_at: float | None) -> None:
    extra = {"resets_at": resets_at} if resets_at is not None else {}
    fake.configure({"lead": {"sleep_s": 0.3, "rate_limited": True, **extra},
                    "blind-framer": {"rate_limited": True, **extra}})


def test_a_usage_stop_waits_for_the_reset_and_resumes(cfg: DriverConfig, tmp: Path,
                                                      fake: Fake, limits: Limits) -> None:
    import time as _t

    from stemma.driver.runner import OUTCOME_CODES, run_to_end

    lim = dataclasses.replace(limits, usage_resume_margin_s=30.0)
    reset = _t.time() + 3600
    _limited(fake, reset)
    run = create_run(cfg, write_task(tmp), limits=lim)
    waited: list[float] = []

    def sleep(s: float) -> None:  # the limit resets while the driver waits
        waited.append(s)
        fake.configure({"lead": {"sleep_s": 0.3}})

    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim, sleep=sleep)
    assert code == OUTCOME_CODES["ended_without_release"]  # ran on; the fake never releases
    assert len(waited) == 1 and 3600 + 30 - 60 < waited[0] <= 3600 + 30
    w = kinds(run, "usage_wait")
    assert len(w) == 1 and w[0]["resume"] == 1 and w[0]["resets_at"] == reset
    assert len(kinds(run, "run_resumed")) == 1
    framer = [s for s in checks_api.states(run).values() if s.request.role == "blind-framer"]
    assert framer[0].outcome == "completed"  # relaunched after the reset


def test_automatic_resumes_are_bounded_and_counted_across_restarts(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    from stemma.driver.runner import OUTCOME_CODES, run_to_end

    lim = dataclasses.replace(limits, max_usage_resumes=2, usage_wait_unknown_s=5.0)
    _limited(fake, None)  # no reset time reported: the recorded default wait
    run = create_run(cfg, write_task(tmp), limits=lim)
    waited: list[float] = []
    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim,
                      sleep=waited.append)
    assert code == OUTCOME_CODES["usage_limited"]
    assert waited == [5.0, 5.0]
    assert kinds(run, "usage_resumes_used_up")[0]["resumes"] == 2
    # A driver started again keeps to the same count.
    assert run_to_end(cfg, run, resume_first=True, api=checks_api, limits=lim,
                      sleep=waited.append) == OUTCOME_CODES["usage_limited"]
    assert waited == [5.0, 5.0]


def test_a_reset_too_far_off_is_not_waited_for(cfg: DriverConfig, tmp: Path, fake: Fake,
                                               limits: Limits) -> None:
    import time as _t

    from stemma.driver.runner import OUTCOME_CODES, run_to_end

    _limited(fake, _t.time() + 3 * 24 * 3600)  # a weekly limit
    run = create_run(cfg, write_task(tmp), limits=limits)
    waited: list[float] = []
    assert run_to_end(cfg, run, resume_first=False, api=checks_api, limits=limits,
                      sleep=waited.append) == OUTCOME_CODES["usage_limited"]
    assert waited == [] and kinds(run, "usage_wait_too_long")


def test_an_interrupted_wait_stops_the_driver(cfg: DriverConfig, tmp: Path, fake: Fake,
                                              limits: Limits) -> None:
    from stemma.driver.runner import OUTCOME_CODES, run_to_end

    _limited(fake, None)
    run = create_run(cfg, write_task(tmp), limits=limits)

    def interrupted(_s: float) -> None:
        raise KeyboardInterrupt

    assert run_to_end(cfg, run, resume_first=False, api=checks_api, limits=limits,
                      sleep=interrupted) == OUTCOME_CODES["driver_stopped"]


