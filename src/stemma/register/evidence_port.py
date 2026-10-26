"""The register's view of the evidence service (part A).

The register calls exactly the two functions CONTRACTS section 4 names:
`stemma.evidence.api.check_quote(run_dir, receipt_id, quote)` (a dict with `found`)
and `stemma.evidence.api.read_text(run_dir, receipt_id)`; both raise
`EvidenceError` when they cannot answer. Until part A lands in this branch, a run may
set `STEMMA_EVIDENCE_STUB=1` to use `stub_check_quote`, which reads the contract's
store layout (`store/receipts/<id>.json` naming `sha256`, `store/text/<sha256>.txt`)
and matches the quote exactly after collapsing whitespace. The stub is never chosen
silently: without the variable a missing service is a typed error.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class EvidenceUnavailable(Exception):
    """Neither the evidence service nor an explicitly chosen stub is available."""


class QuoteCheckFailed(Exception):
    """The evidence service could not answer for this receipt (unknown, no text...)."""


class Evidence(Protocol):
    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool: ...
    def read_text(self, run_dir: Path, receipt_id: str) -> str: ...


_WS = re.compile(r"\s+")


def _stub_text(run_dir: Path, receipt_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", receipt_id) or receipt_id.startswith("."):
        raise QuoteCheckFailed(f"receipt id {receipt_id!r} is not a plain name")
    receipt_path = run_dir / "store" / "receipts" / f"{receipt_id}.json"
    if not receipt_path.exists():
        raise QuoteCheckFailed(f"unknown receipt {receipt_id}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    text_path = run_dir / "store" / "text" / f"{receipt['sha256']}.txt"
    if not text_path.exists():
        raise QuoteCheckFailed(f"receipt {receipt_id} has no stored text")
    return text_path.read_text(encoding="utf-8")


def stub_check_quote(run_dir: Path, receipt_id: str, quote: str) -> dict[str, Any]:
    text = _WS.sub(" ", _stub_text(run_dir, receipt_id))
    q = _WS.sub(" ", quote).strip()
    if not q:
        raise QuoteCheckFailed("empty quotation")
    return {"found": q in text, "receipt_id": receipt_id}


class _Adapter:
    def __init__(self, check: Callable[..., Any], read: Callable[..., Any],
                 error: type[BaseException]) -> None:
        self._check, self._read, self._error = check, read, error

    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        try:
            result = self._check(run_dir, receipt_id, quote)
        except self._error as exc:
            raise QuoteCheckFailed(str(exc)) from exc
        found = result.get("found") if isinstance(result, dict) else None
        if not isinstance(found, bool):
            raise QuoteCheckFailed("the evidence service gave no found/not-found answer")
        return found

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        try:
            return str(self._read(run_dir, receipt_id))
        except self._error as exc:
            raise QuoteCheckFailed(str(exc)) from exc


def load() -> Evidence:
    try:
        api = importlib.import_module("stemma.evidence.api")
    except ImportError:
        api = None
    if api is not None:
        return _Adapter(api.check_quote, api.read_text, api.EvidenceError)
    if os.environ.get("STEMMA_EVIDENCE_STUB") == "1":
        return _Adapter(stub_check_quote, _stub_text, QuoteCheckFailed)
    raise EvidenceUnavailable("stemma.evidence.api is not installed")


def is_stub() -> bool:
    try:
        importlib.import_module("stemma.evidence.api")
    except ImportError:
        return True
    return False
