"""Market series in an as_of run (CONTRACTS sections 0d and 4): the host sets the end
date, and a series with any row dated on or after as_of is refused with nothing kept.
Invented series values; nothing here touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FakeTransport, Reply, set_as_of

from stemma.evidence.cache import RequestCache, request_key
from stemma.evidence.discovery import series_url
from stemma.evidence.errors import EvidenceError
from stemma.evidence.extract import extractor_versions
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
AS_OF = "2026-10-01"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&coed=2026-09-30"
STOOQ = "https://stooq.com/q/d/l/?s=crwv.us&i=d&d1=19000101&d2=20260930"
FRED_CSV = b"observation_date,DGS10\n2026-09-28,4.01\n2026-09-29,.\n2026-09-30,3.98\n"
STOOQ_CSV = (b"Date,Open,High,Low,Close,Volume\n2026-09-29,101.5,104,99.2,103.1,1000\n"
             b"2026-09-30,103.0,105.5,102.0,104.9,1200\n")
LATER_CSV = FRED_CSV + b"2026-10-02,4.20\n"


def _csv(body: bytes) -> Reply:
    return Reply(200, [("Content-Type", "text/csv"),
                       ("Last-Modified", "Fri, 16 Oct 2026 07:00:00 GMT")], body)


def test_the_host_sets_the_end_date() -> None:
    q = parse_qs(urlsplit(series_url("fred", "DGS10", AS_OF)).query)
    assert q == {"id": ["DGS10"], "coed": ["2026-09-30"]}
    q = parse_qs(urlsplit(series_url("stooq", "crwv.us", AS_OF)).query)
    assert q == {"s": ["crwv.us"], "i": ["d"], "d1": ["19000101"], "d2": ["20260930"]}
    assert series_url("stooq", "^spx", AS_OF).startswith("https://stooq.com/q/d/l/?s=%5Espx&")
    assert "coed" not in series_url("fred", "DGS10", None)  # a run without as_of
    assert "d2" not in series_url("stooq", "crwv.us", None)


@pytest.mark.parametrize("source,symbol,code", [
    ("fred", "DGS10&coed=2030-01-01", "bad_symbol"), ("fred", "dgs10", "bad_symbol"),
    ("fred", "", "bad_symbol"), ("stooq", "crwv.us&d2=20301231", "bad_symbol"),
    ("stooq", "CRWV.US", "bad_symbol"), ("stooq", "a b", "bad_symbol"),
    ("yahoo", "CRWV", "bad_source"), ("fred", 7, "bad_symbol"),
])
def test_no_symbol_moves_the_end_date(make_service: Factory, run_dir: Path, source: str,
                                      symbol: Any, code: str) -> None:
    # Hostile: a symbol carrying its own date parameter is refused before any fetch.
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({})
    with pytest.raises(EvidenceError) as e:
        make_service(t).market_series(source, symbol, "rates")
    assert e.value.code == code and t.calls == []


@pytest.mark.parametrize("source,symbol,url,body,last", [
    ("fred", "DGS10", FRED, FRED_CSV, "2026-09-30"),
    ("stooq", "crwv.us", STOOQ, STOOQ_CSV, "2026-09-30"),
])
def test_a_series_ending_before_as_of_is_captured(make_service: Factory, run_dir: Path,
                                                 source: str, symbol: str, url: str,
                                                 body: bytes, last: str) -> None:
    set_as_of(run_dir, AS_OF)
    svc = make_service(FakeTransport({url: _csv(body)}))
    r = svc.market_series(source, symbol, "the series to the end of September")
    assert r["status"] == "ok" and r["kind"] == "series" and r["sha256"]
    assert r["series"]["last_date"] == last and r["series"]["ends_before"] == AS_OF
    assert svc.store.get_text(r["sha256"]).startswith(body.decode()[:20])
    # The live response's headers that could postdate as_of are not kept.
    assert [h[0].lower() for h in r["http"]["headers"]] == ["content-type"]
    assert "2026-10-16" not in json.dumps(r) and "16 Oct" not in json.dumps(r)


def test_a_later_row_refuses_the_whole_series_and_keeps_nothing(
        make_service: Factory, run_dir: Path, tmp_path: Path) -> None:
    # Hostile: the source ignores the end date. Nothing is stored or cached, and the
    # refusal does not say the later date.
    set_as_of(run_dir, AS_OF)
    svc = make_service(FakeTransport({FRED: _csv(LATER_CSV)}))
    r = svc.market_series("fred", "DGS10", "rates")
    assert r["status"] == "series_past_as_of" and r["sha256"] is None
    shown = json.dumps(r) + json.dumps(svc.store.get_receipt(r["receipt_id"]))
    assert "2026-10-02" not in shown and "4.20" not in shown
    assert not list((run_dir / "store").rglob("bytes/*"))  # nothing stored
    assert svc.cache is not None
    key = request_key("series", FRED, AS_OF, extractor_versions())
    assert svc.cache.get(key) is None


def test_a_poisoned_cache_entry_is_refused(make_service: Factory, run_dir: Path,
                                          tmp_path: Path) -> None:
    # Hostile: a cache entry for this exact request holding a later row (written by
    # anything but this check) is refused, never served.
    set_as_of(run_dir, AS_OF)
    cache = RequestCache(tmp_path / "cache")
    cache.put(request_key("series", FRED, AS_OF, extractor_versions()),
              {"fetched_at": "2026-10-20T00:00:00Z", "http": None, "final_url": FRED,
               "content_type": "text/csv", "archive": None}, LATER_CSV)
    t = FakeTransport({})
    r = make_service(t, cache_dir=tmp_path / "cache").market_series("fred", "DGS10", "rates")
    assert r["status"] == "series_past_as_of" and r["sha256"] is None and t.calls == []


@pytest.mark.parametrize("body,why", [
    (b"No data", "no data rows"), (b"Date,Close\n", "no data rows"),
    (b"<html><body>Exceeded the daily hits limit</body></html>\n<p>x</p>", "not a date"),
    (b"observation_date,DGS10\nSeptember 30,3.98\n", "YYYY-MM-DD"),
    (b"\xff\xfe\x00junk", "not UTF-8"),
])
def test_an_answer_that_is_not_a_series_keeps_nothing(make_service: Factory, run_dir: Path,
                                                      body: bytes, why: str) -> None:
    set_as_of(run_dir, AS_OF)
    r = make_service(FakeTransport({FRED: _csv(body)})).market_series("fred", "DGS10", "x")
    assert r["status"] == "series_unreadable" and why in r["detail"] and r["sha256"] is None


def test_a_redirect_off_the_source_is_refused(make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, AS_OF)
    t = FakeTransport({FRED: Reply(302, [("Location", "https://evil.example.org/x.csv")]),
                       "https://evil.example.org/x.csv": _csv(LATER_CSV)})
    r = make_service(t).market_series("fred", "DGS10", "rates")
    assert r["sha256"] is None and r["status"] != "ok"
    assert all("evil.example.org" not in c[0] for c in t.calls)


def test_a_reason_is_required(make_service: Factory, run_dir: Path) -> None:
    with pytest.raises(EvidenceError) as e:
        make_service(FakeTransport({})).market_series("fred", "DGS10", " ")
    assert e.value.code == "reason_required"


def test_a_live_run_takes_the_whole_series(make_service: Factory, run_dir: Path) -> None:
    url = series_url("fred", "DGS10", None)
    r = make_service(FakeTransport({url: _csv(LATER_CSV)})).market_series("fred", "DGS10",
                                                                          "rates")
    assert r["status"] == "ok" and r["series"]["last_date"] == "2026-10-02"
