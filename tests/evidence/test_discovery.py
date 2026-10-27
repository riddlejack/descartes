"""Discovery in an as_of run (CONTRACTS section 0d): browsing an archived page's links,
and EDGAR full-text search bounded by the host. Refusals, and hostile probes that try
to see past as_of. Invented sites and filers; nothing here touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FakeTransport, Reply, html, set_as_of

from stemma.evidence import discovery
from stemma.evidence.archive import cdx_query_url
from stemma.evidence.discovery import edgar_search_url
from stemma.evidence.errors import EvidenceError
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
AS_OF = "2026-09-30"
PAGE = "https://investors.example.org/news/"
STAMP = "20260925120000"
LATER = "https://web.archive.org/web/20261020000000/https://news.example.org/leaked-figures"
BODY = html(
    "<h1>Press releases</h1>"
    "<a href='2026/q2-results'>Second quarter results</a>"
    "<a href='/about#team'>About us</a> <a href='/about'>About</a>"
    "<a href='https://other.example.org/x?y=1'>Elsewhere</a>"
    f"<a href='{LATER}'>A link into the archive</a>"
    "<a href='mailto:ir@example.org'>Mail</a><a href='javascript:void(0)'>Menu</a>"
    "<p>" + "Words to pass the short-text check. " * 20 + "</p>")


def _cdx(url: str, *stamps: str) -> Reply:
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"]]
    rows += [[s, url, "200", "text/html", "D"] for s in stamps]
    return Reply(200, [("Content-Type", "application/json")], json.dumps(rows).encode())


def _replay(stamp: str, url: str, body: bytes, memento: str) -> dict[str, Reply]:
    return {f"https://web.archive.org/web/{stamp}id_/{url}": Reply(
        200, [("Content-Type", "text/html; charset=utf-8"), ("Memento-Datetime", memento)],
        body)}


def _archived_page(make_service: Factory, run_dir: Path, body: bytes = BODY) -> tuple[
        EvidenceService, str]:
    set_as_of(run_dir, AS_OF)
    routes: dict[str, Any] = {cdx_query_url(PAGE, AS_OF): _cdx(PAGE, STAMP),
                              **_replay(STAMP, PAGE, body, "Fri, 25 Sep 2026 12:00:00 GMT")}
    svc = make_service(FakeTransport(routes))
    svc.archive.sleep = lambda _s: None
    r = svc.capture(PAGE, "the issuer's press page as of the date")
    assert r["status"] == "ok", r
    return svc, str(r["sha256"])


# --- archived_links -----------------------------------------------------------------

def test_links_are_absolute_against_the_page_not_the_archive(make_service: Factory,
                                                            run_dir: Path) -> None:
    svc, sha = _archived_page(make_service, run_dir)
    out = svc.archived_links(sha)
    assert out["base_url"] == PAGE and out["snapshot_timestamp"] == STAMP
    assert out["kind"] == "archive"
    urls = [link["url"] for link in out["links"]]
    assert urls == ["https://investors.example.org/news/2026/q2-results",
                    "https://investors.example.org/about",
                    "https://other.example.org/x?y=1",
                    "https://news.example.org/leaked-figures"]
    assert out["links"][1]["text"] == "About us"  # first text kept; fragment dropped
    assert out["not_web_links"] == 2 and out["truncated"] is False


def test_a_link_into_the_archive_dated_later_is_never_followed(make_service: Factory,
                                                              run_dir: Path) -> None:
    # Hostile: an archived page links to a Wayback copy made after as_of. The link is
    # given as the page it copies, and capturing that page looks it up before as_of:
    # the later copy is never fetched.
    svc, sha = _archived_page(make_service, run_dir)
    link = svc.archived_links(sha)["links"][-1]["url"]
    assert link == "https://news.example.org/leaked-figures"
    t = svc.fetcher.transport
    assert isinstance(t, FakeTransport)
    t.routes[cdx_query_url(link, AS_OF)] = Reply(200, [], b"[]")
    before = len(t.calls)
    r = svc.capture(link, "follow the link")
    assert r["status"] == "no_snapshot_before_as_of"
    assert [c[0] for c in t.calls[before:]] == [cdx_query_url(link, AS_OF)]
    assert not any("20261020" in c[0] for c in t.calls)


def test_a_base_href_is_followed_and_unwrapped(make_service: Factory, run_dir: Path) -> None:
    body = html("<base href='https://web.archive.org/web/20260101000000/https://cdn.example.org/'>"
                "<a href='doc.html'>Doc</a>" + "<p>Enough words here to pass. </p>" * 20)
    svc, sha = _archived_page(make_service, run_dir, body)
    assert [x["url"] for x in svc.archived_links(sha)["links"]] == [
        "https://cdn.example.org/doc.html"]


def test_links_are_capped(make_service: Factory, run_dir: Path,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "LINKS_MAX", 2)
    svc, sha = _archived_page(make_service, run_dir)
    out = svc.archived_links(sha)
    assert len(out["links"]) == 2 and out["truncated"] is True


def test_archived_links_refusals(make_service: Factory, run_dir: Path) -> None:
    svc = make_service(FakeTransport({}))
    with pytest.raises(EvidenceError) as e:
        svc.archived_links("0" * 64)
    assert e.value.code == "unknown_sha256"
    with pytest.raises(EvidenceError) as e:
        svc.archived_links("../etc/passwd")
    assert e.value.code in ("bad_sha256", "bad_text_id")


def test_a_page_that_is_not_html_has_no_links(make_service: Factory, run_dir: Path) -> None:
    csv = "https://data.example.org/table.csv"
    set_as_of(run_dir, AS_OF)
    routes: dict[str, Any] = {cdx_query_url(csv, AS_OF): _cdx(csv, STAMP),
                              f"https://web.archive.org/web/{STAMP}id_/{csv}": Reply(
                                  200, [("Content-Type", "text/csv"),
                                        ("Memento-Datetime", "Fri, 25 Sep 2026 12:00:00 GMT")],
                                  b"a,b\n1,2\n")}
    svc = make_service(FakeTransport(routes))
    svc.archive.sleep = lambda _s: None
    r = svc.capture(csv, "a table")
    assert r["sha256"]
    with pytest.raises(EvidenceError) as e:
        svc.archived_links(r["sha256"])
    assert e.value.code == "not_html"


def test_a_page_captured_live_is_refused_once_the_run_is_as_of(make_service: Factory,
                                                               run_dir: Path) -> None:
    # Hostile: a live capture (made before the run had as_of) cannot seed browsing.
    t = FakeTransport({PAGE: Reply(200, [("Content-Type", "text/html")], BODY)})
    svc = make_service(t)
    sha = svc.capture(PAGE, "live")["sha256"]
    set_as_of(run_dir, AS_OF)
    with pytest.raises(EvidenceError) as e:
        svc.archived_links(sha)
    assert e.value.code == "captured_live"


# --- sec_search ---------------------------------------------------------------------

def _hit(acc: str, form: str, filed: str, doc: str = "d1.htm") -> dict[str, Any]:
    return {"_id": f"{acc}:{doc}", "_source": {
        "adsh": acc, "form": form, "file_date": filed, "ciks": ["0000999999"],
        "display_names": ["Invented Co  (INVT)  (CIK 0000999999)"]},
        "highlight": {"text": ["revenue was <b>large</b>"]}}


def _answer(*hits: dict[str, Any], total: int = 0) -> Reply:
    data = {"hits": {"total": {"value": total or len(hits)}, "hits": list(hits)}}
    return Reply(200, [("Content-Type", "application/json")], json.dumps(data).encode())


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def test_the_end_date_is_the_day_before_as_of(make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    url = edgar_search_url('"Invented Co" revenue', AS_OF, ["S-1", "10-K"], "999999", 2)
    q = _params(url)
    assert q == {"q": ['"Invented Co" revenue'], "dateRange": ["custom"],
                 "startdt": ["2001-01-01"], "enddt": ["2026-09-29"], "forms": ["S-1,10-K"],
                 "ciks": ["0000999999"], "from": ["100"]}
    live = _params(edgar_search_url("x", None, None, None, 1))
    assert live == {"q": ["x"]}


@pytest.mark.parametrize("query", [
    "x&enddt=2030-01-01", "x&dateRange=all", "x\n&enddt=2030-01-01", "x#&enddt=2030",
])
def test_no_query_moves_the_end_date(query: str) -> None:
    # Hostile: parameters smuggled into the query stay inside q.
    q = _params(edgar_search_url(query, AS_OF, None, None, 1))
    assert q["enddt"] == ["2026-09-29"] and q["dateRange"] == ["custom"]
    assert q["q"] == [query.strip()]


def test_a_later_hit_is_withheld_without_a_trace(make_service: Factory,
                                                run_dir: Path) -> None:
    # Hostile: the index ignores the end date and answers with a later S-1. It is
    # withheld, and nothing (count, total, highlight) says it existed.
    set_as_of(run_dir, AS_OF)
    url = edgar_search_url("Invented Co", AS_OF, None, None, 1)
    t = FakeTransport({url: _answer(_hit("0000999999-26-000011", "10-Q", "2026-08-01"),
                                    _hit("0000999999-26-000077", "S-1", "2026-10-26"),
                                    _hit("0000999999-26-000078", "8-K", "2026-09-30"),
                                    _hit("0000999999-26-000079", "8-K", "late"),
                                    total=4)})
    svc = make_service(t)
    out = svc.sec_search("Invented Co")
    assert [h["accession"] for h in out["hits"]] == ["0000999999-26-000011"]
    assert out["hits"][0] == {"accession": "0000999999-26-000011", "form": "10-Q",
                              "filing_date": "2026-08-01",
                              "filers": ["Invented Co  (INVT)  (CIK 0000999999)"],
                              "ciks": ["0000999999"], "document": "d1.htm"}
    shown = json.dumps(out)
    for trace in ("2026-10-26", "S-1", "000077", "000078", "000079", "total", "large"):
        assert trace not in shown, trace
    assert out["filed_before"] == AS_OF


def test_a_live_run_searches_without_a_bound(make_service: Factory, run_dir: Path) -> None:
    url = edgar_search_url("Invented Co", None, None, None, 1)
    svc = make_service(FakeTransport({url: _answer(
        _hit("0000999999-26-000077", "S-1", "2026-10-26"))}))
    out = svc.sec_search("Invented Co")
    assert [h["form"] for h in out["hits"]] == ["S-1"] and out["warnings"] == []


@pytest.mark.parametrize("args,code", [
    (("",), "bad_query"), (("x" * 501,), "bad_query"),
    (("x", ["S-1; DROP"]), "bad_form"), (("x", "S-1"), "bad_form"),
    (("x", None, "12a"), "bad_cik"), (("x", None, None, 0), "bad_page"),
    (("x", None, None, 21), "bad_page"), (("x", None, None, True), "bad_page"),
])
def test_sec_search_refuses_bad_arguments(make_service: Factory, run_dir: Path,
                                          args: tuple[Any, ...], code: str) -> None:
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({})
    with pytest.raises(EvidenceError) as e:
        make_service(t).sec_search(*args)
    assert e.value.code == code and t.calls == []


def test_sec_search_failures_are_typed(make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    url = edgar_search_url("x", AS_OF, None, None, 1)
    for reply, code in ((Reply(500, [], b"oops"), "search_unavailable"),
                        (Reply(200, [], b"<html>maintenance</html>"), "search_unreadable"),
                        (Reply(200, [], b'{"hits": {"hits": 3}}'), "search_unreadable"),
                        (Reply(302, [("Location", "https://evil.example.org/")]),
                         "search_unavailable")):
        with pytest.raises(EvidenceError) as e:
            make_service(FakeTransport({url: reply})).sec_search("x")
        assert e.value.code == code, reply
    with pytest.raises(EvidenceError) as e:
        make_service(FakeTransport({}), sec_identity=None).sec_search("x")
    assert e.value.code == "sec_identity_not_configured"
