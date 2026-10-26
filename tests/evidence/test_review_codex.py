"""Regression tests for the cross-family review of cbefb29 (items 1 to 8 and the two
suspected ones). Each reproduces the attack."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeTransport, Reply, html, set_as_of

from stemma.evidence import api, limits, netpolicy, transport
from stemma.evidence import fetch as fetch_mod
from stemma.evidence.archive import cdx_query_url
from stemma.evidence.errors import CaptureStatus
from stemma.evidence.extract import extract
from stemma.evidence.fetch import Fetcher
from stemma.evidence.htmltext import html_to_text
from stemma.evidence.quotes import find_quote
from stemma.evidence.render import Rendered
from stemma.evidence.service import EvidenceService
from stemma.evidence.transport import StdlibTransport

Factory = Callable[..., EvidenceService]
FILL = "<p>" + "Invented commentary sentence that pads the page to length. " * 12 + "</p>"


def _pdf(ops: bytes) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(ops)).encode() + b" >>\nstream\n" + ops + b"\nendstream",
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>")]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


VISIBLE = b"(" + b"Visible annual report text about the company and its results. " * 3 + b") Tj T* "


# 1. PDF visibility flags survive a NUL glyph; clip-only text is invisible ------------

@pytest.mark.parametrize("ops", [
    b"BT /F1 10 Tf 12 TL 72 720 Td " + VISIBLE + b"(Code \\000 here.) Tj T* "
    b"3 Tr (Revenue was 9,999 million.) Tj ET",
    b"BT /F1 10 Tf 12 TL 72 720 Td " + VISIBLE + b"7 Tr (Revenue was 9,999 million.) Tj ET",
])
def test_invisible_pdf_text_flagged_despite_nul_and_clip_mode(ops: bytes) -> None:
    result = extract(_pdf(ops), "application/pdf")
    text = result.text or ""
    assert "9,999" in text
    flagged = [text[r["start"]:r["end"]] for r in result.flagged
               if r["kind"] == "invisible_pdf_text"]
    assert any("9,999" in piece for piece in flagged), (result.flagged, text)


# 2. No quote edge may change a quantity or its sign -------------------------------------

@pytest.mark.parametrize("text, quote", [
    ("rose by .5 million units", "5 million units"),
    ("a dose of 1e-3 units", "3 units"),
    ("a dose of 1E+3 units", "3 units"),
    ("a loss of -$5 million", "$5 million"),
    ("revenue of $5 million", "5 million"),
    ("revenue of €12 million", "12 million"),
    ("the dose was 1e-3", "the dose was 1"),
    ("the rate was 25%", "the rate was 25"),
    ("the price was 5€", "the price was 5"),
    ("change was -3 points", "change was -"),
    ("total was 1.5", "total was 1."),
])
def test_quantity_edges_refused(text: str, quote: str) -> None:
    assert find_quote(text, quote)["found"] is False, (text, quote)


@pytest.mark.parametrize("text, quote", [
    ("a loss of -$5 million", "-$5 million"),
    ("the rate was 25% this year", "the rate was 25%"),
    ("The total was 1,250. Next year", "The total was 1,250."),
    ("COVID-19 cases rose", "19 cases rose"),
    ("between 2020-2021 sales", "2021 sales"),
])
def test_whole_quantities_found(text: str, quote: str) -> None:
    assert find_quote(text, quote)["found"] is True, (text, quote)


# 3. Digits from two elements never meet without a recorded split -----------------------

def test_css_block_spans_split_and_recorded() -> None:
    text, flagged, warnings = html_to_text(
        "<p><span style='display:block'>100</span><span style='display:block'>200</span></p>")
    assert "100200" not in text and "100" in text and "200" in text
    splits = [r for r in flagged if r["kind"] == "element_split"]
    assert len(splits) == 1 and any("element_splits" in w for w in warnings)
    assert find_quote(text, "100200", flagged)["found"] is False
    assert find_quote(text, "200", flagged)["found"] is False  # edge at a split: refused
    assert find_quote(text, "100", flagged)["found"] is False


def test_split_only_where_digits_meet() -> None:
    text, flagged, _ = html_to_text("<p>Revenue <span>rose</span> to <span>1,250</span> million</p>")
    assert "Revenue rose to 1,250 million" in text and not flagged
    text, flagged, _ = html_to_text("<p><span>1</span><span>250</span> units</p>")
    assert "1250" not in text and [r["kind"] for r in flagged] == ["element_split"]


def test_split_recorded_on_receipt(make_service: Factory, run_dir: Path) -> None:
    page = html("<div><span style='display:block'>100</span><span>200</span></div>" + FILL)
    t = FakeTransport({"https://example.org/": Reply(200, [("Content-Type", "text/html")], page)})
    r = make_service(t).capture("https://example.org/", "probe")
    assert any(x["kind"] == "element_split" for x in r["flagged_ranges"])
    assert api.check_quote(run_dir, r["receipt_id"], "100200")["found"] is False


# 4. The deadline holds over TLS ----------------------------------------------------------

@pytest.fixture
def tls_files(tmp_path: Path) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available to make a test certificate")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=example.com",
                    "-addext", "subjectAltName=DNS:example.com"],
                   check=True, capture_output=True)
    return key, cert


class _Loopback(StdlibTransport):
    def resolve(self, host: str) -> list[str]:
        return ["127.0.0.1"]


def _listen(handler: Callable[[socket.socket], None]) -> tuple[socket.socket, int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_tls_header_drip_stopped_at_deadline(tls_files: tuple[Path, Path],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    key, cert = tls_files
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)

    def handler(raw: socket.socket) -> None:
        try:
            conn = server_ctx.wrap_socket(raw, server_side=True)
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            for _ in range(20):
                time.sleep(0.4)
                conn.sendall(b"X-Slow: 1\r\n")
        except (OSError, ssl.SSLError):
            return

    srv, port = _listen(handler)
    monkeypatch.setattr(netpolicy, "ALLOWED_PORTS", frozenset({80, 443, port}))
    monkeypatch.setattr(fetch_mod, "check_resolved", lambda host, addrs: addrs[0])
    monkeypatch.setattr(transport, "_tls_context",
                        lambda: ssl.create_default_context(cafile=str(cert)))
    monkeypatch.setattr(limits, "TOTAL_DEADLINE_S", 1.5)
    monkeypatch.setattr(limits, "READ_TIMEOUT_S", 1.0)
    started = time.monotonic()
    result = Fetcher(_Loopback(), throttle=False).fetch(f"https://example.com:{port}/")
    srv.close()
    assert time.monotonic() - started < 2.5, result
    assert result.failure is CaptureStatus.TIMEOUT


# 5, 7. The browser fallback is off unless the run turns it on; status is checked -----

class _Spy:
    name, version = "spy", "0"

    def __init__(self, result: Rendered | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    def render(self, url: str) -> Rendered:
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


SHELL = b"<html><body><div id='root'></div></body></html>"
FULL = html("<h1>Dashboard</h1>" + "<p>Orders rose 12% to 4,410.</p>" * 10)


def test_fallback_off_by_default(run_dir: Path, tmp_path: Path) -> None:
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], SHELL)})
    svc = EvidenceService(run_dir, transport=t, cache_dir=None, throttle=False)
    spy = _Spy(Rendered(FULL, "https://app.example.org/", []))
    svc._renderer = spy  # a browser present but not enabled for this run
    r = svc.capture("https://app.example.org/", "probe")
    assert spy.calls == [] and r["kind"] == "web"
    assert "browser_fallback_off_for_this_run" in r["warnings"]


def test_fallback_runs_when_enabled(run_dir: Path) -> None:
    (run_dir / "run.json").write_text(json.dumps({"run_id": "t", "as_of": None,
                                                 "evidence": {"browser_fallback": True}}))
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], SHELL)})
    spy = _Spy(Rendered(FULL, "https://app.example.org/", []))
    svc = EvidenceService(run_dir, transport=t, cache_dir=None, throttle=False, renderer=spy)
    assert svc.capture("https://app.example.org/", "probe")["kind"] == "rendered"


@pytest.mark.parametrize("status, expected", [(403, "forbidden"), (404, "not_found"),
                                              (500, "http_error")])
def test_rendered_error_status_is_typed(run_dir: Path, status: int, expected: str) -> None:
    (run_dir / "run.json").write_text(json.dumps({"run_id": "t", "as_of": None,
                                                 "evidence": {"browser_fallback": True}}))
    from stemma.evidence.render import RenderHttpError

    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], SHELL)})
    spy = _Spy(RenderHttpError(status, FULL))
    svc = EvidenceService(run_dir, transport=t, cache_dir=None, throttle=False, renderer=spy)
    r = svc.capture("https://app.example.org/", "probe")
    rendered = [json.loads(p.read_text()) for p in (run_dir / "store" / "receipts").glob("*.json")]
    statuses = {x["kind"]: x["status"] for x in rendered}
    assert statuses["rendered"] == expected
    assert r["status"] in ("empty_text", "short_text")  # the plain capture is returned
    assert all(x.get("text_path") is None for x in rendered if x["kind"] == "rendered")


def test_playwright_gate_allows_only_the_validated_document() -> None:
    from stemma.evidence.render import _gate_decision

    main = "https://app.example.org/page"
    assert _gate_decision(main, main, True) is True
    assert _gate_decision("https://app.example.org/app.js", main, False) is False
    assert _gate_decision("https://elsewhere.example.net/", main, True) is False
    assert _gate_decision("https://app.example.org/other", main, True) is False


# 6. Archive Memento-Datetime is read from every header ------------------------------------

URL = "https://stats.example.org/bulletin"


def _archive(make_service: Factory, run_dir: Path, headers: list[tuple[str, str]]) -> dict[str, Any]:
    set_as_of(run_dir, "2024-01-01")
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"],
            ["20231231120000", URL, "200", "text/html", "D"]]
    routes = {cdx_query_url(URL, "2024-01-01"): Reply(200, [], json.dumps(rows).encode()),
              f"https://web.archive.org/web/20231231120000id_/{URL}":
                  Reply(200, [("Content-Type", "text/html"), *headers], html(FILL))}
    svc = make_service(FakeTransport(routes), cache_dir=None)
    svc.archive.sleep = lambda _s: None
    return svc.capture(URL, "probe")


def test_memento_after_many_headers_still_read(make_service: Factory, run_dir: Path) -> None:
    padding = [(f"X-Pad-{i}", "p" * 200) for i in range(90)]  # about 18 KiB first
    r = _archive(make_service, run_dir, [*padding,
                                         ("Memento-Datetime", "Tue, 02 Jan 2024 00:00:00 GMT")])
    assert r["status"] == "no_snapshot_before_as_of"


def test_missing_memento_refused(make_service: Factory, run_dir: Path) -> None:
    assert _archive(make_service, run_dir, [])["status"] == "no_snapshot_before_as_of"


def test_two_mementos_any_late_refused(make_service: Factory, run_dir: Path) -> None:
    r = _archive(make_service, run_dir, [("Memento-Datetime", "Sun, 31 Dec 2023 12:00:00 GMT"),
                                         ("Memento-Datetime", "Tue, 02 Jan 2024 00:00:00 GMT")])
    assert r["status"] == "no_snapshot_before_as_of"


def test_headers_over_limit_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_HEADER_BYTES", 4096)
    t = FakeTransport({"https://example.org/": Reply(
        200, [(f"X-Pad-{i}", "p" * 200) for i in range(40)], b"x")})
    result = Fetcher(t, throttle=False).fetch("https://example.org/")
    assert result.failure is CaptureStatus.TRANSPORT_ERROR


# 8. A FIFO under task/ does not hang capture_file -----------------------------------------

def test_fifo_attachment_does_not_hang(make_service: Factory, run_dir: Path) -> None:
    os.mkfifo(run_dir / "task" / "pipe.txt")

    def alarm(_signum: int, _frame: Any) -> None:
        raise TimeoutError("capture_file hung on a FIFO")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(5)
    try:
        r = make_service().capture_file("task/pipe.txt", "probe")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert r["status"] == "file_not_found" and "regular file" in r["detail"]


# Suspected: PDF extraction runs under a time and output limit ----------------------------

def test_pdf_extraction_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from stemma.evidence import extract as extract_mod

    monkeypatch.setattr(limits, "PDF_EXTRACT_TIMEOUT_S", 1.0)
    # A worker that hangs, as PDFium can on a hostile file.
    monkeypatch.setattr(extract_mod, "_pdf_worker_argv",
                        lambda: [sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    result = extract(_pdf(b"BT /F1 10 Tf 72 720 Td " + VISIBLE + b"ET"), "application/pdf")
    assert time.monotonic() - started < 4
    assert result.status is CaptureStatus.EXTRACTION_FAILED and "time limit" in (result.detail or "")


def test_pdf_extraction_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "PDF_MAX_TEXT_CHARS", 50)
    result = extract(_pdf(b"BT /F1 10 Tf 72 720 Td " + VISIBLE + b"ET"), "application/pdf")
    assert result.status is CaptureStatus.EXTRACTION_FAILED and "limit" in (result.detail or "")


# Suspected: extraction reuse keyed by bytes, content type and charset -------------------

def test_same_bytes_other_charset_gets_its_own_text(make_service: Factory, run_dir: Path) -> None:
    body = ("<html><body><p>" + "Prix élevé de 5 euros. " * 20 + "</p></body></html>").encode(
        "latin-1")
    t = FakeTransport({
        "https://a.example.org/": Reply(200, [("Content-Type", "text/html; charset=iso-8859-1")], body),
        "https://b.example.org/": Reply(200, [("Content-Type", "text/html; charset=koi8-r")], body)})
    svc = make_service(t, cache_dir=None)
    first = svc.capture("https://a.example.org/", "latin-1")
    second = svc.capture("https://b.example.org/", "koi8-r")
    assert first["sha256"] == second["sha256"]
    assert first["text_id"] != second["text_id"] and second["charset"] == "koi8-r"
    assert "élevé" in svc.read_text(first["receipt_id"])
    assert "élevé" not in svc.read_text(second["receipt_id"])
    again = svc.capture("https://a.example.org/", "latin-1 again")
    assert again["text_id"] == first["text_id"]
    assert svc.read(second["text_id"], 0, 50)["text"] == svc.read_text(second["receipt_id"])[:50]


@pytest.fixture(autouse=True)
def _no_leftover_alarm() -> Iterator[None]:
    yield
    signal.alarm(0)
