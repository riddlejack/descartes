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
    assert r["status"] == "filed_after_as_of" and r["sha256"] is None
    shown = json.dumps(r) + json.dumps(svc.store.get_receipt(r["receipt_id"]))
    for secret in ("2026-10-26", "S-1", "inv-s1.htm", "Invented Co"):
        assert secret not in shown, secret
    assert r["sec"] == {"accession": LATER.accession}
    assert t.calls == []


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
