"""Run 3, the demo run: the real plugin, live, from the pre-registered task.

Not collected by pytest. Needs STEMMA_RUNS, STEMMA_CLAUDE_BIN, STEMMA_CLAUDE_CONFIG and
STEMMA_PYTHON (and STEMMA_SEC_IDENTITY for the SEC tools). Creates the run from the
pre-registered task and resource limits, prints the run folder first, and runs it to the
end through `run_to_end`, which resumes by itself after a usage stop (run 2's launcher
called `start`, which never does).

    python tests/e2e/run3_demo.py [--question-file Q.json] [--dry-run]

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
    "question": ("Should H.B. Fuller's board sell its Building Adhesive Solutions segment, "
                 "and does Ancora's $1.2\u20131.4 billion cash proposal value it fairly?"),
    # The options the user named (D2), fixed at framing.
    "options": [
        "Sell the segment to Ancora on its proposal or an improved one",
        ("Sell the segment to another buyer through the market check the company promised "
         "by year-end 2026"),
        "Keep the segment",
    ],
    "scope": "question_only",  # D1: a sale of the whole company is outside the question
    "intake": {
        "What is the decision or purpose?": "the board's view before the year-end market check",
        "Who asked for it, and who will use it?": "the board's strategy adviser",
        "How will it be used?": "a decision memo for the board",
        "How much detail?": "a short memo with its supporting analysis",
        "What data do you hold?": "none; public sources only",
    },
    "as_of": "2026-10-27",
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

# Resource limits from the pre-registration (never a quality bar; recorded as such in
# run.json): lead 4 h across resumes, 3 checks at once, a check stopped at 40 min,
# 3 launches per check request, 3 review rounds, at most 6 automatic usage resumes.
LIMITS = Limits(max_concurrent_checks=3, check_max_s=40 * 60.0, lead_max_s=4 * 3600.0,
                max_check_attempts=3, max_review_rounds=3, max_usage_resumes=6)

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
    ap = argparse.ArgumentParser(prog="run3_demo.py")
    ap.add_argument("--question-file", type=Path, default=None,
                    help="JSON object with 'question', optional 'options' and 'intake'")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and print the task and configuration; create nothing")
    args = ap.parse_args(argv)
    task = task_with(args.question_file)
    work = Path(tempfile.mkdtemp(prefix="stemma-run3-task-"))
    task_path = work / "task.json"
    task_path.write_text(json.dumps(task, indent=1, ensure_ascii=False), encoding="utf-8")
    try:
        check_preregistered(task_path)
        cfg = DriverConfig.from_env()
    except DriverError as exc:
        print(f"run3: {type(exc).__name__}: {exc}", file=sys.stderr)
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
