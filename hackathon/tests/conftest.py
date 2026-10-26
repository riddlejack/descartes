"""An invented, finished-looking run folder built with the real register.

Nothing here is from real work. The events are part B's recorded walking-skeleton events
(placeholder roles, no research content)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from stemma.register.store import Caller, Register

REPO = Path(__file__).resolve().parents[2]
TEXT = "Larkspur Ferries carried 1.92 million passengers on revenue of 48.7m."


class FakeEvidence:
    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        return quote in TEXT

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        return TEXT


def make_run(root: Path) -> Path:
    run = root / "20261026-120000-abc123"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": run.name, "as_of": None,
                                              "created_at": "2026-10-26T12:00:00Z",
                                              "harness_version": "2.1.308 (Claude Code)"}))
    (run / "task").mkdir()
    (run / "task" / "question.md").write_text("Should Larkspur Ferries add a winter route?\n")
    reg = Register(run, evidence=FakeEvidence())
    worker = Caller("stemma:worker", "a1", "s-lead")
    for statement, number, written, quote in (
            ("Revenue was 48.7m.", 48.7e6, "48.7m", "revenue of 48.7m"),
            ("Passengers were 1.92 million.", 1.92e6, "1.92 million", "1.92 million passengers")):
        reg.file_claim(worker, {"statement": statement, "tier": "observed", "branch_id": "b1",
                                "entities": ["Larkspur Ferries"],
                                "value": {"number": number, "kind": "level",
                                          "as_written": written},
                                "support": [{"receipt_id": "r-annual", "quote": quote}]})
    (run / "store" / "receipts").mkdir(parents=True)
    (run / "store" / "receipts" / "r-annual.json").write_text(json.dumps(
        {"receipt_id": "r-annual", "status": "ok", "kind": "url", "sha256": "ab" * 32,
         "http": {"requested_url": "https://larkspur.example/ar.pdf"}}))
    (run / "store" / "bytes").mkdir()
    (run / "store" / "bytes" / ("ab" * 32)).write_bytes(b"%PDF raw bytes are never recorded")
    (run / "deliverable").mkdir()
    (run / "deliverable" / "answer.reader.md").write_text(
        "# Add the winter route\n\nRevenue was 48.7m (claim 1).\n")
    shutil.copy(REPO / "tests" / "e2e" / "recorded" / "events.jsonl", run / "events.jsonl")
    (run / "datalayer").mkdir(exist_ok=True)
    (run / "datalayer" / "queries.jsonl").write_text(json.dumps(
        {"engine": "zetaris", "purpose": "query", "sql": "SELECT claim_id FROM x.claim",
         "rows": 2, "ok": True, "ms": 812}) + "\n")
    for private in (".claude/settings.json", ".stemma/state.json", "plugin/agents/lead.md"):
        (run / private).parent.mkdir(parents=True, exist_ok=True)
        (run / private).write_text("driver-internal\n")
    return run


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return make_run(tmp_path / "runs")
