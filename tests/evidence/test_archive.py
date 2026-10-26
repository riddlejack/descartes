"""as_of mode: every capture is an archived copy dated before as_of, or a typed failure.
Hostile cases: the archive offers only later copies, redirects to a later copy,
redirects out of the archive, or dates the copy after as_of in its headers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from conftest import FakeTransport, Reply, html, set_as_of

from stemma.evidence.archive import cdx_query_url
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
URL = "https://stats.example.org/bulletin"
AS_OF = "2024-06-01"
OLD = html("<h1>Bulletin</h1>" + "<p>Rate was 3.9% in April 2024.</p>" * 10)


def cdx(*stamps: str) -> Reply:
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"]]
    rows += [[s, URL, "200", "text/html", "D"] for s in stamps]
    return Reply(200, [("Content-Type", "application/json")], json.dumps(rows).encode())


def snap(stamp: str) -> str:
    return f"https://web.archive.org/web/{stamp}id_/{URL}"


def svc_with(make_service: Factory, run_dir: Path, routes: dict[str, object]) -> tuple[
        EvidenceService, FakeTransport]:
    set_as_of(run_dir, AS_OF)
    t = FakeTransport(routes)
    svc = make_service(t)
    svc.archive.sleep = lambda _s: None
    return svc, t


def test_cdx_query_asks_for_day_before() -> None:
    query = cdx_query_url(URL, AS_OF)
    assert "to=20240531" in query and "filter=statuscode:200" in query


def test_newest_snapshot_before_as_of_used(make_service: Factory, run_dir: Path) -> None:
    routes = {
        cdx_query_url(URL, AS_OF): cdx("20240101120000", "20240530235959"),
        snap("20240530235959"): Reply(200, [("Content-Type", "text/html"),
                                            ("Memento-Datetime", "Thu, 30 May 2024 23:59:59 GMT")],
                                      OLD),
    }
    svc, t = svc_with(make_service, run_dir, routes)
    r = svc.capture(URL, "rate as of")
    assert r["status"] == "ok" and r["kind"] == "archive"
    assert r["archive"]["snapshot_timestamp"] == "20240530235959"
    assert r["archive"]["snapshot_age_days"] == 1
    assert r["as_of"] == AS_OF
    assert all("stats.example.org" not in c[0].split("/")[2] for c in t.calls)  # never live


def test_only_later_snapshots_fails_typed(make_service: Factory, run_dir: Path) -> None:
    # A misbehaving index lists a capture on the as_of date itself.
    svc, _ = svc_with(make_service, run_dir, {cdx_query_url(URL, AS_OF): cdx("20240601000000")})
    r = svc.capture(URL, "probe")
    assert r["status"] == "no_snapshot_before_as_of" and r["sha256"] is None


def test_no_captures_fails_typed(make_service: Factory, run_dir: Path) -> None:
    svc, _ = svc_with(make_service, run_dir, {cdx_query_url(URL, AS_OF): Reply(200, [], b"[]")})
    assert svc.capture(URL, "probe")["status"] == "no_snapshot_before_as_of"


def test_redirect_to_later_capture_refused(make_service: Factory, run_dir: Path) -> None:
    routes = {
        cdx_query_url(URL, AS_OF): cdx("20240530000000"),
        snap("20240530000000"): Reply(302, [("Location", snap("20240715000000"))]),
        snap("20240715000000"): Reply(200, [("Content-Type", "text/html")], OLD),
    }
    svc, t = svc_with(make_service, run_dir, routes)
    r = svc.capture(URL, "probe")
    assert r["status"] == "no_snapshot_before_as_of"
    assert snap("20240715000000") not in [c[0] for c in t.calls]


def test_redirect_out_of_archive_refused(make_service: Factory, run_dir: Path) -> None:
    routes = {
        cdx_query_url(URL, AS_OF): cdx("20240530000000"),
        snap("20240530000000"): Reply(302, [("Location", URL)]),
        URL: Reply(200, [("Content-Type", "text/html")], OLD),
    }
    svc, t = svc_with(make_service, run_dir, routes)
    assert svc.capture(URL, "probe")["status"] == "no_snapshot_before_as_of"
    assert URL not in [c[0] for c in t.calls]


def test_memento_date_after_as_of_refused(make_service: Factory, run_dir: Path) -> None:
    routes = {
        cdx_query_url(URL, AS_OF): cdx("20240530000000"),
        snap("20240530000000"): Reply(200, [("Content-Type", "text/html"),
                                            ("Memento-Datetime", "Mon, 15 Jul 2024 00:00:00 GMT")],
                                      OLD),
    }
    svc, _ = svc_with(make_service, run_dir, routes)
    assert svc.capture(URL, "probe")["status"] == "no_snapshot_before_as_of"


def test_archive_down_is_typed_after_retries(make_service: Factory, run_dir: Path) -> None:
    svc, t = svc_with(make_service, run_dir, {cdx_query_url(URL, AS_OF): Reply(503, [], b"")})
    r = svc.capture(URL, "probe")
    assert r["status"] == "archive_unavailable"
    assert len(t.calls) == 3 and len(r["archive"]["lookup_attempts"]) == 3


def test_archive_replay_error_is_not_absence(make_service: Factory, run_dir: Path) -> None:
    routes = {cdx_query_url(URL, AS_OF): cdx("20240530000000"),
              snap("20240530000000"): Reply(404, [], b"not in archive")}
    svc, _ = svc_with(make_service, run_dir, routes)
    assert svc.capture(URL, "probe")["status"] == "archive_unavailable"


def test_as_of_loud_page_names_no_fallback(make_service: Factory, run_dir: Path) -> None:
    routes = {cdx_query_url(URL, AS_OF): cdx("20240530000000"),
              snap("20240530000000"): Reply(200, [("Content-Type", "text/html"), (
                  "Memento-Datetime", "Thu, 30 May 2024 00:00:00 GMT")], b"<html></html>")}
    set_as_of(run_dir, AS_OF)
    r = make_service(FakeTransport(routes)).capture(URL, "probe")
    assert r["status"] == "empty_text" and "browser_fallback_unavailable" in r["warnings"]

