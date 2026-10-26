"""The register's view of the evidence service (part A).

The register calls exactly the two functions CONTRACTS section 4 names:
`stemma.evidence.api.check_quote(run_dir, receipt_id, quote)` (a dict with `found`)
and `stemma.evidence.api.read_text(run_dir, receipt_id)`; both raise `EvidenceError`
when they cannot answer, which the register turns into a refusal naming the check. A
missing evidence service is a typed error; there is no stand-in.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class EvidenceUnavailable(Exception):
    """The evidence service (part A) is not installed."""


class QuoteCheckFailed(Exception):
    """The evidence service could not answer for this receipt (unknown, no text...)."""


class Evidence(Protocol):
    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool: ...
    def read_text(self, run_dir: Path, receipt_id: str) -> str: ...


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
    except ImportError as exc:
        raise EvidenceUnavailable("stemma.evidence.api is not installed") from exc
    return _Adapter(api.check_quote, api.read_text, api.EvidenceError)
