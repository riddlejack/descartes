"""The walking-skeleton run (docs/notes/walking-skeleton.md): the real plugin, live.

Not collected by pytest. Needs STEMMA_RUNS, STEMMA_CLAUDE_BIN, STEMMA_CLAUDE_CONFIG and
STEMMA_PYTHON. Creates the run from the pre-registered task with small resource limits
and runs the driver on the repository's own plugin/; prints the run folder first.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start

TASK = {
    "question": ("Is the United States on track to reach 500,000 public EV charging ports "
                 "by 2030?"),
    "intake": {
        "What is the decision or purpose?": "a briefing for a policy analyst",
        "Who asked for it, and who will use it?": "a policy analyst",
        "How will it be used?": "decide whether the target needs new measures",
        "How much detail?": "a short memo",
        "What data do you hold?": "none",
    },
    "switches": {"s1_method": "decomposition", "s2_shape": "team",
                 "s3_hypothesis_in_briefs": True, "s4_orientation": "frame_first"},
}
# Resource limits that keep the run small (not a quality bar; recorded as such).
LIMITS = Limits(max_concurrent_checks=3, check_max_s=40 * 60.0, lead_max_s=4 * 3600.0,
                max_check_attempts=3)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="stemma-ws-task-"))
    task = work / "task.json"
    task.write_text(json.dumps(TASK, indent=1), encoding="utf-8")
    cfg = DriverConfig.from_env()
    run_dir = create_run(cfg, task, limits=LIMITS)
    print(f"run folder: {run_dir}", flush=True)
    return start(cfg, run_dir, limits=LIMITS)


if __name__ == "__main__":
    sys.exit(main())
