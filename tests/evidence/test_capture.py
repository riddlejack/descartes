"""capture and capture_file end to end over a fake transport: receipts for every
attempt, bytes written once, the request-keyed cache, and the store layout."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeTransport, Reply, html, make_pdf, set_as_of

from stemma.evidence.errors import EvidenceError
from stemma.evidence.service import EvidenceService

PAGE = html("<h1>Invented statistics bulletin</h1>" + "<p>Unemployment was 4.2% in March.</p>" * 10)
Factory = Callable[..., EvidenceService]


def receipt_files(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "store" / "receipts").glob("*.json"))


def test_capture_html_receipt(make_service: Factory, run_dir: Path) -> None:
    t = FakeTransport({"https://stats.example.org/bulletin": Reply(
        200, [("Content-Type", "text/html; charset=utf-8")], PAGE)})
    r = make_service(t).capture("https://stats.example.org/bulletin", "headline rate")
    assert r["status"] == "ok"
    for key in ("receipt_id", "sha256", "content_type", "text_path", "text_chars", "outline",
                "warnings", "served_from_cache", "http"):
        assert key in r
    assert r["http"]["status"] == 200 and r["http"]["final_url"] == "https://stats.example.org/bulletin"
    assert r["served_from_cache"] is False
    assert r["extractor"]["name"] == "markdownify" and r["extractor"]["version"] == "1.2.3"
    raw = run_dir / "store" / "bytes" / r["sha256"]
    assert raw.read_bytes() == PAGE
    assert not raw.stat().st_mode & stat.S_IWUSR  # read-only
    text = (run_dir / r["text_path"]).read_text("utf-8")
    assert len(text) == r["text_chars"] and "4.2%" in text
    stored = json.loads((run_dir / "store" / "receipts" / f"{r['receipt_id']}.json").read_text())
    assert stored == r
    assert (run_dir / "store" / "index.sqlite").exists()


def test_every_failure_writes_a_receipt(make_service: Factory, run_dir: Path) -> None:
    t = FakeTransport({
        "https://example.org/gone": Reply(404, [("Content-Type", "text/html")], b"<p>Not here</p>"),
        "https://example.org/wall": Reply(403, [], b"denied"),
        "https://example.org/busy": Reply(429, [], b""),
        "https://example.org/oops": Reply(500, [], b""),
        "https://example.org/hop": Reply(302, [("Location", "http://10.0.0.1/")]),
    })
    svc = make_service(t)
    receipts = [svc.capture(u, "probe") for u in (
        "https://example.org/gone", "https://example.org/wall", "https://example.org/busy",
        "https://example.org/oops", "https://example.org/hop", "http://localhost.invalid:9/",
        "not a url")]
    statuses = [r["status"] for r in receipts]
    assert statuses == ["not_found", "forbidden", "rate_limited", "http_error",
                        "refused_address", "refused_address", "invalid_url"]
    assert len(receipt_files(run_dir)) == 7
    gone = json.loads((run_dir / "store" / "receipts" / f"{receipts[0]['receipt_id']}.json")
                      .read_text())
    # the error page's bytes are kept but never indexed or quotable
    assert gone["status"] == "not_found" and gone["sha256"] and gone["text_path"] is None


def test_same_bytes_stored_once(make_service: Factory, run_dir: Path) -> None:
    t = FakeTransport({"https://a.example.org/": Reply(200, [("Content-Type", "text/html")], PAGE),
                       "https://b.example.org/": Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t, cache_dir=None)
    r1 = svc.capture("https://a.example.org/", "one")
    r2 = svc.capture("https://b.example.org/", "two")
    assert r1["sha256"] == r2["sha256"] and r1["receipt_id"] != r2["receipt_id"]
    assert len(os.listdir(run_dir / "store" / "bytes")) == 1


def test_tampered_bytes_detected(make_service: Factory, run_dir: Path) -> None:
    t = FakeTransport({"https://a.example.org/": Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t, cache_dir=None)
    r = svc.capture("https://a.example.org/", "one")
    path = run_dir / "store" / "bytes" / r["sha256"]
    path.chmod(0o644)
    path.write_bytes(b"forged")
    with pytest.raises(EvidenceError) as info:
        svc.capture("https://a.example.org/", "again")
    assert info.value.code == "store_corrupt"


def test_cache_serves_only_the_same_request(make_service: Factory, run_dir: Path,
                                            tmp_path: Path) -> None:
    url = "https://stats.example.org/bulletin"
    t = FakeTransport({url: Reply(200, [("Content-Type", "text/html")], PAGE)})
    first = make_service(t).capture(url, "first run")
    assert len(t.calls) == 1
    # A second run (its own folder) making the same request is served from the cache.
    other = tmp_path / "run2"
    other.mkdir()
    svc2 = EvidenceService(other, transport=t, cache_dir=tmp_path / "cache", renderer=None,
                           throttle=False)
    second = svc2.capture(url, "second run")
    assert len(t.calls) == 1
    assert second["served_from_cache"] is True
    assert second["original_fetched_at"] == first["fetched_at"]
    assert second["sha256"] == first["sha256"]
    assert (other / "store" / "bytes" / second["sha256"]).exists()  # its own store
    # A different request (another URL, or an as_of date) is not served from it.
    svc2.capture(url + "?page=2", "different url")
    assert len(t.calls) == 2


def test_cache_key_includes_as_of(make_service: Factory, run_dir: Path) -> None:
    url = "https://stats.example.org/bulletin"
    t = FakeTransport({url: Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t)
    svc.capture(url, "live")
    set_as_of(run_dir, "2024-06-01")
    r = svc.capture(url, "as of")  # must go to the archive, not the live cache entry
    assert r["served_from_cache"] is False
    assert r["kind"] == "archive"


def test_corrupt_cache_entry_ignored(make_service: Factory, tmp_path: Path) -> None:
    url = "https://stats.example.org/bulletin"
    t = FakeTransport({url: Reply(200, [("Content-Type", "text/html")], PAGE)})
    svc = make_service(t)
    svc.capture(url, "first")
    for path in (tmp_path / "cache").rglob("*.bytes"):
        path.write_bytes(b"poisoned")
    r = svc.capture(url, "second")
    assert r["served_from_cache"] is False and "cache_entry_corrupt_ignored" in r["warnings"]
    assert len(t.calls) == 2


def test_failures_not_cached(make_service: Factory) -> None:
    url = "https://example.org/flaky"
    t = FakeTransport({url: [Reply(503, [], b""), Reply(200, [("Content-Type", "text/html")], PAGE)]})
    svc = make_service(t)
    assert svc.capture(url, "a")["status"] == "http_error"
    assert svc.capture(url, "b")["status"] == "ok"


def test_reason_required(make_service: Factory) -> None:
    with pytest.raises(EvidenceError) as info:
        make_service().capture("https://example.org/", "  ")
    assert info.value.code == "reason_required"


def test_capture_file_pdf(make_service: Factory, run_dir: Path) -> None:
    pdf = make_pdf([["Client data pack", "Churn was 3.1% a month across the invented base",
                     "of subscribers, measured over the twelve months of the year."]])
    (run_dir / "task" / "attachments" / "pack.pdf").write_bytes(pdf)
    r = make_service().capture_file("task/attachments/pack.pdf", "user's data pack")
    assert r["status"] == "ok" and r["kind"] == "file"
    assert r["content_type"] == "application/pdf" and r["pages"] == 1
    assert r["source_path"] == "task/attachments/pack.pdf"


def test_capture_file_refuses_outside_task(make_service: Factory, run_dir: Path,
                                           tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (run_dir / "task" / "attachments" / "link.txt").symlink_to(secret)
    svc = make_service()
    for path in (str(secret), "../secret.txt", "task/../../secret.txt",
                 "task/attachments/link.txt", "run.json"):
        r = svc.capture_file(path, "probe")
        assert r["status"] == "path_outside_task", path
        assert r["sha256"] is None
    assert svc.capture_file("task/attachments/missing.pdf", "probe")["status"] == "file_not_found"
