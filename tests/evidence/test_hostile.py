"""Regression tests for ways round the guards found by an independent hostile probe.
Each failed (crash, silent pass or wrong answer) before its fix."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeSec, FakeTransport, Reply, html, set_as_of

from stemma.evidence import api
from stemma.evidence.archive import cdx_query_url
from stemma.evidence.errors import EvidenceError
from stemma.evidence.netpolicy import PolicyRefusal, parse_url
from stemma.evidence.quotes import find_quote
from stemma.evidence.sec import FilingRef, check_accession, check_document, concept_url
from stemma.evidence.service import EvidenceService
from stemma.evidence.store import check_receipt_id, check_sha256

Factory = Callable[..., EvidenceService]
URL = "https://stats.example.org/bulletin"
PAGE = html("<h1>Bulletin</h1>" + "<p>Rate rose 15% in the year to 1,250 units.</p>" * 10)


def cdx(stamp: str) -> Reply:
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"],
            [stamp, URL, "200", "text/html", "D"]]
    return Reply(200, [], json.dumps(rows).encode())


def snap(stamp: str) -> str:
    return f"https://web.archive.org/web/{stamp}id_/{URL}"


@pytest.mark.parametrize("target", ["/web/2025id_/", "/web/20251399000000id_/"])
def test_archive_redirect_with_partial_or_invalid_timestamp(make_service: Factory, run_dir: Path,
                                                            target: str) -> None:
    set_as_of(run_dir, "2025-06-01")
    later = f"https://web.archive.org{target}{URL}"
    routes = {cdx_query_url(URL, "2025-06-01"): cdx("20250101000000"),
              snap("20250101000000"): Reply(302, [("Location", later)]),
              later: Reply(200, [("Content-Type", "text/html")], PAGE)}
    r = make_service(FakeTransport(routes)).capture(URL, "probe")
    assert r["status"] == "no_snapshot_before_as_of"


def test_memento_without_zone_is_read_as_gmt(make_service: Factory, run_dir: Path) -> None:
    set_as_of(run_dir, "2025-06-01")
    routes = {cdx_query_url(URL, "2025-06-01"): cdx("20250101000000"),
              snap("20250101000000"): Reply(200, [("Content-Type", "text/html"),
                                                  ("Memento-Datetime", "2025-07-01 00:00:00")],
                                            PAGE)}
    assert make_service(FakeTransport(routes)).capture(URL, "probe")["status"] == (
        "no_snapshot_before_as_of")


def test_same_bytes_other_type_reuses_first_text(make_service: Factory) -> None:
    t = FakeTransport({"https://a.example.org/": Reply(200, [("Content-Type", "text/html")], PAGE),
                       "https://b.example.org/": Reply(200, [("Content-Type", "text/plain")], PAGE),
                       "https://c.example.org/": Reply(200, [("Content-Type",
                                                              "text/html; charset=koi8-r")], PAGE)})
    svc = make_service(t, cache_dir=None)
    first = svc.capture("https://a.example.org/", "html")
    for url in ("https://b.example.org/", "https://c.example.org/"):
        again = svc.capture(url, "same bytes, other headers")
        assert again["status"] == first["status"] and again["text_chars"] == first["text_chars"]
        assert again["content_type"] == "text/html"
        assert any(w.startswith("text_reused_from_receipt") for w in again["warnings"])


@pytest.mark.parametrize("url", ["http://example.org/café", "http://example.org/?q=€",
                                 "http://bücher.example/例"])
def test_non_ascii_urls_go_on_the_wire_encoded(url: str) -> None:
    parsed = parse_url(url)
    parsed.url.encode("ascii")
    parsed.path_and_query.encode("ascii")
    parsed.host.encode("ascii")


@pytest.mark.parametrize("url", ["http://[fec0::1]/", "http://[::ffff:fec0:1]/"])
def test_site_local_ipv6_refused(url: str) -> None:
    with pytest.raises((PolicyRefusal, ValueError)):
        parse_url(url)


def test_site_local_dns_answer_refused(make_service: Factory) -> None:
    t = FakeTransport({"https://x.example.org/": Reply(200, [], b"x")},
                      dns={"x.example.org": ["fec0::1"]})
    assert make_service(t).capture("https://x.example.org/", "probe")["status"] == "refused_address"
    assert t.calls == []


@pytest.mark.parametrize("path", ["task/a\x00b.txt", "task/\ud800.txt"])
def test_capture_file_odd_paths_typed(make_service: Factory, path: str) -> None:
    r = make_service().capture_file(path, "probe")
    assert r["status"] == "path_outside_task"


def test_capture_file_unreadable_loop_and_hardlink(make_service: Factory, run_dir: Path,
                                                   tmp_path: Path) -> None:
    folder = run_dir / "task" / "attachments"
    locked = folder / "locked.txt"
    locked.write_text("secret")
    locked.chmod(0)
    (folder / "loop").symlink_to(folder / "loop")
    outside = tmp_path / "outside.txt"
    outside.write_text("private data outside the run")
    os.link(outside, folder / "hard.txt")
    svc = make_service()
    try:
        assert svc.capture_file("task/attachments/locked.txt", "p")["status"] == "file_unreadable"
    finally:
        locked.chmod(0o644)
    assert svc.capture_file("task/attachments/loop", "p")["status"] in (
        "path_outside_task", "file_unreadable", "file_not_found")
    assert svc.capture_file("task/attachments/hard.txt", "p")["status"] == "path_outside_task"


def test_forged_cache_entry_under_other_key_ignored(make_service: Factory, tmp_path: Path) -> None:
    t = FakeTransport({URL: Reply(200, [("Content-Type", "text/html")], PAGE),
                       URL + "?b": Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t)
    svc.capture(URL, "a")
    svc.capture(URL + "?b", "b")
    metas = sorted((tmp_path / "cache").rglob("*.json"))
    other = next(m for m in metas if json.loads(m.read_text())["final_url"] == URL + "?b")
    mine = next(m for m in metas if m != other)
    other.write_text(mine.read_text())  # copy one request's entry over another's
    other.with_suffix(".bytes").write_bytes(mine.with_suffix(".bytes").read_bytes())
    r = svc.capture(URL + "?b", "again")
    assert r["served_from_cache"] is False and "cache_entry_corrupt_ignored" in r["warnings"]


@pytest.mark.parametrize("query", ["15%\x00", "\x00\x00\x00", "\ud800abc"])
def test_search_rejects_nul_and_surrogates(make_service: Factory, query: str) -> None:
    t = FakeTransport({URL: Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t)
    svc.capture(URL, "a")
    for mode in ("exact", "words"):
        with pytest.raises(EvidenceError) as info:
            svc.search_store(query, mode=mode)
        assert info.value.code == "bad_query"


def test_quotes_never_match_inside_numbers_or_words() -> None:
    text = "Rate rose 15% in the year to 1,250 units. Café sales."
    assert not find_quote(text, "5% in the year")["found"]
    assert not find_quote(text, "year to 1,2")["found"]
    assert not find_quote(text, "Cafe")["found"]  # would split a combining accent
    assert find_quote(text, "15% in the year to 1,250 units.")["found"]
    assert find_quote(text, "rose 15%")["found"]


def test_trailing_newline_ids_refused() -> None:
    for check, value in ((check_sha256, "a" * 64 + "\n"),
                         (check_receipt_id, "r_20261025T000000_abcdefabcdef\n"),
                         (check_accession, "0000999999-23-000010\n"),
                         (check_document, "a.htm\n")):
        with pytest.raises(EvidenceError):
            check(value)


def test_surrogate_in_reason_and_url_still_receipted(make_service: Factory, run_dir: Path) -> None:
    svc = make_service()
    a = svc.capture("https://example.org/\ud800", "probe")
    b = svc.capture_file("task/x.txt", "reason \ud800")
    assert a["status"] == "invalid_url" and b["status"] == "file_not_found"
    assert len(list((run_dir / "store" / "receipts").glob("*.json"))) == 2


def test_sec_facts_drop_frame_when_dated(make_service: Factory, run_dir: Path) -> None:
    filing = FilingRef("0000999999-23-000010", 999999, "Invented Co", "10-K", "2023-02-20",
                       None, "inv.htm")
    concept = {"units": {"USD": [{"start": "2022-01-01", "end": "2022-12-31", "val": 1,
                                  "accn": filing.accession, "form": "10-K",
                                  "filed": "2023-02-20", "frame": "CY2022"}]}}
    t = FakeTransport({concept_url(999999, "us-gaap", "Revenues"):
                       Reply(200, [], json.dumps(concept).encode()),
                       filing.document_url(): Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t, sec_backend=FakeSec([filing]))
    assert "frame" in svc.sec_facts("INVT", "Revenues")["facts"][0]
    set_as_of(run_dir, "2024-01-01")
    assert "frame" not in svc.sec_facts("INVT", "Revenues")["facts"][0]


def test_read_text_after_fixes(make_service: Factory, run_dir: Path) -> None:
    t = FakeTransport({URL: Reply(200, [("Content-Type", "text/html")], PAGE)})
    r = make_service(t).capture(URL, "a")
    assert "1,250 units" in api.read_text(run_dir, r["receipt_id"])
