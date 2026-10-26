"""Part A registers the evidence tools of docs/CONTRACTS.md section 4 here.

Each tool is a thin wrapper over `EvidenceService`, serving the run folder named by
`STEMMA_RUN_DIR`. A typed failure comes back as `{"error": <code>, "message": ...}`;
a capture failure comes back as a receipt whose `status` names it.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError
from stemma.evidence.service import EvidenceService

_LOCK = threading.Lock()
_SERVICES: dict[Path, EvidenceService] = {}


def _service() -> EvidenceService:
    run_dir = os.environ.get("STEMMA_RUN_DIR")
    if not run_dir:
        raise EvidenceError("no_run_dir", "STEMMA_RUN_DIR is not set for this server")
    key = Path(run_dir).resolve()
    with _LOCK:
        if key not in _SERVICES:
            _SERVICES[key] = EvidenceService(key)
        return _SERVICES[key]


def _call(fn: Callable[[EvidenceService], Any]) -> Any:
    try:
        return fn(_service())
    except EvidenceError as exc:
        return exc.as_dict()


def capture(url: str, reason: str) -> dict[str, Any]:
    """Capture a web page or PDF as exact bytes plus whole stored text, and return its
    receipt (receipt_id, sha256, status, text_path, text_chars, outline, warnings).
    Every attempt gets a receipt, failures included; read the status. When the run
    has an as_of date the copy comes from an archive dated before it, or fails."""
    result: dict[str, Any] = _call(lambda s: s.capture(url, reason))
    return result


def capture_file(path: str, reason: str) -> dict[str, Any]:
    """Capture one of the user's attachments (a file under the run's task/ folder)."""
    result: dict[str, Any] = _call(lambda s: s.capture_file(path, reason))
    return result


def sec_filings(company: str, form: str | None = None, filed_after: str | None = None,
                filed_before: str | None = None) -> dict[str, Any]:
    """List a company's SEC filings (ticker, CIK or name) with accession numbers and
    filing dates, newest first. Dates are YYYY-MM-DD; both bounds are strict.
    filed_before defaults to the run's as_of date."""
    result: dict[str, Any] = _call(
        lambda s: s.sec_filings(company, form, filed_after, filed_before))
    return result


def sec_capture(accession: str, document: str | None = None) -> dict[str, Any]:
    """Capture one SEC filing's document (its primary document unless `document`
    names another file in the filing) and return its receipt."""
    result: dict[str, Any] = _call(lambda s: s.sec_capture(accession, document))
    return result


def sec_facts(company: str, concept: str, filed_before: str | None = None,
              form: str | None = None) -> dict[str, Any]:
    """As-filed XBRL facts for one concept (e.g. us-gaap:Revenues), one row per filing
    and period, each with its filing date and the receipt of the filing it came from.
    Never collapsed to the latest figure. filed_before defaults to as_of; `form`
    (e.g. 10-K) optionally keeps only facts from that form."""
    result: dict[str, Any] = _call(lambda s: s.sec_facts(company, concept, filed_before, form))
    return result


def outline(sha256: str) -> dict[str, Any]:
    """A stored document's outline: headings or pages, each with start and end offsets."""
    result: dict[str, Any] = _call(lambda s: s.outline(sha256))
    return result


def read(sha256: str, start: int = 0, length: int = limits.READ_MAX_CHARS) -> dict[str, Any]:
    """Read part of a stored text: `length` characters (at most 20,000) from `start`.
    Offsets are Unicode code points. `next_start` says where the next part begins."""
    result: dict[str, Any] = _call(lambda s: s.read(sha256, start, length))
    return result


def read_section(sha256: str, anchor: str) -> dict[str, Any]:
    """Read one outline section by its anchor (from `outline`), up to 20,000
    characters; `next_start` continues it with `read`."""
    result: dict[str, Any] = _call(lambda s: s.read_section(sha256, anchor))
    return result


def search_store(query: str, mode: str = "words", sha256: str | None = None,
                 limit: int = 10) -> dict[str, Any]:
    """Search this run's stored texts. mode "words" matches word stems (ranked);
    mode "exact" matches the exact string, e.g. a figure such as "1,204.5". Each hit
    gives sha256, receipt_id, offsets and a short snippet."""
    result: dict[str, Any] = _call(lambda s: s.search_store(query, mode, sha256, limit))
    return result


def check_quote(receipt_id: str, quote: str) -> dict[str, Any]:
    """Check that a quotation appears in a receipt's stored text: exact match after
    collapsing whitespace and straightening typographic quotation marks. Returns
    found, and the offsets of each match."""
    result: dict[str, Any] = _call(lambda s: s.check_quote(receipt_id, quote))
    return result


def tables(sha256: str) -> dict[str, Any]:
    """Docling's table view of a stored document: a derived view for finding and
    reading tables, never for quoting. Status not_installed when Docling is absent."""
    result: dict[str, Any] = _call(lambda s: s.tables(sha256))
    return result


TOOLS: tuple[Callable[..., dict[str, Any]], ...] = (
    capture, capture_file, sec_filings, sec_capture, sec_facts, outline, read,
    read_section, search_store, check_quote, tables,
)


def register_tools(server: Any) -> None:
    """Add part A's MCP tools to the one `stemma` server (wired by part B)."""
    for tool in TOOLS:
        server.add_tool(tool, name=tool.__name__, description=(tool.__doc__ or "").strip())
