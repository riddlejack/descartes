"""The Codex review of the as-of branch (reviews/W2-asof-review.md, 27 October): five
paths to content dated on or after as_of, each probed here as found. Invented filer,
pages and dates; nothing touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeSec, FakeTransport, Reply, html, set_as_of

from stemma.evidence.archive import cdx_query_url
from stemma.evidence.errors import EvidenceError
from stemma.evidence.sec import FilingRef
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
AS_OF = "2026-10-19"
PAGE = "https://news.example.org/invented-co"
LATER_NEWS = b"<html><body><a href='/x'>October 26: Invented Co acquired</a></body></html>"
EARLIER = FilingRef("0000999999-26-000011", 999999, "Invented Co", "10-Q", "2026-08-01",
                    None, "inv-q.htm")
LATER = FilingRef("0000999999-26-000077", 999999, "Invented Co", "S-1", "2026-10-26", None,
                  "inv-s1.htm")


def _stored(run_dir: Path) -> bytes:
    return b"".join(p.read_bytes() for p in (run_dir / "store").rglob("*") if p.is_file())


def _cdx(url: str, stamp: str) -> Reply:
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"],
            [stamp, url, "200", "text/html", "D"]]
    return Reply(200, [("Content-Type", "application/json")], json.dumps(rows).encode())


# --- 1 (P1): no bytes holding later content are kept where a session can read them --

def test_an_archive_error_page_is_not_kept(make_service: Factory, run_dir: Path) -> None:
    # The replay fails with today's error page; in an as_of run its body is not stored.
    set_as_of(run_dir, AS_OF)
    snap = f"https://web.archive.org/web/20261001000000id_/{PAGE}"
    t = FakeTransport({cdx_query_url(PAGE, AS_OF): _cdx(PAGE, "20261001000000"),
                       snap: Reply(503, [("Content-Type", "text/html")], LATER_NEWS)})
    svc = make_service(t)
    svc.archive.sleep = lambda _s: None
    r = svc.capture(PAGE, "probe")
    assert r["status"] == "archive_unavailable" and r["sha256"] is None
    assert b"acquired" not in _stored(run_dir)


def test_an_sec_error_page_is_not_kept(make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({EARLIER.document_url(): Reply(404, [("Content-Type", "text/html")],
                                                     LATER_NEWS)})
    r = make_service(t, sec_backend=FakeSec([EARLIER])).sec_capture(EARLIER.accession)
    assert r["sha256"] is None and b"acquired" not in _stored(run_dir)


# --- 2 (P1): a guessed later filing leaves no trace and one refusal ---------------

def test_a_later_filing_is_not_remembered_and_refuses_as_an_absent_one(
        make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({})
    svc = make_service(t, sec_backend=FakeSec([EARLIER, LATER]))
    later = svc.sec_capture(LATER.accession)
    absent = svc.sec_capture("0000999999-26-000099")
    assert later["sha256"] is None and absent["sha256"] is None
    same = ("status", "detail", "sha256", "text_path", "warnings")
    assert {k: later[k] for k in same} == {k: absent[k] for k in same}
    assert later["status"] == absent["status"] == "no_filing_before_as_of"
    assert later["sec"] == {"accession": LATER.accession}
    assert absent["sec"] == {"accession": "0000999999-26-000099"}
    stored = _stored(run_dir)
    for trace in (b"2026-10-26", b"S-1", b"inv-s1.htm"):
        assert trace not in stored, trace
    assert t.calls == []


def test_a_live_run_still_says_not_found(make_service: Factory, run_dir: Path) -> None:
    svc = make_service(FakeTransport({}), sec_backend=FakeSec([EARLIER]))
    with pytest.raises(EvidenceError) as e:
        svc.sec_capture("0000999999-26-000099")
    assert e.value.code == "sec_filing_not_found"


# --- 3 (P1): an SEC redirect stays inside the approved filing ----------------------

@pytest.mark.parametrize("target", [
    LATER.document_url(),  # another (later) filing on the same host
    "https://news.example.org/october-26",  # another site
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent",
])
def test_an_sec_redirect_out_of_the_filing_is_refused(make_service: Factory, run_dir: Path,
                                                       target: str) -> None:
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({EARLIER.document_url(): Reply(301, [("Location", target)]),
                       target: Reply(200, [("Content-Type", "text/html")], LATER_NEWS)})
    r = make_service(t, sec_backend=FakeSec([EARLIER, LATER])).sec_capture(EARLIER.accession)
    assert r["sha256"] is None and r["status"] != "ok"
    assert all(c[0] != target for c in t.calls)


def test_an_sec_redirect_inside_the_filing_is_followed(make_service: Factory,
                                                       run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    inside = EARLIER.document_url("inv-q-v2.htm")
    page = html("<h1>Invented Co 10-Q</h1>" + "<p>Revenue table.</p>" * 20)
    t = FakeTransport({EARLIER.document_url(): Reply(301, [("Location", inside)]),
                       inside: Reply(200, [("Content-Type", "text/html")], page)})
    r = make_service(t, sec_backend=FakeSec([EARLIER])).sec_capture(EARLIER.accession)
    assert r["status"] == "ok"


# --- 4 (P2): archived_links reads only a successful capture dated before as_of -----

def _plant(svc: EvidenceService, kind: str, status: str, **extra: Any) -> str:
    receipt = svc._base_receipt(kind, "planted")
    sha = svc.store.put_bytes(LATER_NEWS)
    receipt.update({"status": status, "sha256": sha, "content_type": "text/html",
                    "final_url": PAGE, **extra})
    svc.store.put_receipt(receipt)
    svc.store.index_document(sha, LATER_NEWS.decode(), "text/html", receipt["receipt_id"])
    return sha


@pytest.mark.parametrize("kind,status,extra", [
    ("archive", "archive_unavailable", {}),
    ("archive", "ok", {}),  # no capture timestamp
    ("archive", "ok", {"archive": {"snapshot_timestamp": "2026"}}),  # not a full stamp
    ("archive", "ok", {"archive": {"snapshot_timestamp": "20261026000000"}}),
    ("sec", "ok", {"sec": {"accession": LATER.accession, "filing_date": "2026-10-26"}}),
    ("sec", "ok", {"sec": {"accession": LATER.accession}}),  # no filing date
    ("file", "ok", {}),
])
def test_archived_links_refuses_what_is_not_a_dated_capture(
        make_service: Factory, run_dir: Path, kind: str, status: str,
        extra: dict[str, Any]) -> None:
    set_as_of(run_dir, AS_OF)
    svc = make_service(FakeTransport({}))
    sha = _plant(svc, kind, status, **extra)
    with pytest.raises(EvidenceError) as e:
        svc.archived_links(sha)
    assert e.value.code == "not_a_dated_capture"


def test_archived_links_reads_a_dated_capture(make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    svc = make_service(FakeTransport({}))
    sha = _plant(svc, "archive", "ok",
                 archive={"snapshot_timestamp": "20261018120000"})
    assert svc.archived_links(sha)["links"][0]["url"] == "https://news.example.org/x"


# --- 5 (P2): a run.json that cannot say the date makes the broker refuse -----------

@pytest.mark.parametrize("content", [None, "", "{not json", "[]", '{"as_of": ""}', '{"as_of": "2026-13-40"}',
                                     '{"as_of": 20261019}'])
def test_a_run_json_that_cannot_say_the_date_refuses(make_service: Factory, run_dir: Path,
                                                    content: str | None) -> None:
    path = run_dir / "run.json"
    t = FakeTransport({PAGE: Reply(200, [("Content-Type", "text/html")], LATER_NEWS)})
    svc = make_service(t, sec_backend=FakeSec([EARLIER]))
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    for call in (lambda: svc.capture(PAGE, "probe"), lambda: svc.sec_search("Invented"),
                 lambda: svc.market_series("fred", "DGS10", "probe"),
                 lambda: svc.sec_filings("999999")):
        with pytest.raises(EvidenceError) as e:
            call()
        assert e.value.code == "bad_run_json"
    assert t.calls == []
