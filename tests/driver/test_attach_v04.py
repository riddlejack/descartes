"""`stemma attach` with the v0.4 driver: during an automatic usage wait (the
supervisor holds the run lock), in a frame-only run, and after release."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
from driverkit import Fake, write_task

from stemma.driver.__main__ import main
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import OUTCOME_CODES, run_to_end
from stemma.hooks.arrivals import AttachError, attach
from stemma.register import checks_api


def _lead_hook(run: Path) -> dict[str, object]:
    hook = {"hook_event_name": "PreToolUse", "tool_name": "Read",
            "tool_input": {"file_path": str(run / "task/question.md")},
            "agent_type": "stemma:lead", "session_id": "s-1", "tool_use_id": "t9",
            "cwd": str(run)}
    r = subprocess.run([sys.executable, "-I", "-m", "stemma.hooks", "pre"],
                       input=json.dumps(hook), capture_output=True, text=True, check=False,
                       env={"PATH": "/usr/bin:/bin", "STEMMA_RUN_DIR": str(run)}, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout else {}


def test_attach_during_an_automatic_usage_wait(cfg: DriverConfig, tmp: Path, fake: Fake,
                                               limits: Limits,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    lim = dataclasses.replace(limits, usage_wait_unknown_s=1.0)
    fake.configure({"lead": {"sleep_s": 0.2, "rate_limited": True},
                    "blind-framer": {"rate_limited": True}})
    run = create_run(cfg, write_task(tmp), limits=lim)
    late = tmp / "second-wave.csv"
    late.write_text("region,ports\nnorth,10\n")
    attached: list[int] = []
    monkeypatch.setenv("STEMMA_RUNS", str(cfg.runs_dir))

    def sleep(_s: float) -> None:  # while the supervisor holds the run lock
        attached.append(main(["attach", run.name, str(late)]))
        fake.configure({"lead": {"sleep_s": 0.2}})

    code = run_to_end(cfg, run, resume_first=False, api=checks_api, limits=lim, sleep=sleep)
    assert code == OUTCOME_CODES["ended_without_release"]  # resumed; the fake never releases
    assert attached == [0]
    rel = next((run / "task/arrivals").glob("*/second-wave.csv"))
    assert rel.read_text().startswith("region")
    # The resumed lead's next tool call carries the task files and the arrival.
    ctx = str(_lead_hook(run)["additionalContext"])
    assert "task/question.md, task/task.json" in ctx
    assert "second-wave.csv" in ctx and "capture_file" in ctx
    # A worker may capture it: it lies under task/, and the lead may not read it.
    from stemma.hooks import guards

    rel_s = str(rel.relative_to(run))
    assert guards.lead_read_refusal(Path(rel_s)) is not None


def test_attach_to_a_frame_only_run(cfg: DriverConfig, tmp: Path, limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=limits)
    f = tmp / "late.txt"
    f.write_text("x")
    rec = attach(run, f)
    ctx = str(_lead_hook(run)["additionalContext"])
    assert rec["path"] in ctx and "stops after the frame" in ctx


def test_a_released_run_takes_no_attachment(cfg: DriverConfig, tmp: Path,
                                            limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp), limits=limits)
    (run / "release").mkdir()
    (run / "release/manifest.json").write_text("{}")
    f = tmp / "late.txt"
    f.write_text("x")
    with pytest.raises(AttachError, match="released and sealed"):
        attach(run, f)
    assert not (run / "task/arrivals").exists()
