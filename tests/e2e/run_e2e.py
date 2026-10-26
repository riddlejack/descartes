"""One tiny end-to-end launch with placeholder roles (live: uses the pinned Claude Code).

Not collected by pytest. Run it as

    STEMMA_RUNS=<folder> STEMMA_CLAUDE_BIN=<pinned claude> STEMMA_CLAUDE_CONFIG=<clean config> \
    STEMMA_PYTHON=$PWD/.venv/bin/python \
    .venv/bin/python tests/e2e/run_e2e.py

It assembles a plugin source from the placeholder role texts beside this file plus the
real `plugin/hooks/` and `plugin/.mcp.json`, creates a run folder from an invented task,
puts one invented note under the run's `task/`, and runs the driver. The placeholder lead
captures the note with part A's `capture_file`, files one claim on that receipt, calls `finish_run` (refused: no verdict yet), sets
up the rest of the release gate's producers, waits for the checks the driver starts, and
calls `finish_run` again. The script then prints what the run folder shows.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from stemma.driver.config import DriverConfig
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.register import checks_api, jsonl, safeio
from stemma.register.store import Register

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TEXT = "Invented market note.\nNorthwind Traders sold 3,400 units in 2025, its first year.\n"
# Placeholder runs are short: low effort everywhere (s5 is configuration, not a guard).
MODELS = {r: {"model": "claude-opus-5-5", "effort": "low"} for r in (
    "lead", "worker", "blind-framer", "claim-checker", "branch-reviewer",
    "deciders-eye-reviewer", "evidence-auditor", "mechanical-reader", "retrospective-writer")}


def plugin_source(dst: Path) -> Path:
    shutil.copytree(HERE / "placeholder_plugin", dst)
    shutil.copytree(REPO / "plugin" / "hooks", dst / "hooks")
    shutil.copy2(REPO / "plugin" / ".mcp.json", dst / ".mcp.json")
    return dst


def invented_note(run_dir: Path) -> None:
    """An invented attachment under task/ (tool-only: written by the host, no symlink)."""
    safeio.write_text(run_dir, "task/northwind-note.txt", TEXT)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="stemma-e2e-"))
    src = plugin_source(work / "plugin-src")
    task = work / "task.json"
    task.write_text(json.dumps({
        "question": "Placeholder question for the plumbing test: are unit sales large enough?",
        "switches": {"s5_models": MODELS}}), encoding="utf-8")
    cfg = DriverConfig.from_env(src)
    run_dir = create_run(cfg, task)
    invented_note(run_dir)
    print(f"run folder: {run_dir}", flush=True)
    code = start(cfg, run_dir)

    events = jsonl.read_all(run_dir / "events.jsonl")
    reg = Register(run_dir)
    summary = {
        "driver_exit": code,
        "denials": [e.get("refusal") for e in events if e.get("event") == "denied"],
        "launches": [{k: e.get(k) for k in ("role", "prompt", "model", "effort")}
                     for e in events if e.get("event") == "session_launched"],
        "event_kinds": sorted({str(e.get("event")) for e in events}),
        "checks": {s.request.prompt + f" ({s.request.role})": [s.status, s.outcome]
                   for s in checks_api.states(run_dir).values()},
        "claims": [{"tag": c["tag"], "verdict": c["verdict"], "author":
                    reg.get_claim(c["claim_id"])["author"]} for c in reg.find_claims()],
        "finished": reg._run_facts("finished"),
        "reader_exists": (run_dir / "deliverable/answer.reader.md").is_file(),
    }
    print(json.dumps(summary, indent=1))
    return 0 if summary["finished"] else 1


if __name__ == "__main__":
    sys.exit(main())
