"""An invented run folder, built through the real evidence service and register, for the
report viewer, verify and the tamper demo. Every name and figure here is invented."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from stemma.evidence.service import EvidenceService
from stemma.register.store import Caller, Register

LEAD = Caller("stemma:lead", None, "s-lead")
WORKER = Caller("stemma:worker", "a1", "s-lead")
QUESTION = "Will the invented town of Lowmarsh have 400 public benches by 2030?"
SOURCE = ("Lowmarsh Parks Report 2026\n\nThe parks office counted 212 public benches on "
          "1 March 2026, up from 180 a year earlier.\nMost new benches stand along the river "
          "path.\n")
SECOND = "Lowmarsh council minutes\n\nThe council approved funding for 40 benches a year.\n"
# A re-run starts Python with -I, so the script's own folder is put on the path by hand.
SCRIPT = ("import os, sys\n"
          "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
          "from stemma.analysis import inputs, result\n"
          "from helper import years_left\n"
          "v = inputs('{now}', '{rate}')\n"
          "result(v['{now}'] + v['{rate}'] * years_left())\n")
HELPER = "def years_left():\n    return 4\n"


def capture(run_dir: Path, name: str, text: str) -> str:
    (run_dir / "task").mkdir(parents=True, exist_ok=True)
    (run_dir / "task" / name).write_text(text, encoding="utf-8")
    rec = EvidenceService(run_dir).capture_file(f"task/{name}", "test fixture")
    assert rec.get("status") == "ok", rec
    return str(rec["receipt_id"])


def build(run_dir: Path) -> dict[str, str]:
    """Build the run; returns the ids a test needs."""
    run_dir.mkdir(parents=True)
    task = {"question": QUESTION, "scope": "question_only",
            "criteria": ["Says whether 400 benches is reached by 2030",
                         "States today's count and its source"]}
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "20300101-000000-abcdef", "created_at": "2030-01-01T00:00:00Z",
        "task": task, "models": {"lead": {"model": "claude-opus-5-5", "effort": "high"}}}))
    (run_dir / "task").mkdir()
    (run_dir / "task" / "task.json").write_text(json.dumps(task))
    r1 = capture(run_dir, "parks.txt", SOURCE)
    r2 = capture(run_dir, "minutes.txt", SECOND)
    reg = Register(run_dir)
    now = reg.file_claim(WORKER, {
        "statement": "Lowmarsh had 212 public benches on 1 March 2026.",
        "value": {"number": 212, "unit": "benches", "period": "2026-03-01", "kind": "count",
                  "as_written": "212"},
        "tier": "observed",
        "support": [{"receipt_id": r1, "quote": "counted 212 public benches on 1 March 2026"}],
        "branch_id": "b1", "entities": ["Lowmarsh"]})["claim_id"]
    rate = reg.file_claim(WORKER, {
        "statement": "The council funds 40 benches a year.",
        "value": {"number": 40, "unit": "benches a year", "period": "2026", "kind": "rate",
                  "as_written": "40", "denominator": "a year"},
        "tier": "interested_party",
        "support": [{"receipt_id": r2, "quote": "funding for 40 benches a year"}],
        "branch_id": "b1", "entities": ["Lowmarsh council"]})["claim_id"]
    (run_dir / "analysis").mkdir()
    (run_dir / "analysis" / "proj.py").write_text(SCRIPT.format(now=now[:12], rate=rate[:12]))
    (run_dir / "analysis" / "helper.py").write_text(HELPER)
    proj = reg.file_claim(WORKER, {
        "statement": "At that pace Lowmarsh reaches about 372 benches by 2030.",
        "value": {"number": 372, "unit": "benches", "period": "2030", "kind": "count",
                  "as_written": "about 372"},
        "tier": "estimated",
        "derivation": {"script": "analysis/proj.py", "inputs": [now[:12], rate[:12]]},
        "branch_id": "b1", "entities": ["Lowmarsh"]})["claim_id"]
    # More siblings than one re-run copies, added after filing, as in a long run.
    for i in range(70):
        (run_dir / "analysis" / f"unrelated_{i}.py").write_text("X = 1\n")
    (run_dir / "draft").mkdir()
    (run_dir / "draft" / "answer.v1.md").write_text(
        "# Lowmarsh is not on track for 400 benches by 2030\n\n"
        f"There are 212 benches today [[c:{now[:12]}]]. At 40 a year [[c:{rate[:12]}]] "
        f"the town reaches about 372 by 2030 [[c:{proj[:12]}]].\n\n"
        "| Year | Benches |\n|---|---|\n"
        f"| 2026 | 212 [[c:{now[:12]}]] |\n| 2030 | 372 [[c:{proj[:12]}]] |\n")
    reg.submit_draft(LEAD, 1)
    (run_dir / "tree.json").write_text(json.dumps({"nodes": [
        {"id": "Q", "parent": None, "kind": "question", "status": "answered",
         "statement": "Does Lowmarsh reach 400 benches by 2030?"},
        {"id": "Q1", "parent": "Q", "kind": "question", "status": "answered",
         "branch_id": "b1", "statement": "How many benches are there today?"},
        {"id": "Q2", "parent": "Q", "kind": "question", "status": "set_aside",
         "set_aside_reason": "Bench removals are too few to change the answer.",
         "statement": "How many benches are removed each year?"}]}))
    return {"now": now, "rate": rate, "proj": proj, "r1": r1, "r2": r2}


_CACHE: dict[str, tuple[Path, dict[str, str]]] = {}


def fresh_copy(base: Path, dest: Path) -> dict[str, str]:
    """A copy of one invented run built once per test session (building it is slow)."""
    if "run" not in _CACHE:
        built = base / "invented-run"
        _CACHE["run"] = (built, build(built))
    src, ids = _CACHE["run"]
    shutil.copytree(src, dest, symlinks=True)
    return ids
