"""Item anchors for SEC filings, on invented filing-shaped fixtures."""

from __future__ import annotations

from collections.abc import Callable

from conftest import FakeSec, FakeTransport, Reply

from stemma.evidence.sec import FilingRef
from stemma.evidence.sec_outline import applies_to, item_outline
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
FILLER = "<p>" + "Invented narrative sentence for length. " * 12 + "</p>"


def styled(text: str) -> str:
    """Headings as filings write them: styled spans in a div, no heading tags."""
    return f"<div><span style='font-weight:700'>{text}</span></div>"


TEN_K = (
    "<html><body><div>INVENTED CO</div><div>FORM 10-K</div>"
    "<table><tr><td><a href='#a'>Part I</a></td></tr>"
    "<tr><td><a href='#b'>Item 1.</a></td><td>Business</td><td>1</td></tr>"
    "<tr><td><a href='#c'>Item 1A.</a></td><td>Risk Factors</td><td>5</td></tr>"
    "<tr><td><a href='#d'>Item 7.</a></td><td>Management's Discussion</td><td>20</td></tr></table>"
    + styled("PART I") + styled("Item 1.    Business") + FILLER
    + "<p>Item 7 of this report describes results; see also Item 1A.</p>"
    + styled("Item 1A.    Risk Factors") + FILLER
    + styled("PART II") + styled("ITEM 7.") + styled("MANAGEMENT'S DISCUSSION AND ANALYSIS")
    + FILLER + "<p>Net sales were 1,204.5 million in the invented year.</p>"
    + styled("Item 7A. Quantitative and Qualitative Disclosures About Market Risk") + FILLER
    + styled("Item 8. Financial Statements and Supplementary Data") + FILLER
    + "</body></html>"
).encode()

TEN_Q = (
    "<html><body><div>FORM 10-Q</div>"
    "<p>PART I</p><p>Item 1. Financial Statements 3</p><p>Item 2. Management's Discussion 12</p>"
    "<p>PART II</p><p>Item 1. Legal Proceedings 20</p><p>Item 1A. Risk Factors 21</p>"
    + styled("PART I — FINANCIAL INFORMATION") + styled("Item 1. Financial Statements") + FILLER
    + styled("Item 2. Management's Discussion and Analysis") + FILLER
    + styled("PART II — OTHER INFORMATION") + styled("Item 1. Legal Proceedings")
    + "<p>No invented proceedings are pending.</p>" + FILLER
    + styled("Item 1A. Risk Factors") + FILLER + "</body></html>"
).encode()

EIGHT_K = (
    "<html><body><div>FORM 8-K</div>"
    + styled("Item 2.02 Results of Operations and Financial Condition.")
    + "<p>On an invented date the company announced invented results.</p>" + FILLER
    + styled("Item 9.01 Financial Statements and Exhibits.") + FILLER + "</body></html>"
).encode()


def capture(make_service: Factory, form: str, body: bytes) -> tuple[EvidenceService, dict[str, object]]:
    ref = FilingRef("0000999999-24-000077", 999999, "Invented Co", form, "2024-03-01", None,
                    "inv.htm")
    svc = make_service(FakeTransport({ref.document_url(): Reply(
        200, [("Content-Type", "text/html")], body)}), sec_backend=FakeSec([ref]))
    return svc, svc.sec_capture(ref.accession)


def test_ten_k_items_skip_contents_and_cross_references(make_service: Factory) -> None:
    svc, r = capture(make_service, "10-K", TEN_K)
    outline = svc.outline(str(r["sha256"]))["outline"]
    anchors = [o["anchor"] for o in outline if o["kind"].startswith("sec_")]
    assert anchors == ["part-i", "item-1", "item-1a", "part-ii", "item-7", "item-7a", "item-8"]
    section = svc.read_section(str(r["sha256"]), "item-7")
    assert section["text"].startswith("ITEM 7.")
    assert "MANAGEMENT'S DISCUSSION" in section["text"] and "1,204.5 million" in section["text"]
    assert "Quantitative" not in section["text"]  # ends at Item 7A
    item1 = svc.read_section(str(r["sha256"]), "item-1")["text"]
    assert "Item 7 of this report" in item1  # a cross-reference is not a heading
    part2 = svc.read_section(str(r["sha256"]), "part-ii")["text"]
    assert "ITEM 7." in part2 and "Item 8." in part2


def test_ten_q_parts_disambiguate_items(make_service: Factory) -> None:
    svc, r = capture(make_service, "10-Q", TEN_Q)
    outline = svc.outline(str(r["sha256"]))["outline"]
    anchors = [o["anchor"] for o in outline if o["kind"] == "sec_item"]
    assert anchors == ["part-i-item-1", "item-2", "part-ii-item-1", "item-1a"]
    legal = svc.read_section(str(r["sha256"]), "part-ii-item-1")["text"]
    assert legal.startswith("Item 1. Legal Proceedings") and "No invented proceedings" in legal
    first = next(o for o in outline if o["anchor"] == "part-i-item-1")
    assert first["start"] > next(o for o in outline if o["anchor"] == "part-i")["start"]


def test_eight_k_items(make_service: Factory) -> None:
    svc, r = capture(make_service, "8-K", EIGHT_K)
    anchors = [o["anchor"] for o in r["outline"] if o["kind"] == "sec_item"]  # type: ignore[union-attr]
    assert anchors == ["item-2.02", "item-9.01"]
    text = svc.read_section(str(r["sha256"]), "item-2.02")["text"]
    assert "invented results" in text and "9.01" not in text


def test_other_forms_and_missing_items(make_service: Factory) -> None:
    assert applies_to("10-K/A") and applies_to("8-K") and not applies_to("DEF 14A")
    _svc, r = capture(make_service, "10-K", b"<html><body>" + FILLER.encode() * 3 + b"</body></html>")
    assert any(w.startswith("sec_items_not_found") for w in r["warnings"])  # type: ignore[union-attr]
    _svc, r = capture(make_service, "DEF 14A", TEN_K)
    assert not any(o["kind"] == "sec_item" for o in r["outline"])  # type: ignore[union-attr]


def test_item_outline_offsets_are_line_starts() -> None:
    text = "PART I\n\n**Item 1.  Business**\n\nWords.\n\n# Item 1A. Risk Factors\n\nMore.\n"
    out = item_outline(text)
    for entry in out:
        assert entry["start"] == 0 or text[entry["start"] - 1] == "\n"
    assert [e["anchor"] for e in out] == ["part-i", "item-1", "item-1a"]
    assert out[1]["end"] == out[2]["start"] and out[2]["end"] == len(text)
