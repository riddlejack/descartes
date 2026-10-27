"""Create a run folder from `task.json`: `task/`, the run plugin, `CLAUDE.md`, `run.json`."""

from __future__ import annotations

import dataclasses
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
from stemma.driver.task import STOP_AFTER, Task, criteria_markdown, load_task

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
              "as_of": task.as_of, "switches": task.switches_given, "scope": task.scope,
              "criteria": list(task.criteria),
              "options": list(task.options) if task.options is not None else None,
              "roles": task.roles_given, "stop_after": task.stop_after}
    hostio.write_json(run_dir, "task/task.json", record)
    if task.criteria:
        # The user's criteria (D4); written by the driver, so no agent may change them.
        hostio.write_text(run_dir, CRITERIA_FILE, criteria_markdown(task.criteria))
    if task.options is not None:
        text = "".join(f"- {o}\n" for o in task.options)
        hostio.write_text(run_dir, "task/options.md", "# Options the user named\n\n" + text)
    return record


def task_files_notice(record: dict[str, Any]) -> str:
    """The notice naming every task file this run holds, in reading order."""
    files = ["task/question.md"]
    if record.get("intake") is not None:
        files.append("task/intake.md")
    if record.get("options") is not None:
        files.append("task/options.md")
    files.append("task/task.json")
    lines = ["Stemma: this run's task files are " + ", ".join(files) + "."]
    if record.get("criteria"):
        lines.append("The user's criteria are in criteria.md (written by the driver).")
    atts = list(record.get("attachments") or [])
    if atts:
        lines.append("The user attached " + ", ".join(atts) + "; a worker captures each with "
                     "capture_file (the lead reads no sources).")
    return " ".join(lines)


CRITERIA_FILE = "criteria.md"
# The notice the lead's first tool call carries in a frame-only run (CONTRACTS v0.4,
# section 9). The hook delivers it; the driver ends the run when the frame is done.
FRAME_ONLY_NOTICE = (
    "Stemma: this run stops after the frame (stop_after = frame in run.json). Write "
    "intake.md and frame.md, build the tree, ask for the frame critique, and answer it in "
    "framer/lead-response.md. Workers are not started in this run; the driver ends the "
    "run once the response is written."
)


def create_run(cfg: DriverConfig, task_path: Path, limits: Limits | None = None,
               run_id: str | None = None, stop_after: str | None = None) -> Path:
    """Make `$STEMMA_RUNS/<run_id>/` and everything the lead needs before it starts.
    `stop_after` (from the command line) overrides the task's.

    The folder is new and no session has run in it, so the plugin assembly may copy with
    ordinary file calls; every later host write goes through `hostio`."""
    task = load_task(task_path)
    if stop_after is not None:
        if stop_after not in STOP_AFTER:
            raise RunStateError(f"stop_after must be one of {list(STOP_AFTER)}")
        task = dataclasses.replace(task, stop_after=stop_after)
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
        "scope": task.scope,
        "criteria": {"source": "task" if task.criteria else "lead",
                     "file": CRITERIA_FILE, "count": len(task.criteria)},
        "options": list(task.options) if task.options is not None else None,
        "stop_after": task.stop_after,
        "harness": {"binary": str(cfg.claude_bin), "version": version,
                    "config_dir": str(cfg.claude_config)},
        "plugin": assembly.to_json(),
        "limits": lim.to_json(),
    }
    hostio.write_json(run_dir, RUN_JSON, run)
    from stemma.hooks.arrivals import queue_notice

    # The lead has no folder listing (no Glob, no LS, no reading task/ as a folder): its
    # first tool call is told which task files this run holds, so it never guesses.
    queue_notice(run_dir, "task_files", task_files_notice(task_record))
    if task.stop_after == "frame":
        from stemma.hooks.arrivals import queue_notice

        queue_notice(run_dir, "frame_only", FRAME_ONLY_NOTICE)
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
