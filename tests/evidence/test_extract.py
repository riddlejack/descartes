"""Extraction keeps everything, decodes only allowlisted charsets, and is loud when
text is empty or implausibly short."""

from __future__ import annotations

from conftest import html, make_pdf

from stemma.evidence.errors import CaptureStatus
from stemma.evidence.extract import extract, sniff_content_type

TABLE_PAGE = html(
    "<nav>Home | About</nav><h1>Library fines, invented city</h1>"
    "<p>Fines collected fell sharply after the change.</p>"
    "<table><tr><th>Year</th><th>Fines ($)</th><th>Records</th></tr>"
    "<tr><td>2022</td><td>1,204.5</td><td>380</td></tr>"
    "<tr><td>2023</td><td>728</td><td>135</td></tr></table>"
    "<h2>Method</h2><p>Effect 0.41 (95% interval -0.24 to 0.84), p = 0.064.</p>"
    "<aside>Sidebar note: 99 branches.</aside><footer>Footer figure 2,718</footer>"
    + "<p>Filler paragraph about the invented city library system.</p>" * 5
)


def test_html_keeps_every_figure_and_drops_scripts() -> None:
    result = extract(TABLE_PAGE, "text/html; charset=utf-8")
    assert result.status is CaptureStatus.OK
    text = result.text or ""
    for figure in ("1,204.5", "380", "728", "135", "0.41", "-0.24", "0.84", "0.064",
                   "99 branches", "2,718", "Home | About"):
        assert figure in text, figure
    assert "SCRIPT-TEXT" not in text and "color:red" not in text
    assert "| 2022 | 1,204.5 | 380 |" in text  # a table row stays on one line
    assert result.extractor["name"] == "markdownify"


def test_outline_offsets_point_at_headings() -> None:
    result = extract(TABLE_PAGE, "text/html")
    text = result.text or ""
    titles = [o["title"] for o in result.outline]
    assert titles == ["Library fines, invented city", "Method"]
    for item in result.outline:
        assert text[item["start"]:].startswith("#")
        assert item["title"] in text[item["start"]:item["end"]]
    assert result.outline[0]["end"] == len(text)  # h1 runs to the end


def test_utf7_refused() -> None:
    body = b"<html><head><meta charset='utf-7'></head><body>+ADw-script+AD4-x</body></html>"
    result = extract(body, "text/html")
    assert result.status is CaptureStatus.UNSUPPORTED_CHARSET
    assert result.text is None


def test_header_charset_wins_and_latin1_decodes() -> None:
    body = "<html><body><p>Café revenue £ 12</p></body></html>".encode("cp1252")
    result = extract(body, "text/html; charset=windows-1252")
    assert "Café" in (result.text or "")
    assert result.charset == "windows-1252"


def test_meta_charset_used() -> None:
    body = "<html><head><meta charset=\"iso-8859-1\"></head><body>naïve</body></html>".encode("latin-1")
    assert "naïve" in (extract(body, "text/html").text or "")


def test_empty_page_is_loud() -> None:
    result = extract(b"<html><head><script>render()</script></head><body></body></html>", "text/html")
    assert result.status is CaptureStatus.EMPTY_TEXT
    assert "extraction_empty" in result.warnings


def test_short_page_is_loud_but_kept() -> None:
    result = extract(html("<div id='app'>Loading</div>") + b" " * 60_000, "text/html")
    assert result.status is CaptureStatus.SHORT_TEXT
    assert "Loading" in (result.text or "")


def test_pdf_pages_and_offsets() -> None:
    pdf = make_pdf([["Annual report of Invented Co", "Revenue rose to 1,204.5 million",
                     "Further invented commentary on the year, kept for length."],
                    ["Second page (with parens) and more invented text for its length.",
                     "A last invented line on the second page, also kept for length."]])
    result = extract(pdf, "application/octet-stream")  # a mislabelled PDF is still a PDF
    assert result.status is CaptureStatus.OK and result.pages == 2
    text = result.text or ""
    assert "1,204.5 million" in text and "(with parens)" in text
    p1, p2 = result.outline
    assert p1["anchor"] == "p1" and text[p2["start"]:p2["end"]] == (
        "Second page (with parens) and more invented text for its length.\n"
        "A last invented line on the second page, also kept for length.")
    assert result.extractor["name"] == "pypdfium2"


def test_pdf_without_text_layer_is_loud() -> None:
    result = extract(make_pdf([[]]), "application/pdf")
    assert result.status is CaptureStatus.EMPTY_TEXT
    assert any("no_text_layer" in w for w in result.warnings)


def test_scanned_looking_pdf_is_loud() -> None:
    # 40 pages holding only page numbers: text kept, but marked short.
    result = extract(make_pdf([[f"Page {n}"] for n in range(1, 41)]), "application/pdf")
    assert result.status is CaptureStatus.SHORT_TEXT and "Page 40" in (result.text or "")
    assert any("scanned" in w for w in result.warnings)


def test_broken_pdf_is_typed() -> None:
    result = extract(b"%PDF-1.4\nnot really a pdf", "application/pdf")
    assert result.status is CaptureStatus.EXTRACTION_FAILED


def test_unsupported_type() -> None:
    result = extract(b"\x89PNG\r\n\x1a\n....", "image/png")
    assert result.status is CaptureStatus.UNSUPPORTED_CONTENT_TYPE


def test_plain_text_kept_verbatim() -> None:
    result = extract(b"a,b\r\n1,2\r\n", "text/csv")
    assert result.text == "a,b\n1,2\n"


def test_sniffing() -> None:
    assert sniff_content_type(None, b"%PDF-1.7 ...") == "application/pdf"
    assert sniff_content_type("application/octet-stream", b"<!DOCTYPE html><html>") == "text/html"
    assert sniff_content_type(None, b"hello", "notes.txt") == "text/plain"
