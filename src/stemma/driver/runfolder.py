"""Create a run folder from `task.json`: `task/`, the run plugin, `CLAUDE.md`, `run.json`."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.driver import assemble as asm
from stemma.driver import hostio
from stemma.driver.config import PLUGIN_DIR, STEMMA_DIR, DriverConfig, Limits
from stemma.driver.errors import RunStateError
from stemma.driver.launch import harness_version
from stemma.driver.task import Task, load_task

RUN_JSON = "run.json"


# A run id as new_run_id makes it (logical invariant): UTC date, time, 6 hex digits.
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _write_task(run_dir: Path, task: Task) -> dict[str, Any]:
    hostio.write_text(run_dir, "task/question.md", task.question.rstrip("\n") + "\n")
    if task.intake is not None:
        text = "".join(f"## {q}\n\n{a.rstrip()}\n\n" for q, a in task.intake.items())
        hostio.write_text(run_dir, "task/intake.md", text)
    names: list[str] = []
    for p in task.attachments:
        rel = f"task/attachments/{p.name}"
        hostio.write_bytes(run_dir, rel, p.read_bytes())
        names.append(rel)
    record = {"question": task.question, "intake": task.intake, "attachments": names,
              "as_of": task.as_of, "switches": task.switches_given}
    hostio.write_json(run_dir, "task/task.json", record)
    return record


def create_run(cfg: DriverConfig, task_path: Path, limits: Limits | None = None,
               run_id: str | None = None) -> Path:
    """Make `$STEMMA_RUNS/<run_id>/` and everything the lead needs before it starts.

    The folder is new and no session has run in it, so the plugin assembly may copy with
    ordinary file calls; every later host write goes through `hostio`."""
    task = load_task(task_path)
    version = harness_version(cfg)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id or new_run_id()
    run_dir = Path(os.path.realpath(cfg.runs_dir)) / rid
    try:
        run_dir.mkdir()
    except FileExistsError:
        raise RunStateError(f"run folder already exists: {run_dir}") from None
    (run_dir / STEMMA_DIR).mkdir()
    task_record = _write_task(run_dir, task)
    assembly = asm.assemble(cfg.plugin_src, run_dir / PLUGIN_DIR, task.switches)
    hostio.write_bytes(run_dir, "CLAUDE.md", (cfg.plugin_src / asm.STANDING_RULES).read_bytes())
    lim = limits or Limits()
    run = {
        "run_id": rid,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "task": task_record,
        "as_of": task.as_of,
        "switches": task.switches,
        "models": task.switches["s5_models"],
        "harness": {"binary": str(cfg.claude_bin), "version": version,
                    "config_dir": str(cfg.claude_config)},
        "plugin": assembly.to_json(),
        "limits": lim.to_json(),
    }
    hostio.write_json(run_dir, RUN_JSON, run)
    return run_dir


def load_run(run_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((run_dir / RUN_JSON).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunStateError(f"not a run folder (no run.json): {run_dir}") from None
    except json.JSONDecodeError as exc:
        raise RunStateError(f"run.json is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RunStateError("run.json is not an object")
    return data
