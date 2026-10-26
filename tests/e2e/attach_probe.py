"""Live proof that a headless lead on the pinned harness receives an arrival notice.

Not collected by pytest. Needs STEMMA_RUNS, STEMMA_CLAUDE_BIN, STEMMA_CLAUDE_CONFIG and
STEMMA_PYTHON. Builds a copy of the repository's plugin whose lead keeps its real
frontmatter, tools and hooks but whose text is a placeholder: wait for task/arrivals,
list task/, then report every Stemma notice verbatim. While the lead waits, `stemma attach` hands it a
file; the lead's next tool call should carry the notice. Prints the run folder, the
delivery events and the lead's reply; exits 0 only if the reply quotes the notice.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from stemma.driver import events
from stemma.driver.__main__ import main as stemma_main
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import Driver, run_lock

PLACEHOLDER = """
# Lead (placeholder for the arrival-notice probe)

Do exactly these steps and nothing else.
1. Run this Bash command exactly, which waits until a file arrives:
   `until [ -d task/arrivals ]; do sleep 2; done; echo waited`
2. Run the Bash command `ls -R task`.
3. Reply with the complete text of every notice from Stemma you received in this
   session, verbatim, saying after which tool call each came; if none, reply NONE.
"""
TASK = {"question": "Placeholder question for the arrival-notice probe.",
        "intake": {"What is the decision or purpose?": "a probe"}}
# Resource limits of this probe (never a quality bar).
LIMITS = Limits(max_concurrent_checks=1, lead_max_s=900.0, check_max_s=60.0)
ATTACH_AFTER_S = 20.0  # resource limit: seconds into the lead's wait before attaching


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
    work = Path(tempfile.mkdtemp(prefix="stemma-attach-probe-"))
    (work / "task.json").write_text(json.dumps(TASK), encoding="utf-8")
    arrival = work / "client-questions.csv"
    arrival.write_text("question,owner\nIs the pace enough?,analyst\n", encoding="utf-8")
    cfg = DriverConfig.from_env(placeholder_plugin(work))
    run_dir = create_run(cfg, work / "task.json", limits=LIMITS)
    print(f"run folder: {run_dir}", flush=True)
    with run_lock(run_dir):
        d = Driver(cfg, run_dir, limits=LIMITS)
        d.start_lead(resume=False)
        started, attached = time.monotonic(), False
        while d.step():
            if not attached and time.monotonic() - started > ATTACH_AFTER_S:
                code = stemma_main(["attach", str(run_dir), str(arrival)])
                print(f"stemma attach exit {code}", flush=True)
                attached = True
            time.sleep(LIMITS.poll_interval_s)
    sent = [e for e in events.read(run_dir) if e["event"] == "arrival_notice_delivered"]
    print("delivered:", json.dumps(sent), flush=True)
    reply = str((d.state["lead"].get("result") or {}).get("result") or "")
    print("lead reply:\n" + reply, flush=True)
    ok = bool(sent) and "task/arrivals/1/client-questions.csv" in reply
    print("PROVEN" if ok else "NOT PROVEN", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
