"""Run 4, CoreWeave from a frozen web: the real plugin, live, from the pre-registered task.

Not collected by pytest. Needs STEMMA_RUNS, STEMMA_CLAUDE_BIN, STEMMA_CLAUDE_CONFIG and
STEMMA_PYTHON (and STEMMA_SEC_IDENTITY for the SEC tools). Creates the run from the
pre-registered task and resource limits, prints the run folder first, and runs it to the
end through `run_to_end`, which resumes by itself after a usage stop .

    python tests/e2e/run4_demo.py [--question-file Q.json] [--dry-run]

`--question-file` replaces the question, and with it the named options, from a JSON
object: `question` (required), `options` (a list; absent means the user named none) and
`intake` (absent keeps the pre-registered answers). `--dry-run` builds and validates the
task and the driver's configuration, prints both, and creates nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from stemma.driver.config import DriverConfig, Limits
from stemma.driver.errors import DriverError
from stemma.driver.runfolder import create_run
from stemma.driver.runner import run_to_end
from stemma.driver.task import load_task

TASK: dict[str, Any] = {
    "question": ("As of 30 September 2026, should a holder of CoreWeave's 9.625% senior notes "
                 "due 2032 keep holding them through 31 December 2026? Assess CoreWeave's "
                 "capacity to meet its obligations, the notes' expected return and the risks "
                 "to their value."),
    "scope": "question_only",
    "intake": {
        "What is the decision or purpose?": ("decide whether to keep holding CoreWeave's 9.625% "
                                              "senior notes due 2032 from 1 October through 31 "
                                              "December 2026, and what to monitor; give the "
                                              "forecasts in the attached brief for 1-26 October "
                                              "2026"),
        "Who asked for it, and who will use it?": "a fixed-income portfolio manager",
        "How will it be used?": "a decision memo with explicit forecasts",
        "How much detail?": "a thorough memo with its supporting analysis",
        "What data do you hold?": "none; public sources only",
    },
    "attachments": ["<HOME>/.private/stemma-design/build-2026-10/prereg/run4-brief.md"],
    "as_of": "2026-10-01",
    "second_family": True,
}  # no switches, no roles: the defaults; no criteria: the lead drafts them (D4)

# The pre-registered configuration; the run refuses to start if the driver's defaults
# no longer give it (a changed default must not silently change a pre-registered run).
PREREGISTERED_SWITCHES = {
    "s1_method": "decomposition",
    "s2_shape": "team",
    "s3_hypothesis_in_briefs": False,
    "s4_orientation": "frame_first",
    "s6_cross_family": "off",
}
PREREGISTERED_ROLE = {"model": "claude-opus-5-5", "effort": "high"}

# Resource limits from run 4's pre-registration (never a quality bar; recorded as such in
# run.json): lead 3 h across resumes (a backstop: reaching it ends the run with no
# deliverable), one remaining-time notice at 65% of it, 6 checks at once, a check stopped
# at 60 min, 3 launches per check request, 2 review rounds, at most 6 usage resumes.
LIMITS = Limits(max_concurrent_checks=6, check_max_s=3600.0, lead_max_s=3 * 3600.0,
                max_check_attempts=3, max_review_rounds=2, max_usage_resumes=6,
                lead_notice_fractions=(0.65,))

_QUESTION_KEYS = frozenset({"question", "options", "intake"})


def task_with(question_file: Path | None) -> dict[str, Any]:
    """The pre-registered task, or the same task with the question block replaced."""
    task: dict[str, Any] = json.loads(json.dumps(TASK))
    if question_file is None:
        return task
    data = json.loads(question_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{question_file}: must be a JSON object")
    unknown = sorted(set(data) - _QUESTION_KEYS)
    if unknown:
        raise SystemExit(f"{question_file}: unknown keys {unknown}; allowed "
                         f"{sorted(_QUESTION_KEYS)}")
    if "question" not in data:
        raise SystemExit(f"{question_file}: 'question' is required")
    task["question"] = data["question"]
    # New question: its own named options, or none (the old ones belong to the old one).
    if "options" in data:
        task["options"] = data["options"]
    else:
        task.pop("options", None)
    if "intake" in data:
        task["intake"] = data["intake"]
    else:
        print("note: question replaced; the pre-registered intake answers are kept",
              file=sys.stderr, flush=True)
    return task


def check_preregistered(task_path: Path) -> None:
    """Validate with the driver's own reader, and hold the switches and models to the
    pre-registration."""
    t = load_task(task_path)
    switches = {k: v for k, v in t.switches.items() if k != "s5_models"}
    if switches != PREREGISTERED_SWITCHES:
        raise SystemExit(f"switches {switches} differ from the pre-registered "
                         f"{PREREGISTERED_SWITCHES}")
    off = {r: m for r, m in t.switches["s5_models"].items() if m != PREREGISTERED_ROLE}
    if off:
        raise SystemExit(f"roles not at {PREREGISTERED_ROLE}: {off}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run4_demo.py")
    ap.add_argument("--question-file", type=Path, default=None,
                    help="JSON object with 'question', optional 'options' and 'intake'")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and print the task and configuration; create nothing")
    args = ap.parse_args(argv)
    task = task_with(args.question_file)
    work = Path(tempfile.mkdtemp(prefix="stemma-run4-task-"))
    task_path = work / "task.json"
    task_path.write_text(json.dumps(task, indent=1, ensure_ascii=False), encoding="utf-8")
    try:
        check_preregistered(task_path)
        cfg = DriverConfig.from_env()
    except DriverError as exc:
        print(f"run4: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("task:", json.dumps(task, indent=1, ensure_ascii=False), sep="\n", flush=True)
    print("limits:", json.dumps(LIMITS.to_json()), flush=True)
    if args.dry_run:
        print(f"dry run: task valid, driver configuration loads (runs: {cfg.runs_dir})")
        return 0
    run_dir = create_run(cfg, task_path, limits=LIMITS)
    print(f"run folder: {run_dir}", flush=True)
    return run_to_end(cfg, run_dir, resume_first=False, limits=LIMITS)


if __name__ == "__main__":
    sys.exit(main())
