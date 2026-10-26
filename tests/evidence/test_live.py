"""Live tests: they reach the real web. Skipped unless STEMMA_LIVE=1.

Run once with:
    STEMMA_LIVE=1 STEMMA_LIVE_OUT=<folder> [STEMMA_SEC_IDENTITY="Name email"] \
        pytest -m live tests/evidence/test_live.py -s
Each test writes a JSON summary to STEMMA_LIVE_OUT (default: a temporary folder).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from stemma.evidence import api
from stemma.evidence.archive import Archive, as_of_cutoff
from stemma.evidence.fetch import Fetcher
from stemma.evidence.service import EvidenceService
from stemma.evidence.transport import StdlibTransport

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("STEMMA_LIVE") != "1", reason="live test; set STEMMA_LIVE=1"),
]

STATS_PAGE = "https://www.bls.gov/news.release/empsit.nr0.htm"
STATS_PDF = "https://www.bls.gov/news.release/pdf/empsit.pdf"


def out_dir() -> Path:
    folder = Path(os.environ.get("STEMMA_LIVE_OUT") or tempfile.mkdtemp(prefix="stemma-live-"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write(name: str, data: Any) -> None:
    path = out_dir() / f"{name}.json"
    path.write_text(json.dumps(data, indent=1, default=str))
    print(f"\n[live] {name}: {path}")


def new_run(as_of: str | None = None) -> EvidenceService:
    run = Path(tempfile.mkdtemp(prefix="stemma-live-run-")) / "run"
    (run / "task").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "live", "as_of": as_of}))
    return EvidenceService(run, cache_dir=None)


def summary(r: dict[str, Any]) -> dict[str, Any]:
    keys = ("receipt_id", "status", "detail", "kind", "sha256", "bytes_len", "content_type",
            "text_chars", "pages", "warnings", "final_url", "archive", "sec")
    return {k: r.get(k) for k in keys} | {"outline_entries": len(r.get("outline") or []),
                                          "http_status": (r.get("http") or {}).get("status"),
                                          "elapsed_s": (r.get("http") or {}).get("elapsed_s")}


def test_live_statistics_page_and_pdf() -> None:
    svc = new_run()
    page = svc.capture(STATS_PAGE, "live test: government statistics release")
    pdf = svc.capture(STATS_PDF, "live test: the same release as PDF")
    found: dict[str, Any] = {}
    for name, r in (("page", page), ("pdf", pdf)):
        assert r["status"] == "ok", r
        text = api.read_text(svc.run_dir, r["receipt_id"])
        figure = re.search(r"\d{1,3}(?:,\d{3})+|\d+\.\d+ percent", text)
        assert figure is not None
        hits = svc.search_store(figure.group(0), mode="exact", sha256=r["sha256"])["hits"]
        assert hits and text[hits[0]["start"]:hits[0]["end"]] == figure.group(0)
        sentence = text[max(0, figure.start() - 60):figure.end() + 20]
        assert svc.check_quote(r["receipt_id"], sentence)["found"]
        found[name] = {"figure": figure.group(0), "quote": sentence}
    assert pdf["pages"] and pdf["pages"] > 1
    write("statistics", {"page": summary(page), "pdf": summary(pdf), "checks": found})


@pytest.mark.skipif(not os.environ.get("STEMMA_SEC_IDENTITY"), reason="needs STEMMA_SEC_IDENTITY")
def test_live_sec_filing_and_facts() -> None:
    svc = new_run()
    listing = svc.sec_filings("AAPL", form="10-K", filed_before="2025-01-01")
    assert listing["filings"] and listing["filings"][0]["filing_date"] < "2025-01-01"
    latest = listing["filings"][0]
    filing = svc.sec_capture(latest["accession"])
    assert filing["status"] == "ok", filing
    facts = svc.sec_facts("AAPL", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                          filed_before="2025-01-01", form="10-K")
    assert facts["facts"] and all(f["filed"] < "2025-01-01" for f in facts["facts"])
    periods: dict[tuple[Any, Any], set[Any]] = {}
    for fact in facts["facts"]:
        periods.setdefault((fact["start"], fact["end"]), set()).add(fact["accession"])
    write("sec", {
        "listing_count": len(listing["filings"]), "latest": latest, "filing": summary(filing),
        "facts_count": len(facts["facts"]),
        "periods_reported_in_more_than_one_filing": sum(1 for v in periods.values() if len(v) > 1),
        "filing_receipts": facts["filings"], "warnings": facts["warnings"],
        "sample_facts": facts["facts"][-4:],
    })


SCRIPT_BUILT_PAGE = "https://data.worldbank.org/indicator/NY.GDP.MKTP.CD"


def test_live_browser_fallback() -> None:
    from stemma.evidence.render import default_renderer

    if default_renderer() is None:
        pytest.skip("Playwright (extra evidence-render) not installed")
    svc = new_run()
    r = svc.capture(SCRIPT_BUILT_PAGE, "live test: page whose figures are written by scripts")
    plain = svc.store.get_receipt(r["fallback_of"]) if r.get("fallback_of") else r
    write("browser_fallback", {"plain": summary(plain), "rendered": summary(r),
                               "blocked_requests": r.get("blocked_requests")})
    assert plain["status"] in ("short_text", "empty_text")
    assert r["kind"] == "rendered" and r["status"] == "ok", r
    assert r["text_chars"] > plain["text_chars"]


def test_live_as_of_capture() -> None:
    svc = new_run(as_of="2024-01-01")
    r = svc.capture(STATS_PAGE, "live test: release as of 2024-01-01")
    assert r["status"] == "ok", r
    assert r["archive"]["snapshot_timestamp"] < "20240101000000"
    write("as_of", summary(r))


STUDY_URLS = [
    "https://www.bls.gov/news.release/empsit.nr0.htm",
    "https://www.bls.gov/news.release/pdf/empsit.pdf",
    "https://www.ons.gov.uk/economy/inflationandpriceindices",
    "https://www.census.gov/",
    "https://www.abs.gov.au/",
    "https://www.federalreserve.gov/monetarypolicy/openmarket.htm",
    "https://www.ecb.europa.eu/stats/html/index.en.html",
    "https://www.who.int/",
    "https://en.wikipedia.org/wiki/Inflation",
    "https://www.apple.com/",
    "https://investor.apple.com/",
    "https://www.tesla.com/",
    "https://www.ft.com/",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K",
    "https://www.example.com/",
]
STUDY_DATES = ["2020-03-01", "2023-07-01"]


def test_live_archive_reliability() -> None:
    """How reliably the Internet Archive gives a copy dated before a date, and how
    often its availability API would have handed back a later copy."""
    fetcher = Fetcher(StdlibTransport())
    archive = Archive(fetcher)
    rows: list[dict[str, Any]] = []
    for as_of in STUDY_DATES:
        cutoff = as_of_cutoff(as_of)
        for url in STUDY_URLS:
            started = time.monotonic()
            outcome = archive.capture(url, as_of)
            row: dict[str, Any] = {
                "url": url, "as_of": as_of,
                "status": "ok" if outcome.status is None and outcome.fetch is not None
                and outcome.fetch.failure is None and outcome.fetch.http_status == 200
                else str(outcome.status or (outcome.fetch.failure if outcome.fetch else None)
                         or f"http_{outcome.fetch.http_status if outcome.fetch else None}"),
                "detail": outcome.detail or (outcome.fetch.detail if outcome.fetch else None),
                "snapshot": outcome.record.get("snapshot_timestamp"),
                "lookup_attempts": len(outcome.record.get("lookup_attempts", [])),
                "bytes": len(outcome.fetch.body or b"") if outcome.fetch else 0,
                "seconds": round(time.monotonic() - started, 1),
            }
            stamp = as_of.replace("-", "")
            avail = fetcher.fetch(f"https://archive.org/wayback/available?url={quote(url, safe='')}&timestamp={stamp}")
            closest = None
            if avail.body and avail.http_status == 200:
                try:
                    closest = json.loads(avail.body)["archived_snapshots"]["closest"]["timestamp"]
                except (KeyError, TypeError, ValueError):
                    closest = None
            row["availability_api_closest"] = closest
            row["availability_api_after_as_of"] = bool(
                closest and closest >= cutoff.strftime("%Y%m%d%H%M%S"))
            rows.append(row)
            print(row)
    ok = sum(1 for r in rows if r["status"] == "ok")
    write("archive_reliability", {
        "attempts": len(rows), "ok": ok,
        "availability_api_returned_later_copy": sum(r["availability_api_after_as_of"] for r in rows),
        "rows": rows,
    })
    assert rows
