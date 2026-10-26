"""The evidence service's plain Python API, for other parts and for tests.

`check_quote(run_dir, receipt_id, quote)` and `read_text(run_dir, receipt_id)` are
what part B's register calls. Both read only the run's own store and never touch
the network. Both raise `EvidenceError` (typed `code`) when they cannot answer:
`bad_receipt_id`, `unknown_receipt`, `no_text`, `empty_quote`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stemma.evidence.errors import HAS_TEXT, CaptureStatus, EvidenceError
from stemma.evidence.quotes import QUOTE_NORMALISATION, find_quote
from stemma.evidence.service import EvidenceService
from stemma.evidence.store import Store

__all__ = [
    "QUOTE_NORMALISATION",
    "CaptureStatus",
    "EvidenceError",
    "EvidenceService",
    "check_quote",
    "read_text",
]


def _stored_text(run_dir: str | Path, receipt_id: str) -> tuple[dict[str, Any], str]:
    root = Path(run_dir)
    if not (root / "store").is_dir():
        raise EvidenceError("unknown_receipt", f"no evidence store in {run_dir}")
    store = Store(root)
    receipt = store.get_receipt(receipt_id)
    if receipt["status"] not in HAS_TEXT or not receipt.get("sha256"):
        raise EvidenceError(
            "no_text", f"receipt {receipt_id} has status {receipt['status']} and no stored text"
        )
    return receipt, store.get_text(receipt.get("text_id") or receipt["sha256"])


def check_quote(run_dir: str | Path, receipt_id: str, quote: str) -> dict[str, Any]:
    """Is `quote` in the receipt's stored text, after `QUOTE_NORMALISATION`?

    Returns `found`, `matches` (start and end offsets, code points in the stored
    text, and the flagged `regions` each lies in), `match_count`, `warnings` (one per
    match in hidden or invisible text or a table with merged cells: a warning for
    the checker, never a refusal), `normalisation`, `receipt_id`, `sha256`.
    """
    receipt, text = _stored_text(run_dir, receipt_id)
    found = find_quote(text, quote, receipt.get("flagged_ranges"))
    return dict(found, receipt_id=receipt_id, sha256=receipt["sha256"])


def read_text(run_dir: str | Path, receipt_id: str) -> str:
    """The whole stored text behind a receipt."""
    return _stored_text(run_dir, receipt_id)[1]
