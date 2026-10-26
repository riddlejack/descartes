"""Create a run folder from `task.json`: `task/`, the run plugin, `CLAUDE.md`, `run.json`."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.driver import assemble as asm
from stemma.driver.config import PLUGIN_DIR, STEMMA_DIR, DriverConfig, Limits
from stemma.driver.errors import RunStateError
from stemma.driver.launch import harness_version
from stemma.driver.task import Task, load_task

RUN_JSON = "run.json"


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _write_task(run_dir: Path, task: Task) -> dict[str, Any]:
    tdir = run_dir / "task"
    tdir.mkdir()
    (tdir / "question.md").write_text(task.question.rstrip("\n") + "\n", encoding="utf-8")
    if task.intake is not None:
        text = "".join(f"## {q}\n\n{a.rstrip()}\n\n" for q, a in task.intake.items())
        (tdir / "intake.md").write_text(text, encoding="utf-8")
    names: list[str] = []
    if task.attachments:
        adir = tdir / "attachments"
        adir.mkdir()
        for p in task.attachments:
            shutil.copy2(p, adir / p.name)
            names.append(f"task/attachments/{p.name}")
    record = {"question": task.question, "intake": task.intake, "attachments": names,
              "as_of": task.as_of, "switches": task.switches_given}
    write_json_atomic(tdir / "task.json", record)
    return record


def create_run(cfg: DriverConfig, task_path: Path, limits: Limits | None = None,
               run_id: str | None = None) -> Path:
    """Make `$STEMMA_RUNS/<run_id>/` and everything the lead needs before it starts."""
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
    shutil.copy2(cfg.plugin_src / asm.STANDING_RULES, run_dir / "CLAUDE.md")
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
    write_json_atomic(run_dir / RUN_JSON, run)
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
