"""Regression tests for the merge-gate hostile review (items 1 to 10 and the three
suspected ones). Each reproduces the attack; each failed before its fix."""

from __future__ import annotations

import gzip
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeSec, FakeTransport, Reply, html, make_pdf, set_as_of

from stemma.evidence import api, limits, netpolicy
from stemma.evidence import fetch as fetch_mod
from stemma.evidence.archive import cdx_query_url
from stemma.evidence.charset import prescan
from stemma.evidence.errors import CaptureStatus
from stemma.evidence.extract import extract
from stemma.evidence.fetch import Fetcher
from stemma.evidence.htmltext import html_to_text
from stemma.evidence.netpolicy import PolicyRefusal, parse_url
from stemma.evidence.quotes import find_quote
from stemma.evidence.sec import FilingRef
from stemma.evidence.service import EvidenceService
from stemma.evidence.transport import StdlibTransport

Factory = Callable[..., EvidenceService]
FILL = "<p>" + "Invented commentary sentence that pads the page to length. " * 12 + "</p>"


# 1. Quotes never start or end inside a number -----------------------------------------

@pytest.mark.parametrize("text, quote", [
    ("added 1,250 jobs in May", "250 jobs in May"),
    ("revenue of 2.5 billion dollars", "5 billion dollars"),
    ("revenue of $1.5 billion", "revenue of $1"),
    ("total was 1,250", "total was 1"),
    ("rate of 0.25%", "25%"),
    ("output fell -3.2% last year", "3.2% last year"),
    ("a margin of 12 500 units", "500 units"),
])
def test_quote_inside_number_refused(text: str, quote: str) -> None:
    assert find_quote(text, quote)["found"] is False


@pytest.mark.parametrize("text, quote", [
    ("added 1,250 jobs in May", "1,250 jobs in May"),
    ("the years 2020-2021 were hard", "2021 were hard"),
    ("it was 1,250. Then", "it was 1,250"),
])
def test_whole_numbers_still_found(text: str, quote: str) -> None:
    assert find_quote(text, quote)["found"] is True


# 2. Figures never fuse in conversion ------------------------------------------------------

@pytest.mark.parametrize("markup, fused, kept", [
    ("<p>Revenue $5<sup>1</sup> billion</p>", "$51 billion", "$5^1^ billion"),
    ("<p>speed 3&times;10<sup>8</sup> m/s</p>", "3×108", "3×10^8^"),
    ("<p>Q<sub>1</sub> 2024</p>", "Q1 2024", "Q~1~ 2024"),
])
def test_sup_sub_marked(markup: str, fused: str, kept: str) -> None:
    text = html_to_text(markup)[0]
    assert fused not in text and kept in text


@pytest.mark.parametrize("tag", ["header", "footer", "figure", "nav", "aside", "address",
                                 "details", "summary", "option", "main", "fieldset"])
def test_adjacent_blocks_do_not_fuse(tag: str) -> None:
    text = html_to_text(f"<{tag}>100</{tag}><{tag}>200</{tag}>")[0]
    assert "100200" not in text and "100" in text and "200" in text


def test_mathml_and_namespaced_and_xml_do_not_fuse() -> None:
    math = html_to_text("<math><mn>10</mn><msup><mn>2</mn><mn>3</mn></msup></math>")[0]
    assert "1023" not in math and "2^3^" in math
    assert "100200" not in html_to_text("<x:a>100</x:a><x:b>200</x:b>")[0]
    assert "100200" not in html_to_text("<doc><a>100</a><b>200</b></doc>", xml=True)[0]
    inline = html_to_text("<p>revenue of <ix:nonFraction>1,204</ix:nonFraction> million</p>")[0]
    assert "revenue of 1,204 million" in inline  # inline XBRL is not split


def test_conversion_deletes_no_visible_character() -> None:
    markup = "<p>Price <del>$50</del> <ins>$40</ins>; noted<sup>a</sup>.</p>"
    text = html_to_text(markup)[0]
    for piece in ("$50", "$40", "noted", "a"):
        assert piece in text


# 3. Charset prescan -----------------------------------------------------------------------

BODY = "<p>" + "Employment rose by 1,250 jobs in the region during the quarter. " * 10 + "</p>"


@pytest.mark.parametrize("head", [
    "<meta charset='utf-16'>",                                   # utf-16 meta means utf-8
    "<!-- <meta charset='koi8-r'> -->",                          # comments do not count
    "<script>var s='<meta charset=\"utf-16be\">';</script>",     # script strings do not count
    "<meta name='description' content='charset=utf-16le'>",      # attributes do not count
])
def test_prescan_real_meta_only(head: str) -> None:
    body = f"<!doctype html><html><head>{head}</head><body>{BODY}</body></html>".encode()
    result = extract(body, "text/html")
    assert result.status is CaptureStatus.OK and "1,250 jobs" in (result.text or "")
    assert result.charset == "utf-8"


def test_prescan_reads_http_equiv_and_xml_declaration() -> None:
    assert prescan(b"<meta http-equiv='Content-Type' content='text/html; charset=koi8-r'>") == "koi8-r"
    greek = "<?xml version='1.0' encoding='ISO-8859-7'?><doc><p>Τιμή 5</p></doc>"
    result = extract(greek.encode("iso-8859-7"), "application/xml")
    assert "Τιμή 5" in (result.text or "") and "xml version" not in (result.text or "")


def test_quoted_utf7_header_refused() -> None:
    result = extract(b"<html><body>+ADw-p+AD4-x</body></html>", "text/html; charset='utf-7'")
    assert result.status is CaptureStatus.UNSUPPORTED_CHARSET


# 4, 5. Truncated bodies and the total deadline, on a loopback server ---------------------

class _Loopback(StdlibTransport):
    def resolve(self, host: str) -> list[str]:
        return ["127.0.0.1"]


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Callable[[socket.socket], None]], int]]:
    servers: list[socket.socket] = []

    def start(handler: Callable[[socket.socket], None]) -> int:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        servers.append(srv)
        port = srv.getsockname()[1]

        def loop() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=handler, args=(conn,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        monkeypatch.setattr(netpolicy, "ALLOWED_PORTS", frozenset({80, 443, port}))
        monkeypatch.setattr(fetch_mod, "check_resolved", lambda host, addrs: addrs[0])
        return port

    yield start
    for srv in servers:
        srv.close()


def _half(data: bytes) -> bytes:
    return data[: len(data) // 2]


def _serve(head: bytes, body: bytes) -> Callable[[socket.socket], None]:
    def handler(conn: socket.socket) -> None:
        conn.recv(65536)
        conn.sendall(head + body)
        conn.close()
    return handler


def test_short_body_refused(loopback: Any) -> None:
    port = loopback(_serve(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                           b"Content-Length: 5000\r\n\r\n", b"<table><tr><td>Q1</td><td>100"))
    result = Fetcher(_Loopback(), throttle=False).fetch(f"http://example.com:{port}/")
    assert result.failure is CaptureStatus.TRANSPORT_ERROR and result.body is None


@pytest.mark.parametrize("make_body", [
    lambda: _half(gzip.compress(b"<p>" + b"Revenue rose to 1,250 million. " * 200 + b"</p>")),
    lambda: gzip.compress(b"<p>2023: 100</p>") + b"\x00\x01garbage",
])
def test_damaged_gzip_refused(loopback: Any, make_body: Callable[[], bytes]) -> None:
    body = make_body()
    port = loopback(_serve(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                           + f"Content-Length: {len(body)}\r\n\r\n".encode(), body))
    result = Fetcher(_Loopback(), throttle=False).fetch(f"http://example.com:{port}/")
    assert result.failure is not None and result.body is None


def test_every_gzip_member_decoded(loopback: Any) -> None:
    body = gzip.compress(b"<p>Revenue 2023: 100</p>") + gzip.compress(b"<p>Revenue 2024: 250</p>")
    port = loopback(_serve(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                           + f"Content-Length: {len(body)}\r\n\r\n".encode(), body))
    result = Fetcher(_Loopback(), throttle=False).fetch(f"http://example.com:{port}/")
    assert result.failure is None and b"250" in (result.body or b"")


def test_drip_feed_stopped_at_total_deadline(loopback: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "TOTAL_DEADLINE_S", 1.5)
    monkeypatch.setattr(limits, "READ_TIMEOUT_S", 1.0)

    def handler(conn: socket.socket) -> None:
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
        for _ in range(20):
            time.sleep(0.4)  # always under the per-read timeout
            try:
                conn.sendall(b"x")
            except OSError:
                return

    port = loopback(handler)
    started = time.monotonic()
    result = Fetcher(_Loopback(), throttle=False).fetch(f"http://example.com:{port}/")
    assert time.monotonic() - started < 2.5
    assert result.failure is CaptureStatus.TIMEOUT


def test_drip_fed_headers_stopped_at_total_deadline(loopback: Any,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "TOTAL_DEADLINE_S", 1.5)
    monkeypatch.setattr(limits, "READ_TIMEOUT_S", 1.0)

    def handler(conn: socket.socket) -> None:
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        for _ in range(20):
            time.sleep(0.4)
            try:
                conn.sendall(b"X-Slow: 1\r\n")
            except OSError:
                return

    port = loopback(handler)
    started = time.monotonic()
    result = Fetcher(_Loopback(), throttle=False).fetch(f"http://example.com:{port}/")
    assert time.monotonic() - started < 2.5 and result.failure is CaptureStatus.TIMEOUT


# 6. capture_file never leaves task/ through a swapped link --------------------------------

def test_capture_file_symlink_swap_race(make_service: Factory, run_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("SECRET record outside task, 31,337 units\n" * 10)
    task = run_dir / "task"
    (task / "d").mkdir()
    (task / "d" / "f.txt").write_text("inside attachment text\n" * 10)
    os.symlink(outside, task / "link")
    svc = make_service()
    stop = threading.Event()

    def swapper() -> None:
        while not stop.is_set():
            os.rename(task / "d", task / "d_real")
            os.rename(task / "link", task / "d")
            os.rename(task / "d", task / "link")
            os.rename(task / "d_real", task / "d")

    thread = threading.Thread(target=swapper, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            r = svc.capture_file("task/d/f.txt", "race")
            if r["status"] == "ok":
                assert "SECRET" not in svc.read_text(r["receipt_id"])
                assert r["source_path"] == "task/d/f.txt"
    finally:
        stop.set()
        thread.join()


def test_capture_file_refuses_any_link(make_service: Factory, run_dir: Path) -> None:
    task = run_dir / "task"
    (task / "real").mkdir()
    (task / "real" / "a.txt").write_text("inside " * 50)
    os.symlink(task / "real", task / "alias")
    os.symlink(task / "real" / "a.txt", task / "a-link.txt")
    svc = make_service()
    assert svc.capture_file("task/real/a.txt", "p")["status"] == "ok"
    assert svc.capture_file("task/alias/a.txt", "p")["status"] == "path_outside_task"
    assert svc.capture_file("task/a-link.txt", "p")["status"] == "path_outside_task"
    assert svc.capture_file(str(task / "real" / "a.txt"), "p")["source_path"] == "task/real/a.txt"


# 7. Visible words decide "implausibly short" ----------------------------------------------

def test_script_shell_is_short_and_zero_width_is_empty() -> None:
    nav = "".join(f'<a href="https://stats.example.org/indicators/category-{i}/overview">'
                  f'<img src="/static/icons/category-{i}.svg"></a>' for i in range(8))
    data = ",".join(str(1000 + i) for i in range(3000))
    shell = (f'<!doctype html><html><head><title>GDP</title><script type="application/json">'
             f'{{"gdp":[{data}]}}</script></head><body><nav>{nav}</nav><div id="root"></div>'
             '</body></html>').encode()
    assert extract(shell, "text/html").status is CaptureStatus.SHORT_TEXT
    zero = ("<html><body><p>" + "\u200b" * 300 + "</p></body></html>").encode()
    assert extract(zero, "text/html").status is CaptureStatus.EMPTY_TEXT
    hidden_only = ("<html><body><div hidden>" + "Revenue was 9 billion. " * 30
                   + "</div></body></html>").encode()
    assert extract(hidden_only, "text/html").status is CaptureStatus.EMPTY_TEXT


def test_shell_is_loud_without_a_browser(make_service: Factory) -> None:
    page = b"<html><body><nav><a href='https://x.example.org/a'><img src='/i.svg'></a></nav></body></html>"
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], page)})
    r = make_service(t).capture("https://app.example.org/", "probe")
    assert r["status"] in ("short_text", "empty_text")
    assert "browser_fallback_unavailable" in r["warnings"]


# 8. SEC amendments ------------------------------------------------------------------------

def test_sec_facts_form_keeps_amendments_labelled(make_service: Factory) -> None:
    filings = [FilingRef("0000999999-23-000001", 999999, "Invented Co", "10-K", "2023-02-01",
                         None, "a.htm"),
               FilingRef("0000999999-23-000009", 999999, "Invented Co", "10-K/A", "2023-06-01",
                         None, "b.htm")]
    concept = {"units": {"USD": [
        {"val": 100, "end": "2022-12-31", "start": "2022-01-01", "form": "10-K",
         "accn": filings[0].accession, "filed": "2023-02-01"},
        {"val": 80, "end": "2022-12-31", "start": "2022-01-01", "form": "10-K/A",
         "accn": filings[1].accession, "filed": "2023-06-01"}]}}
    routes: dict[str, object] = {
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000999999/us-gaap/Revenues.json":
            Reply(200, [], json.dumps(concept).encode())}
    for f in filings:
        routes[f.document_url()] = Reply(200, [("Content-Type", "text/html")], html(FILL))
    svc = make_service(FakeTransport(routes), sec_backend=FakeSec(filings))
    out = svc.sec_facts("INVT", "Revenues", filed_before="2024-01-01", form="10-K")
    assert {(f["value"], f["form"]) for f in out["facts"]} == {(100, "10-K"), (80, "10-K/A")}
    assert any("amendments" in w for w in out["warnings"])


# 9. A Content-Length that is not ASCII digits ---------------------------------------------

@pytest.mark.parametrize("length", ["²", "١٢", "12 34", "-5"])
def test_odd_content_length_is_typed_and_receipted(make_service: Factory, run_dir: Path,
                                                   length: str) -> None:
    t = FakeTransport({"https://example.org/": Reply(
        200, [("Content-Type", "text/html"), ("Content-Length", length)], html(FILL))})
    r = make_service(t).capture("https://example.org/", "probe")
    assert r["status"] == "transport_error"
    assert (run_dir / "store" / "receipts" / f"{r['receipt_id']}.json").exists()


def test_unexpected_fault_still_receipted(make_service: Factory, run_dir: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(FakeTransport({"https://example.org/": Reply(200, [], html(FILL))}))

    def boom(*_a: Any, **_k: Any) -> Any:
        raise KeyError("simulated fault")

    monkeypatch.setattr(svc.fetcher, "fetch", boom)
    r = svc.capture("https://example.org/", "probe")
    assert r["status"] == "internal_error" and "KeyError" in r["detail"]
    assert (run_dir / "store" / "receipts" / f"{r['receipt_id']}.json").exists()


# 10. Hidden and invisible text and merged cells: kept, ranged, warned -------------------

def test_hidden_html_text_kept_with_range_and_quote_warning(make_service: Factory,
                                                           run_dir: Path) -> None:
    page = html("<span style='display:none'>Revenue was $9 billion in 2024.</span>"
                "<p hidden>Margin was 77 percent.</p>" + FILL)
    t = FakeTransport({"https://example.org/": Reply(200, [("Content-Type", "text/html")], page)})
    r = make_service(t).capture("https://example.org/", "probe")
    text = api.read_text(run_dir, r["receipt_id"])
    hidden = [x for x in r["flagged_ranges"] if x["kind"] == "hidden"]
    assert len(hidden) == 2
    assert "Revenue was $9 billion" in text[hidden[0]["start"]:hidden[0]["end"]]
    checked = api.check_quote(run_dir, r["receipt_id"], "Revenue was $9 billion in 2024.")
    assert checked["found"] and checked["warnings"] and checked["matches"][0]["regions"] == ["hidden"]
    plain = api.check_quote(run_dir, r["receipt_id"], "Invented commentary sentence")
    assert plain["found"] and plain["warnings"] == []


def test_merged_cell_table_flagged(make_service: Factory, run_dir: Path) -> None:
    table = ("<table><tr><th>Region</th><th>2023</th><th>2024</th></tr>"
             "<tr><td rowspan=2>North</td><td>10</td><td>11</td></tr>"
             "<tr><td>12</td><td>13</td></tr><tr><td>South</td><td>5</td><td>6</td></tr></table>")
    t = FakeTransport({"https://example.org/": Reply(200, [("Content-Type", "text/html")],
                                                    html(table + FILL))})
    r = make_service(t).capture("https://example.org/", "probe")
    assert any(x["kind"] == "table_merged_cells" for x in r["flagged_ranges"])
    checked = api.check_quote(run_dir, r["receipt_id"], "| 12 | 13 |")
    assert checked["found"] and "table_merged_cells" in checked["matches"][0]["regions"]


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


def test_invisible_and_white_pdf_text_flagged() -> None:
    filler = b"(" + b"Visible annual report text about the company and its results. " * 3 + b") Tj T* "
    ops = (b"BT /F1 10 Tf 12 TL 72 720 Td " + filler + b"(Revenue was 1,250 million.) Tj T* "
           b"3 Tr (Revenue was 9,999 million.) Tj T* 0 Tr 1 1 1 rg (Margin was 77 percent.) Tj ET")
    result = extract(_pdf(ops), "application/pdf")
    text = result.text or ""
    assert "9,999" in text and "77 percent" in text and "1,250" in text  # nothing deleted
    kinds = {r["kind"]: text[r["start"]:r["end"]] for r in result.flagged}
    assert "9,999" in kinds["invisible_pdf_text"] and "77 percent" in kinds["white_pdf_text"]
    assert "1,250" not in kinds["invisible_pdf_text"] + kinds["white_pdf_text"]
    warned = find_quote(text, "Revenue was 9,999 million.", result.flagged)
    assert warned["found"] and warned["warnings"]


def test_truncated_pdf_is_loud() -> None:
    pdf = make_pdf([["Page one revenue 100 " * 10], ["Page two revenue 200 " * 10]])
    result = extract(pdf[: int(len(pdf) * 0.55)], "application/pdf")
    assert result.status is not CaptureStatus.OK


# Suspected items --------------------------------------------------------------------------

def test_as_of_ticker_lookup_warned(make_service: Factory, run_dir: Path) -> None:
    filing = FilingRef("0000999999-23-000001", 999999, "Invented Co", "10-K", "2023-02-01",
                       None, "a.htm")
    svc = make_service(FakeTransport(), sec_backend=FakeSec([filing]))
    assert svc.sec_filings("INVT")["warnings"] == []
    set_as_of(run_dir, "2024-01-01")
    assert any("today's ticker" in w for w in svc.sec_filings("INVT")["warnings"])
    assert not any("today's ticker" in w for w in svc.sec_filings("0000999999")["warnings"])


def test_archive_hop_must_be_https(make_service: Factory, run_dir: Path) -> None:
    url = "https://stats.example.org/b"
    set_as_of(run_dir, "2024-01-01")
    rows = [["timestamp", "original", "statuscode", "mimetype", "digest"],
            ["20231231120000", url, "200", "text/html", "D"]]
    plain = f"http://web.archive.org/web/20231230000000id_/{url}"
    routes = {cdx_query_url(url, "2024-01-01"): Reply(200, [], json.dumps(rows).encode()),
              f"https://web.archive.org/web/20231231120000id_/{url}": Reply(302, [("Location", plain)]),
              plain: Reply(200, [("Content-Type", "text/html")], html(FILL))}
    svc = make_service(FakeTransport(routes))
    assert svc.capture(url, "probe")["status"] == "no_snapshot_before_as_of"


def test_exact_search_normalises_like_check_quote(make_service: Factory) -> None:
    page = html("<p>Output was 12 500 tonnes and “good”.</p>" + FILL)
    t = FakeTransport({"https://example.org/": Reply(200, [("Content-Type", "text/html")], page)})
    svc = make_service(t)
    r = svc.capture("https://example.org/", "probe")
    hits = svc.search_store("12 500 tonnes", mode="exact")["hits"]
    assert hits and svc.read(r["sha256"], hits[0]["start"], hits[0]["end"] - hits[0]["start"])[
        "text"] == "12 500 tonnes"
    assert svc.search_store('"good"', mode="exact")["hits"]


def test_fullwidth_dots_refused_before_resolution() -> None:
    for url in ("http://127。0。0。1/", "http://０x7f.1/"):
        with pytest.raises(PolicyRefusal):
            parse_url(url)
