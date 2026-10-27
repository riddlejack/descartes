"""Hostile probes of the broker's paths in an as_of run (CONTRACTS section 0d): no path
returns anything dated on or after as_of, not even in a refusal."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from conftest import FakeSec, FakeTransport, Reply, set_as_of

from stemma.evidence.archive import cdx_query_url
from stemma.evidence.sec import FilingRef
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
AS_OF = "2026-10-19"
LATER = FilingRef("0000999999-26-000077", 999999, "Invented Co", "S-1", "2026-10-26", None,
                  "inv-s1.htm")
EARLIER = FilingRef("0000999999-26-000011", 999999, "Invented Co", "10-Q", "2026-08-01",
                    None, "inv-q.htm")


def test_a_refused_later_filing_says_nothing_about_it(make_service: Factory,
                                                      run_dir: Path) -> None:
    # Guessing an accession must not tell the agent that a later filing exists, its
    # form, its company or its date.
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({})
    svc = make_service(t, sec_backend=FakeSec([EARLIER, LATER]))
    r = svc.sec_capture(LATER.accession)
    assert r["status"] == "no_filing_before_as_of" and r["sha256"] is None
    shown = json.dumps(r) + json.dumps(svc.store.get_receipt(r["receipt_id"]))
    for secret in ("2026-10-26", "S-1", "inv-s1.htm", "Invented Co"):
        assert secret not in shown, secret
    assert r["sec"] == {"accession": LATER.accession}
    assert t.calls == []
    assert not (run_dir / "store/derived/sec-filings").exists() or not list(
        (run_dir / "store/derived/sec-filings").iterdir())


def test_a_refused_later_filing_in_a_live_run_is_not_withheld(make_service: Factory,
                                                              run_dir: Path) -> None:
    svc = make_service(FakeTransport({}), sec_backend=FakeSec([EARLIER, LATER]))
    listed = svc.sec_filings("999999")
    assert [f["form"] for f in listed["filings"]] == ["S-1", "10-Q"]  # no as_of: all


def test_the_filing_list_never_shows_a_later_filing(make_service: Factory,
                                                    run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    svc = make_service(FakeTransport({}), sec_backend=FakeSec([EARLIER, LATER]))
    for before in (None, "2026-12-31", "2099-01-01"):
        out = svc.sec_filings("999999", filed_before=before)
        assert [f["accession"] for f in out["filings"]] == [EARLIER.accession], before


def test_an_archive_url_dated_later_is_never_fetched(make_service: Factory,
                                                     run_dir: Path) -> None:
    # Hostile: the agent passes a Wayback URL of a capture after as_of. The service
    # looks that URL up in the archive before as_of and never fetches the later copy.
    set_as_of(run_dir, AS_OF)
    later = "https://web.archive.org/web/20261027000000/https://news.example.org/s1"
    t = FakeTransport({cdx_query_url(later, AS_OF): Reply(200, [], b"[]")})
    svc = make_service(t)
    svc.archive.sleep = lambda _s: None
    r = svc.capture(later, "probe")
    assert r["status"] == "no_snapshot_before_as_of" and r["sha256"] is None
    assert [c[0] for c in t.calls] == [cdx_query_url(later, AS_OF)]


def test_xbrl_facts_are_filtered_and_the_raw_answer_is_not_stored(
        make_service: Factory, run_dir: Path) -> None:
    # Item 5 of the orchestrator's 27 October brief. Facts filed on or after as_of are
    # dropped; and the raw company-concept answer, which holds them, is not stored,
    # because a worker may Read store/bytes/<sha256> (Codex-gate probe, 27 October).
    from test_sec import CONCEPT, FILINGS, routes

    set_as_of(run_dir, "2024-01-01")
    svc = make_service(FakeTransport(routes()), sec_backend=FakeSec(FILINGS))
    out = svc.sec_facts("INVT", "Revenues")
    assert out["facts"] and all(f["filed"] < "2024-01-01" for f in out["facts"])
    for later in (f for f in CONCEPT["units"]["USD"] if f["filed"] >= "2024-01-01"):
        assert later["accn"] not in json.dumps(out)
    source = svc.store.get_receipt(out["source_receipt_id"])
    assert source["status"] == "text_withheld_as_of" and source["sha256"] is None
    stored = b"".join(p.read_bytes() for p in (run_dir / "store").rglob("bytes/*"))
    assert b"0000999999-24-000010" not in stored and b"8169" not in stored
