"""An invented run folder: one ferry operator, three captures, five claims.

Nothing here comes from real work; every name, figure and filing is made up.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

RUN_ID = "run-test-0001"
AS_OF = "2025-06-01"
CIK = 9999901


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_postgres: needs the local Postgres container")
    config.addinivalue_line("markers", "live_zetaris: needs a running Zetaris instance")


def _cid(n: int) -> str:
    return hashlib.sha256(f"invented-claim-{n}".encode()).hexdigest()


C_REVENUE, C_PASSENGERS, C_FARE, C_OLD, C_NEW = (_cid(i) for i in range(1, 6))


class FakeRegister:
    """Stands in for part B's register: `find_claims` and `get_claim` only."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = {r["claim_id"]: r for r in records}

    def find_claims(self, text: str | None = None, branch_id: str | None = None,
                    tier: str | None = None, entity: str | None = None,
                    include_superseded: bool = False) -> list[dict[str, Any]]:
        return [{"claim_id": cid} for cid in sorted(self.records)
                if include_superseded or not self.records[cid]["superseded_by"]]

    def get_claim(self, given: str) -> dict[str, Any]:
        return self.records[given]


def _claim(cid: str, statement: str, number: float | None, tier: str = "observed",
           support: list[dict[str, str]] | None = None, **extra: Any) -> dict[str, Any]:
    value = None if number is None else {"number": number, "unit": extra.pop("unit", "GBP"),
                                         "period": "FY2024", "kind": extra.pop("kind", "level"),
                                         "as_written": extra.pop("as_written", str(number))}
    rec = {"claim_id": cid, "statement": statement, "value": value, "tier": tier,
           "support": support or [], "derivation": extra.pop("derivation", None),
           "branch_id": "b-demand", "entities": ["Larkspur Ferries Ltd"],
           "author": "stemma:worker", "created_at": "2025-05-20T10:00:00.000Z",
           "supersedes": extra.pop("supersedes", None),
           "superseded_by": extra.pop("superseded_by", None),
           "verdict": extra.pop("verdict", None),
           "verdict_history": extra.pop("verdict_history", [])}
    assert not extra
    return rec


def claims() -> list[dict[str, Any]]:
    holds = {"verdict": "holds", "evidence": "quote found on page 3", "session": "s-1"}
    return [
        _claim(C_REVENUE, "Larkspur's FY2024 revenue was 48.7 million pounds.", 48_700_000.0,
               support=[{"receipt_id": "r-annual", "quote": "Revenue of £48.7m"}],
               as_written="48.7m", verdict=holds, verdict_history=[holds]),
        _claim(C_PASSENGERS, "Larkspur carried 1.92 million passengers in FY2024.", 1_920_000,
               support=[{"receipt_id": "r-annual", "quote": "1.92 million passengers"}],
               unit="passengers", kind="count", as_written="1.92 million"),
        _claim(C_FARE, "Average revenue per passenger was about 25.36 pounds.",
               48_700_000.0 / 1_920_000, tier="estimated",
               derivation={"script": "analysis/fare.py", "inputs": [C_REVENUE, C_PASSENGERS]}),
        _claim(C_OLD, "Larkspur ran 7 vessels.", 7, superseded_by=C_NEW, kind="count",
               unit="vessels", support=[{"receipt_id": "r-annual", "quote": "a fleet of 7"}],
               verdict={"verdict": "holds_with_correction", "evidence": "fleet note"},
               verdict_history=[{"verdict": "holds_with_correction",
                                 "evidence": "fleet note", "session": "s-2"}]),
        _claim(C_NEW, "Larkspur ran 8 vessels at year end.", 8, supersedes=C_OLD, kind="count",
               unit="vessels", support=[{"receipt_id": "r-annual", "quote": "8 vessels"}]),
    ]


SEC_JSON = {
    "cik": CIK, "taxonomy": "us-gaap", "tag": "Revenues", "entityName": "Larkspur Ferries Ltd",
    "units": {"USD": [
        {"start": "2023-01-01", "end": "2023-12-31", "val": 61000000, "accn": "0009999901-24-000003",
         "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-03-01", "frame": "CY2023"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 63500000.5,
         "accn": "0009999901-25-000002", "fy": 2024, "fp": "FY", "form": "10-K",
         "filed": "2025-02-28"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 63400000,
         "accn": "0009999901-25-000009", "fy": 2024, "fp": "FY", "form": "10-K/A",
         "filed": "2025-09-15"},
    ]},
}

TABLES_JSON = {"docling": "2.142.1", "tables": [
    {"index": 0, "page": 3, "markdown": "| Route | Passengers |\n|---|---|\n"
     "| North \\| South | 1,200,000 |\n| Harbour | 720,000 |"},
]}


def receipt(rid: str, **fields: Any) -> dict[str, Any]:
    base = {"receipt_id": rid, "created_at": "2025-05-19T09:00:00.000Z", "kind": "url",
            "reason": "test", "as_of": AS_OF, "status": "ok", "detail": None, "sha256": None,
            "bytes_len": None, "content_type": None, "text_chars": None,
            "served_from_cache": False, "original_fetched_at": None, "extractor": None,
            "http": None}
    base.update(fields)
    return base


def make_run(root: Path) -> Path:
    run = root / RUN_ID
    (run / "store" / "receipts").mkdir(parents=True)
    (run / "store" / "bytes").mkdir()
    (run / "store" / "derived").mkdir()
    (run / "run.json").write_text(json.dumps({"run_id": RUN_ID, "as_of": AS_OF}))
    sec_bytes = json.dumps(SEC_JSON).encode()
    sec_sha = hashlib.sha256(sec_bytes).hexdigest()
    (run / "store" / "bytes" / sec_sha).write_bytes(sec_bytes)
    annual_sha = hashlib.sha256(b"annual report").hexdigest()
    receipts = [
        receipt("r-annual", sha256=annual_sha, bytes_len=13, content_type="application/pdf",
                text_chars=9000, http={"requested_url": "https://larkspur.example/ar.pdf",
                                       "final_url": "https://larkspur.example/ar.pdf"},
                extractor={"name": "pypdfium2", "version": "5.14.0"}),
        receipt("r-sec", kind="sec_api", sha256=sec_sha, bytes_len=len(sec_bytes),
                content_type="application/json", requested_url="https://data.sec.example/x",
                sec={"cik": CIK, "company": "Larkspur Ferries Ltd", "concept": "us-gaap:Revenues"}),
        receipt("r-timeout", status="timeout", detail="no answer in 30 s",
                http={"requested_url": "https://slow.example/"}),
    ]
    for r in receipts:
        (run / "store" / "receipts" / f"{r['receipt_id']}.json").write_text(json.dumps(r))
    (run / "store" / "derived" / f"{annual_sha}.tables.json").write_text(json.dumps(TABLES_JSON))
    return run


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return make_run(tmp_path)


@pytest.fixture
def register() -> FakeRegister:
    return FakeRegister(claims())
