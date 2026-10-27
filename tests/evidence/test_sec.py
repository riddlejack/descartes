"""SEC tools over a fake backend and fake transport (invented company, invented data)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeSec, FakeTransport, Reply, html, set_as_of

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError
from stemma.evidence.sec import FilingRef, concept_url
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]


def filing(acc: str, form: str, filed: str) -> FilingRef:
    return FilingRef(acc, 999999, "Invented Co", form, filed, None, f"inv-{acc[-6:]}.htm")


FILINGS = [
    filing("0000999999-23-000010", "10-K", "2023-02-20"),
    filing("0000999999-23-000020", "10-Q", "2023-05-05"),
    filing("0000999999-24-000010", "10-K", "2024-02-21"),
    filing("0000999999-25-000010", "10-K", "2025-02-19"),
]

CONCEPT = {
    "cik": 999999, "taxonomy": "us-gaap", "tag": "Revenues",
    "units": {"USD": [
        # FY2022 revenue as first filed, then restated in the next 10-K
        {"start": "2022-01-01", "end": "2022-12-31", "val": 8200, "accn": "0000999999-23-000010",
         "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2023-02-20", "frame": "CY2022"},
        {"start": "2022-01-01", "end": "2022-12-31", "val": 8169, "accn": "0000999999-24-000010",
         "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-02-21"},
        {"start": "2023-01-01", "end": "2023-03-31", "val": 2100, "accn": "0000999999-23-000020",
         "fy": 2023, "fp": "Q1", "form": "10-Q", "filed": "2023-05-05"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 9000, "accn": "0000999999-24-000010",
         "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-02-21", "frame": "CY2023"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 9900, "accn": "0000999999-25-000010",
         "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-19", "frame": "CY2024"},
    ]},
}


def routes() -> dict[str, object]:
    out: dict[str, object] = {
        concept_url(999999, "us-gaap", "Revenues"): Reply(
            200, [("Content-Type", "application/json")], json.dumps(CONCEPT).encode()),
    }
    for f in FILINGS:
        out[f.document_url()] = Reply(200, [("Content-Type", "text/html")], html(
            f"<h1>Invented Co {f.form}</h1>" + f"<p>Filed {f.filing_date}. Revenue table.</p>" * 10))
    return out


@pytest.fixture
def sec_svc(make_service: Factory) -> tuple[EvidenceService, FakeTransport]:
    t = FakeTransport(routes())
    return make_service(t, sec_backend=FakeSec(FILINGS)), t


def test_sec_filings_strict_bounds_newest_first(sec_svc: tuple[EvidenceService, FakeTransport]) -> None:
    svc, _ = sec_svc
    out = svc.sec_filings("INVT", form="10-K", filed_before="2025-02-19")
    assert [f["filing_date"] for f in out["filings"]] == ["2024-02-21", "2023-02-20"]
    out = svc.sec_filings("INVT", filed_after="2023-02-20", filed_before="2024-12-31")
    assert [f["accession"] for f in out["filings"]] == ["0000999999-24-000010", "0000999999-23-000020"]


def test_sec_filings_default_to_as_of(sec_svc: tuple[EvidenceService, FakeTransport],
                                      run_dir: Path) -> None:
    svc, _ = sec_svc
    set_as_of(run_dir, "2024-01-01")
    out = svc.sec_filings("INVT")
    assert out["filed_before"] == "2024-01-01"
    assert max(f["filing_date"] for f in out["filings"]) < "2024-01-01"
    later = svc.sec_filings("INVT", filed_before="2030-01-01")  # cannot see past as_of
    assert later["filed_before"] == "2024-01-01" and later["warnings"]


def test_sec_capture_receipt(sec_svc: tuple[EvidenceService, FakeTransport]) -> None:
    svc, t = sec_svc
    svc.sec_filings("INVT")
    r = svc.sec_capture("0000999999-24-000010")
    assert r["status"] == "ok" and r["kind"] == "sec"
    assert r["sec"]["filing_date"] == "2024-02-21" and r["sec"]["form"] == "10-K"
    assert r["requested_url"].endswith("/999999/000099999924000010/inv-000010.htm")
    assert t.calls[-1][2]["User-Agent"] == "Test Builder test@example.org"


def test_sec_capture_refuses_filed_after_as_of(sec_svc: tuple[EvidenceService, FakeTransport],
                                               run_dir: Path) -> None:
    svc, t = sec_svc
    set_as_of(run_dir, "2024-02-21")  # filed on the as_of date is not before it
    r = svc.sec_capture("0000999999-24-000010")
    assert r["status"] == "no_filing_before_as_of" and r["sha256"] is None
    assert t.calls == []


def test_sec_capture_rejects_bad_input(sec_svc: tuple[EvidenceService, FakeTransport]) -> None:
    svc, _ = sec_svc
    for args, code in ((("../../x",), "bad_accession"),
                       (("0000999999-24-000010", "../../../etc/passwd"), "bad_document"),
                       (("0000999999-99-000099",), "sec_filing_not_found")):
        with pytest.raises(EvidenceError) as info:
            svc.sec_capture(*args)
        assert info.value.code == code


def test_sec_facts_as_filed_never_collapsed(sec_svc: tuple[EvidenceService, FakeTransport]) -> None:
    svc, _ = sec_svc
    out = svc.sec_facts("INVT", "us-gaap:Revenues")
    fy2022 = [f for f in out["facts"] if f["end"] == "2022-12-31"]
    assert sorted(f["value"] for f in fy2022) == [8169, 8200]  # both versions kept
    for fact in out["facts"]:
        assert fact["filed"] and fact["accession"] and fact["filing_receipt_id"]
        receipt = svc.store.get_receipt(fact["filing_receipt_id"])
        assert receipt["sec"]["accession"] == fact["accession"]
    assert svc.store.get_receipt(out["source_receipt_id"])["status"] == "ok"


def test_sec_facts_filed_before_hides_restatement(
        sec_svc: tuple[EvidenceService, FakeTransport]) -> None:
    svc, _ = sec_svc
    out = svc.sec_facts("INVT", "Revenues", filed_before="2024-01-01")
    assert {f["value"] for f in out["facts"] if f["end"] == "2022-12-31"} == {8200}
    assert all(f["filed"] < "2024-01-01" for f in out["facts"])
    only_k = svc.sec_facts("INVT", "Revenues", form="10-K")
    assert {f["form"] for f in only_k["facts"]} == {"10-K"}


def test_sec_facts_as_of_withholds_raw_text(sec_svc: tuple[EvidenceService, FakeTransport],
                                            run_dir: Path) -> None:
    svc, _ = sec_svc
    set_as_of(run_dir, "2024-01-01")
    out = svc.sec_facts("INVT", "Revenues")
    source = svc.store.get_receipt(out["source_receipt_id"])
    # the answer is not stored (it holds later figures), so they are never searchable
    assert source["status"] == "text_withheld_as_of" and source["text_path"] is None
    assert svc.search_store("8169", mode="exact")["hits"] == []
    assert all(f["filed"] < "2024-01-01" for f in out["facts"])


def test_sec_facts_filing_limit_warned(sec_svc: tuple[EvidenceService, FakeTransport],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = sec_svc
    monkeypatch.setattr(limits, "SEC_FACTS_MAX_FILINGS", 1)
    out = svc.sec_facts("INVT", "Revenues")
    captured = [a for a, r in out["filings"].items() if r["receipt_id"]]
    assert captured == ["0000999999-25-000010"]  # newest first
    assert out["warnings"] and any(f["filing_receipt_id"] is None for f in out["facts"])


def test_sec_bad_concept_and_company(sec_svc: tuple[EvidenceService, FakeTransport]) -> None:
    svc, _ = sec_svc
    with pytest.raises(EvidenceError) as info:
        svc.sec_facts("INVT", "us-gaap:../../x")
    assert info.value.code == "bad_concept"
    with pytest.raises(EvidenceError) as info:
        svc.sec_filings("NOPE")
    assert info.value.code == "sec_company_not_found"


def test_sec_needs_identity_and_never_falls_back(make_service: Factory, run_dir: Path) -> None:
    t = FakeTransport(routes())
    svc = make_service(t, sec_identity=None, sec_backend=FakeSec(FILINGS))
    calls = (lambda: svc.sec_filings("INVT"),
             lambda: svc.sec_capture("0000999999-24-000010"),
             lambda: svc.sec_facts("INVT", "Revenues"))
    for call in calls:
        with pytest.raises(EvidenceError) as info:
            call()
        assert info.value.code == "sec_identity_not_configured"
    assert t.calls == []  # nothing reached SEC under a default identity


def test_receipt_records_identity_sent_not_value(
        sec_svc: tuple[EvidenceService, FakeTransport], run_dir: Path) -> None:
    svc, _ = sec_svc
    svc.sec_filings("INVT")
    r = svc.sec_capture("0000999999-24-000010")
    assert r["http"]["sec_identity_header_sent"] is True
    for path in (run_dir / "store").rglob("*"):
        if path.is_file():
            assert b"test@example.org" not in path.read_bytes(), path
