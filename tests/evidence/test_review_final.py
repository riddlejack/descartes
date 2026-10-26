"""Regression tests for the Codex recheck of 0af9227 (the last fix round). Each
reproduces the attack."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeTransport, Reply, html

from stemma.evidence import limits
from stemma.evidence.errors import CaptureStatus, EvidenceError
from stemma.evidence.extract import extract
from stemma.evidence.htmltext import html_to_text
from stemma.evidence.quotes import find_quote
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
FILL = "<p>" + "Invented commentary sentence that pads the page to length. " * 12 + "</p>"


# 1. Number tokens decide quote edges ------------------------------------------------------

@pytest.mark.parametrize("text, quote", [
    ("a dose of 1e-3 units", "-3 units"),
    ("a dose of 1e-3 units", "3 units"),
    ("a dose of 1e-3 units", "dose of 1e"),
    ("a dose of 1.5E+12 units", "+12 units"),
    ("rose by .5 million", "5 million"),
    ("a loss of -$5 million", "$5 million"),
    ("revenue of $5 million", "5 million"),
    ("the rate was 25%", "the rate was 25"),
    ("the price was 5€", "the price was 5"),
    ("change was -3 points", "change was -"),
    ("added 1,250 jobs", "250 jobs"),
    ("total was 1.5", "total was 1."),
    ("rate of 0.25%", "25%"),
    ("margin of 12 500 units", "500 units"),
])
def test_edge_inside_number_token_refused(text: str, quote: str) -> None:
    assert find_quote(text, quote)["found"] is False, (text, quote)


@pytest.mark.parametrize("text, quote", [
    ("a dose of 1e-3 units", "1e-3 units"),
    ("a loss of -$5 million", "-$5 million"),
    ("the rate was 25% this year", "the rate was 25%"),
    ("The total was 1,250. Next year", "The total was 1,250."),
    ("COVID-19 cases rose", "19 cases rose"),
    ("between 2020-2021 sales", "2021 sales"),
    ("it cost €12 million", "€12 million"),
])
def test_whole_number_tokens_found(text: str, quote: str) -> None:
    assert find_quote(text, quote)["found"] is True, (text, quote)


def test_number_tokens() -> None:
    from stemma.evidence.quotes import number_tokens

    text = "loss of -$5.2 million, dose 1e-3, rate 25%, COVID-19, 2020-2021, .5"
    found = [text[a:b] for a, b in number_tokens(text)]
    assert found == ["-$5.2", "1e-3", "25%", "19", "2020", "2021", ".5"]


# 2. Every element boundary next to a digit is split ---------------------------------------

@pytest.mark.parametrize("markup, fused", [
    ("<p><span style='display:block'>100</span><span style='display:block'>.200</span></p>",
     "100.200"),
    ("<p><span>Revenue</span><span>100</span></p>", "Revenue100"),
    ("<p><span>12</span><span>-5</span></p>", "12-5"),
    ("<p><span>100,</span><span>200</span></p>", "100,200"),
])
def test_boundary_next_to_digit_split(markup: str, fused: str) -> None:
    text, flagged, _ = html_to_text(markup)
    assert fused not in text
    assert any(r["kind"] == "element_split" for r in flagged)
    assert find_quote(text, fused, flagged)["found"] is False


def test_inline_xbrl_tags_do_not_split() -> None:
    text, flagged, _ = html_to_text(
        "<p>net sales of $<ix:nonFraction name='us-gaap:Revenues'>391.0</ix:nonFraction> "
        "billion</p>")
    assert "net sales of $391.0 billion" in text and not flagged
    assert find_quote(text, "$391.0 billion", flagged)["found"]


# 3. No in-band markers: source characters cannot switch the ranges off --------------------

@pytest.mark.parametrize("planted", ["", "", "", "", "",
                                     "\U000f0000\U000f0001\U000f0002\U000f0003\U000f0004"])
def test_marker_characters_in_source_do_not_disable_ranges(planted: str) -> None:
    markup = (f"<p>{planted}</p><p><span>1</span><span>250</span> units</p>"
              "<p hidden>Revenue was 9 billion.</p>")
    text, flagged, warnings = html_to_text(markup)
    kinds = {r["kind"] for r in flagged}
    assert {"element_split", "hidden"} <= kinds, (flagged, warnings)
    assert find_quote(text, "250 units", flagged)["found"] is False
    assert planted in text  # the page's own characters are kept


# 4. Search hits name the receipt of the text searched -------------------------------------

def test_search_hit_receipt_matches_variant(make_service: Factory) -> None:
    body = ("<html><body><p>" + "Prix élevé de 5 euros. " * 20 + "</p></body></html>").encode(
        "latin-1")
    t = FakeTransport({
        "https://a.example.org/": Reply(200, [("Content-Type", "text/html; charset=iso-8859-1")], body),
        "https://b.example.org/": Reply(200, [("Content-Type", "text/html; charset=koi8-r")], body)})
    svc = make_service(t, cache_dir=None)
    svc.capture("https://a.example.org/", "latin-1")
    koi = svc.capture("https://b.example.org/", "koi8-r")
    hits = svc.search_store("de 5 euros", mode="exact", sha256=koi["text_id"])["hits"]
    assert hits and all(h["receipt_id"] == koi["receipt_id"] for h in hits)
    quote = svc.read(koi["text_id"], hits[0]["start"], hits[0]["end"] - hits[0]["start"])["text"]
    assert svc.check_quote(hits[0]["receipt_id"], quote)["found"]


# 5. The browser fallback is unavailable in this version ------------------------------------

def test_enabling_browser_fallback_is_refused(run_dir: Path) -> None:
    (run_dir / "run.json").write_text(json.dumps({"run_id": "t", "as_of": None,
                                                 "evidence": {"browser_fallback": True}}))
    svc = EvidenceService(run_dir, transport=FakeTransport(), cache_dir=None, throttle=False)
    with pytest.raises(EvidenceError) as info:
        svc.capture("https://example.org/", "probe")
    assert info.value.code == "browser_fallback_unavailable"


def test_loud_page_says_fallback_unavailable(make_service: Factory) -> None:
    shell = b"<html><body><div id='root'></div></body></html>"
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], shell)})
    r = make_service(t).capture("https://app.example.org/", "probe")
    assert r["status"] == "empty_text" and r["kind"] == "web"
    assert "browser_fallback_unavailable" in r["warnings"]


# 6. The PDF worker's pipes are capped while reading -----------------------------------------

def test_pdf_worker_flood_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.evidence import extract as extract_mod

    monkeypatch.setattr(limits, "PDF_MAX_OUTPUT_BYTES", 100_000)
    flood = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
    monkeypatch.setattr(extract_mod, "_pdf_worker_argv", lambda: [sys.executable, "-c", flood])
    result = extract(b"%PDF-1.4\n%%EOF\n", "application/pdf")
    assert result.status is CaptureStatus.EXTRACTION_FAILED
    assert "output passed its byte limit" in (result.detail or "")


def test_pdf_worker_stderr_flood_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.evidence import extract as extract_mod

    flood = "import sys\nwhile True: sys.stderr.write('x' * 65536)"
    monkeypatch.setattr(extract_mod, "_pdf_worker_argv", lambda: [sys.executable, "-c", flood])
    result = extract(b"%PDF-1.4\n%%EOF\n", "application/pdf")
    assert result.status is CaptureStatus.EXTRACTION_FAILED


def test_pdf_text_limit_checked_per_character() -> None:
    from stemma.evidence.pdfworker import TooMuchText, _CharBudget

    budget = _CharBudget(10)
    for _ in range(10):
        budget.take()
    with pytest.raises(TooMuchText):
        budget.take()


def test_html_fill_still_ok(make_service: Factory) -> None:
    t = FakeTransport({"https://example.org/": Reply(200, [("Content-Type", "text/html")], html(FILL))})
    assert make_service(t).capture("https://example.org/", "ok")["status"] == "ok"
