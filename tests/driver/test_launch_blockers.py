"""Two launch blockers the tree check found: a frame-only lead failed its own canary,
and the owner's memory file loaded into every clean session under the home folder."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from driverkit import Fake, write_task
from test_driver_v04 import kinds, seed_frame

from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.register import checks_api


def test_a_frame_only_lead_passes_its_canary_without_agent(cfg: DriverConfig, tmp: Path,
                                                           fake: Fake, limits: Limits) -> None:
    # The fake, like harness 2.1.308, leaves out a tool the settings deny by name.
    fast = dataclasses.replace(limits, frame_settle_s=0.3)
    fake.configure({"lead": {"write_files": {"intake.md": "i", "criteria.md": "- C1: a\n",
                                             "framer/lead-response.md": "answered"},
                             "sleep_s": 30}})
    run = create_run(cfg, write_task(tmp, stop_after="frame"), limits=fast)
    seed_frame(run, respond=False)
    assert start(cfg, run, checks_api, fast) == 8
    lead = [e for e in kinds(run, "canary_passed") if e["role"] == "lead"]
    assert lead and not kinds(run, "canary_failed")
    call = next(c for c in fake.calls() if c["role"] == "lead")
    settings = json.loads(Path(call["argv"][call["argv"].index("--settings") + 1]).read_text())
    assert "Agent" in settings["permissions"]["deny"]


# --- No memory file from outside the run loads into a clean session -------------------

def test_every_clean_launch_excludes_memory_from_above_the_run(cfg: DriverConfig, tmp: Path,
                                                              limits: Limits) -> None:
    import os
    import pwd

    from stemma.driver import clean

    run = create_run(cfg, write_task(tmp), limits=limits)
    cl = clean.clean_launch(cfg.claude_bin, cfg.claude_config, "run:x", cwd=run,
                            model="claude-opus-5-5", effort="high", tools=["Read"],
                            settings_path=tmp / "s.json")
    ex = set(cl.settings["claudeMdExcludes"])
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert str(home / ".claude" / "CLAUDE.md") in ex
    assert str(Path(os.path.realpath(run)).parent / "CLAUDE.md") in ex
    assert str(Path("/") / "CLAUDE.md") in ex
    assert str(cfg.claude_config / "CLAUDE.md") in ex
    assert str(run / "CLAUDE.md") not in ex and str(Path(os.path.realpath(run)) / "CLAUDE.md") \
        not in ex  # the run's own standing rules still load
    assert json.loads((tmp / "s.json").read_text())["claudeMdExcludes"] == cl.settings[
        "claudeMdExcludes"]
    # The driver's own launches carry it too.
    from stemma.driver.runner import start

    start(cfg, run, checks_api, limits)
    lead = json.loads((run / ".stemma/settings/lead.json").read_text())
    assert str(home / ".claude" / "CLAUDE.md") in lead["claudeMdExcludes"]


def test_a_memory_file_the_excludes_miss_stops_the_launch(tmp: Path) -> None:
    from stemma.driver import clean
    from stemma.driver.errors import HarnessError

    (tmp / "outer").mkdir()
    (tmp / "outer" / "CLAUDE.md").write_text("planted preferences")
    run = tmp / "outer" / "run"
    run.mkdir()
    leaks = clean.memory_leaks(run, tmp / "cfg", [])
    assert str(Path(os.path.realpath(tmp)) / "outer" / "CLAUDE.md") in leaks or \
        str(tmp / "outer" / "CLAUDE.md") in leaks
    assert clean.memory_leaks(run, tmp / "cfg", clean.memory_excludes(run, tmp / "cfg")) == []
    with pytest.raises(HarnessError, match="memory files outside the run would load"):
        orig = clean.memory_excludes
        try:
            clean.memory_excludes = lambda _c, _d: []  # type: ignore[assignment]
            clean.clean_launch(Path("/bin/true"), tmp / "cfg", "x", cwd=run,
                               model="claude-opus-5-5", effort="high", tools=["Read"],
                               settings_path=tmp / "s2.json")
        finally:
            clean.memory_excludes = orig  # type: ignore[assignment]
