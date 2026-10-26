"""Invented documents captured through part A's real evidence service, for tests."""

from __future__ import annotations

from pathlib import Path

from stemma.evidence.service import EvidenceService


def capture_invented(run_dir: Path, name: str, text: str) -> str:
    """Write an invented attachment under the run's task/ and capture it; the receipt id."""
    (run_dir / "task").mkdir(parents=True, exist_ok=True)
    (run_dir / "task" / name).write_text(text, encoding="utf-8")
    receipt = EvidenceService(run_dir).capture_file(f"task/{name}", "test fixture")
    if receipt.get("status") != "ok" or not receipt.get("receipt_id"):
        raise AssertionError(f"capture failed: {receipt}")
    return str(receipt["receipt_id"])
