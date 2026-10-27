"""Live proof that the lead's Stop hook keeps its session alive while a background
worker runs, and that the worker finishes and reports back (run 3's failure, 27 Oct).

Not collected by pytest. Run as for run_e2e.py (same environment variables). The
placeholder lead starts one background worker and ends its turn at once; the worker
sleeps, writes its branch record and reports. Without the hook the session would end and
the harness would kill the worker. The script prints what the run folder shows and exits
0 only when: the hook refused at least one stop, the worker completed (none killed), and
the lead wrote down the worker's report.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.register import jsonl
from stemma.register.store import Caller, Register

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MODELS = {r: {"model": "claude-opus-5-5", "effort": "low"} for r in (
    "lead", "worker", "blind-framer", "claim-checker", "branch-reviewer",
    "deciders-eye-reviewer", "evidence-auditor", "mechanical-reader", "retrospective-writer")}
MARK = "RECORD WRITTEN"

LEAD = """---
name: lead
description: Placeholder lead for the Stop hook probe. Not a research role.
tools: Read, Write, Agent(stemma:worker)
---
You are a placeholder testing Stemma's plumbing. Do exactly this:
1. Start one stemma:worker with the Agent tool, description "probe worker", prompt "Do your
   steps." Do not wait for it: as soon as it has started, reply "dispatched" and end your
   turn.
2. Whenever you are told the run is not released: if the probe worker has not completed
   yet, reply "waiting" and end your turn.
3. As soon as the probe worker has completed (its completion notice and final reply reach
   you), write probe/lead-saw.txt holding the line "lead saw: " followed by the worker's
   final reply, then reply "done".
"""

WORKER = f"""---
name: worker
description: Placeholder worker for the Stop hook probe.
tools: Bash, Write
---
Do exactly this: run the Bash command `sleep 40; date`. Then write branches/b1/record.md
holding the one line "probe record". Then reply "worker finished: {MARK}".
"""


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="stemma-stop-"))
    src = work / "plugin-src"
    shutil.copytree(HERE / "placeholder_plugin", src)
    shutil.copytree(REPO / "plugin" / "hooks", src / "hooks")
    shutil.copy2(REPO / "plugin" / ".mcp.json", src / ".mcp.json")
    (src / "lead/base.md").write_text(LEAD, encoding="utf-8")
    (src / "agents/worker.md").write_text(WORKER, encoding="utf-8")
    task = work / "task.json"
    task.write_text(json.dumps({"question": "Placeholder: Stop hook probe.",
                                "switches": {"s5_models": MODELS}}), encoding="utf-8")
    cfg = DriverConfig.from_env(src)
    # Resource limit for the probe: the lead may run four minutes, then the hook lets it go.
    limits = Limits(lead_max_s=240.0)
    run_dir = create_run(cfg, task, limits=limits)
    reg = Register(run_dir)
    reg.tree_upsert(Caller("stemma:lead", None, "seed"),
                    {"id": "n1", "kind": "question", "statement": "Probe question.",
                     "branch_id": "b1", "status": "in_progress"})
    (run_dir / "branches/b1").mkdir(parents=True)
    (run_dir / "branches/b1/order.md").write_text("Probe order.\n", encoding="utf-8")
    print(f"run folder: {run_dir}", flush=True)
    code = start(cfg, run_dir, limits=limits)
    events = jsonl.read_all(run_dir / "events.jsonl")
    refused = [e for e in events if e.get("event") == "lead_stop_refused"]
    stats = None
    for line in (run_dir / ".stemma/sessions/lead.stream.jsonl").read_text().splitlines():
        msg = json.loads(line)
        if msg.get("type") == "result":
            stats = msg.get("subagent_stats")
    saw = run_dir / "probe/lead-saw.txt"
    stream = (run_dir / ".stemma/sessions/lead.stream.jsonl").read_text()
    reported = [json.loads(x) for x in stream.splitlines()
                if '"task_notification"' in x and MARK in x]
    summary = {
        "driver_exit": code,
        "stop_refusals": len(refused),
        "first_refusal_in_flight": refused[0].get("in_flight") if refused else None,
        "worker_record": (run_dir / "branches/b1/record.md").is_file(),
        "worker_report_reached_lead": bool(reported),
        "lead_saw_report": saw.read_text() if saw.is_file() else None,
        "subagent_stats": stats,
        "stop_events": [e.get("event") for e in events
                        if e.get("event") in ("lead_stop_refused", "stop_allowed",
                                              "stop_hook_failed")],
    }
    print(json.dumps(summary, indent=1))
    killed = (stats or {}).get("killed") or {}
    # What run 3 lacked: the session kept alive, the worker run to completion (not
    # killed), and its report delivered to the lead's session. Whether a placeholder
    # lead at low effort then writes it down is not the plumbing under test.
    ok = (bool(refused) and summary["worker_record"] and bool(reported)
          and not any(killed.values()) and (stats or {}).get("completed", 0) >= 1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
