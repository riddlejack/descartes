"""Reproductions of three review findings (cross-family review of 4eea3ba), fake binary.

(3) a checking session on the wrong model or effort loses verdict authority: revoked,
    stopped, relaunched on a new session;
(5) no host write follows a planted symlink, and no temporary file lands outside a
    tool-only path;
(7) the driver reports success only when the register recorded the release.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from driverkit import Fake, write_task

from stemma.driver import events
from stemma.driver.config import TOOL_ONLY, DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.register import checks_api


def kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in events.read(run) if e["event"] == kind]


def released(_run: Path) -> bool:
    return True


# ----- (3) ----------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["transcript", "init"])
def test_wrong_model_check_is_revoked_stopped_and_relaunched(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits, where: str) -> None:
    bad: dict[str, Any] = ({"transcript_effort": "low"} if where == "transcript"
                           else {"init_overrides": {"model": "claude-fable-5-1"}})
    fake.configure({"lead": {"sleep_s": 0.3}, "blind-framer": {**bad, "sleep_s": 0.5},
                    "blind-framer@2": {}})
    revoked: list[tuple[str, str]] = []
    run = create_run(cfg, write_task(tmp), limits=limits)
    # The second attempt of the framer behaves; the fake keys behaviour by role, so
    # switch its config once the first framer has been revoked.

    def revoke(run_dir: Path, session_id: str, reason: str) -> None:
        revoked.append((session_id, reason))
        fake.configure({"lead": {"sleep_s": 0.3}})

    assert start(cfg, run, checks_api, limits, released=released, revoke=revoke) == 0
    framers = [c for c in fake.calls() if c["role"] == "blind-framer"]
    assert len(framers) == 2 and framers[0]["session_id"] != framers[1]["session_id"]
    assert revoked and revoked[0][0] == framers[0]["session_id"]
    assert "effort" in revoked[0][1] or "model" in revoked[0][1]
    ev = kinds(run, "check_revoked")
    assert ev and ev[0]["session_id"] == framers[0]["session_id"]
    rid = checks_api.request_id_for("blind-framer", "frame:own")
    st = checks_api.states(run)[rid]
    assert st.session_id == framers[1]["session_id"] and st.outcome == "completed"
    if where == "init":  # stopped at once, before its turn could finish
        assert "end" not in framers[0]


def test_wrong_model_relaunch_is_bounded(cfg: DriverConfig, tmp: Path, fake: Fake,
                                         limits: Limits) -> None:
    fake.configure({"blind-framer": {"transcript_effort": "low"}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    revoked: list[str] = []
    start(cfg, run, checks_api, limits, released=released,
          revoke=lambda _r, sid, _why: revoked.append(sid))
    framers = [c for c in fake.calls() if c["role"] == "blind-framer"]
    assert len(framers) == limits.max_check_attempts == len(revoked)
    st = next(iter(checks_api.states(run).values()))
    assert st.status == "ended" and st.outcome == "model_mismatch"


def test_lead_mismatch_is_logged_not_revoked(cfg: DriverConfig, tmp: Path, fake: Fake,
                                             limits: Limits) -> None:
    fake.configure({"lead": {"transcript_effort": "low"}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    revoked: list[str] = []
    start(cfg, run, checks_api, limits, released=released,
          revoke=lambda _r, sid, _why: revoked.append(sid))
    assert revoked == [] and kinds(run, "model_effort_mismatch")


# ----- (5) ----------------------------------------------------------------------------

def test_host_writes_follow_no_symlink(cfg: DriverConfig, tmp: Path, fake: Fake,
                                       limits: Limits) -> None:
    fake.configure({})
    run = create_run(cfg, write_task(tmp), limits=limits)
    outside = tmp / "outside.txt"
    outside.write_text("untouched")
    # Agent-style plants: each host-written name made a symlink to a file outside.
    for rel in ("status.json", "status.html", ".status.html.tmp"):
        os.symlink(outside, run / rel)
    start(cfg, run, checks_api, limits, released=released)
    assert outside.read_text() == "untouched"
    assert not (run / "status.json").is_symlink() and not (run / "status.html").is_symlink()
    json.loads((run / "status.json").read_text())


def test_no_temporary_files_outside_tool_only_paths(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                    limits: Limits) -> None:
    fake.configure({})
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits, released=released)
    tops = {p.rstrip("/").split("/")[0] for p in TOOL_ONLY}
    stray = [p.relative_to(run).as_posix() for p in run.rglob("*")
             if (".tmp" in p.name or p.name.endswith(".lock"))
             and p.relative_to(run).parts[0] not in tops
             and p.relative_to(run).as_posix() not in TOOL_ONLY]
    assert stray == []


def test_symlinked_driver_state_refused(cfg: DriverConfig, tmp: Path, fake: Fake,
                                        limits: Limits) -> None:
    from stemma.driver.errors import RunStateError
    from stemma.register.safeio import UnsafePath

    fake.configure({})
    run = create_run(cfg, write_task(tmp), limits=limits)
    outside = tmp / "elsewhere"
    outside.mkdir()
    os.rmdir(run / ".stemma" / "settings") if (run / ".stemma" / "settings").exists() else None
    os.symlink(outside, run / ".stemma" / "settings")
    with pytest.raises((UnsafePath, RunStateError)):
        start(cfg, run, checks_api, limits, released=released)
    assert list(outside.iterdir()) == []


# ----- (7) ----------------------------------------------------------------------------

def test_lead_exit_without_release_is_not_success(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                  limits: Limits) -> None:
    fake.configure({})
    run = create_run(cfg, write_task(tmp), limits=limits)
    code = start(cfg, run, checks_api, limits)  # the real register: nothing released
    assert code == 1
    st = json.loads((run / "status.json").read_text())
    assert st["state"] == "stopped" and st["outcome"] == "ended_without_release"
    assert kinds(run, "driver_finished")[-1]["outcome"] == "ended_without_release"


def test_release_is_success(cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    fake.configure({})
    run = create_run(cfg, write_task(tmp), limits=limits)
    assert start(cfg, run, checks_api, limits, released=released) == 0
    st = json.loads((run / "status.json").read_text())
    assert st["state"] == "finished" and st["outcome"] == "released"


def test_limit_and_canary_ends_are_typed(cfg: DriverConfig, tmp: Path, fake: Fake) -> None:
    short = Limits(max_concurrent_checks=2, poll_interval_s=0.05, init_timeout_s=5.0,
                   check_max_s=60.0, lead_max_s=0.5, stop_grace_s=2.0)
    fake.configure({"lead": {"sleep_s": 20}})
    run = create_run(cfg, write_task(tmp), limits=short)
    not_released = lambda _r: False
    assert start(cfg, run, checks_api, short, released=not_released) == 3
    assert json.loads((run / "status.json").read_text())["outcome"] == "stopped_by_limit"
    fake.configure({"lead": {"init_overrides": {"plugins": []}, "sleep_s": 5}})
    run2 = create_run(cfg, write_task(tmp), limits=short)
    assert start(cfg, run2, checks_api, short, released=released) == 2  # canary wins
    assert json.loads((run2 / "status.json").read_text())["outcome"] == "canary_failed"
