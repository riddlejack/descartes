"""Live proof that token accounting records a real session and its subagent.

Not collected by pytest. Needs STEMMA_RUNS, STEMMA_CLAUDE_BIN, STEMMA_CLAUDE_CONFIG and
STEMMA_PYTHON. A placeholder lead (the real plugin, hooks and tools) starts one worker
subagent with a one-word job and stops. Prints usage.jsonl's records, the summary in
status.json and usage.md; exits 0 only if the lead's record matches its own stream-json
result usage and the worker's record has tokens.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from stemma.driver import usage
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import Driver, run_lock

PLACEHOLDER = """
# Lead (placeholder for the token-accounting probe)

Do exactly these steps and nothing else.
1. Start one stemma:worker subagent with this prompt: "This is a probe of token
   accounting, not research. Your order would be branches/b-probe/order.md; do not
   read it or anything else. Reply with the single word OK."
2. When it has replied, reply with the single word done.
"""
TASK = {"question": "Placeholder question for the token-accounting probe.",
        "intake": {"What is the decision or purpose?": "a probe"}}
# Resource limits of this probe (never a quality bar).
LIMITS = Limits(max_concurrent_checks=1, lead_max_s=600.0, check_max_s=60.0)


def placeholder_plugin(work: Path) -> Path:
    repo_plugin = Path(__file__).resolve().parents[2] / "plugin"
    dst = work / "plugin"
    shutil.copytree(repo_plugin, dst)
    base = dst / "lead" / "base.md"
    text = base.read_text(encoding="utf-8")
    head, _, _ = text.partition("\n---\n")  # keep the frontmatter
    base.write_text(head + "\n---\n" + PLACEHOLDER, encoding="utf-8")
    for frag in (dst / "lead").glob("s*.md"):
        frag.write_text("", encoding="utf-8")
    return dst


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="stemma-usage-probe-"))
    (work / "task.json").write_text(json.dumps(TASK), encoding="utf-8")
    cfg = DriverConfig.from_env(placeholder_plugin(work))
    run_dir = create_run(cfg, work / "task.json", limits=LIMITS)
    print(f"run folder: {run_dir}", flush=True)
    with run_lock(run_dir):
        d = Driver(cfg, run_dir, limits=LIMITS)
        d.start_lead(resume=False)
        while d.step():
            time.sleep(LIMITS.poll_interval_s)
        d._finish(d._run_outcome())
    recs = usage.read_records(run_dir)
    for r in recs:
        print(json.dumps({k: r.get(k) for k in ("kind", "role", "agent_type", "branch",
                                                "models", "efforts", "messages", "tokens",
                                                "wall_s", "harness")}), flush=True)
    print((run_dir / "usage.md").read_text(), flush=True)
    lead = next(r for r in recs if r["kind"] == "session" and r["role"] == "lead")
    worker = [r for r in recs if r["kind"] == "subagent"]
    harness = (lead.get("harness") or {}).get("usage") or {}
    same = all(harness.get(k) == lead["tokens"][k] for k in usage.TOKEN_KEYS)
    print("lead matches its result usage:", same, flush=True)
    ok = same and bool(worker) and worker[0]["tokens"]["output_tokens"] > 0
    print("PROVEN" if ok else "NOT PROVEN", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
