"""Raised PDF markers (footnote numbers, superscripts) set apart from the text they
follow, so "2025.18" with a raised "18" is never read as one number."""

from __future__ import annotations

from test_review_codex import _pdf

from stemma.evidence.errors import CaptureStatus
from stemma.evidence.extract import extract
from stemma.evidence.quotes import find_quote

OPS = (b"BT /F1 10 Tf 12 TL 72 720 Td "
       b"(Visible annual report text about the company and its results, at length.) Tj T* "
       b"(The total available as of December 31, 2025.) Tj 6 Tf 4 Ts (18) Tj 10 Tf 0 Ts T* "
       b"(According to the agency, outlays were 1,250) Tj 6 Tf 4 Ts (3) Tj 10 Tf 0 Ts "
       b"( million in all.) Tj T* "
       b"(A real decimal stays whole: 2025.18 units.) Tj T* "
       b"(Text that continues after a marker) Tj 6 Tf 4 Ts (a) Tj 10 Tf 0 Ts (next) Tj ET")


def _extract() -> tuple[str, list[dict[str, object]]]:
    result = extract(_pdf(OPS), "application/pdf")
    assert result.status is CaptureStatus.OK
    assert any(w.startswith("raised_markers_separated") for w in result.warnings)
    return result.text or "", result.flagged


def test_marker_after_period_is_set_apart_and_sentence_quotable() -> None:
    text, flagged = _extract()
    assert "2025. 18" in text and "December 31, 2025.18" not in text
    sentence = find_quote(text, "The total available as of December 31, 2025.", flagged)
    assert sentence["found"] and sentence["warnings"] == []
    assert not find_quote(text, "December 31, 2025.18", flagged)["found"]
    marker = find_quote(text, "18", flagged)
    assert any("superscript" in m["regions"] for m in marker["matches"])


def test_marker_after_number_does_not_join_it() -> None:
    text, flagged = _extract()
    assert "1,250 3" in text and "1,2503" not in text
    assert find_quote(text, "outlays were 1,250", flagged)["found"]
    spanning = find_quote(text, "1,250 3", flagged)
    assert spanning["found"] and spanning["warnings"]  # across the split and into the marker


def test_real_decimal_and_following_text_kept() -> None:
    text, flagged = _extract()
    assert find_quote(text, "stays whole: 2025.18 units.", flagged)["found"]
    assert not any(text[int(f["start"]):int(f["end"])] == "2025.18" for f in flagged)
    assert "marker a next" in text  # a split after the marker too, nothing dropped
    kinds = sorted(str(f["kind"]) for f in flagged)
    assert kinds.count("superscript") == 3 and kinds.count("superscript_split") == 4


def test_line_start_footnote_text_not_flagged() -> None:
    ops = (b"BT /F1 10 Tf 12 TL 72 720 Td "
           b"(Body text of the report, written at the usual size for reading.) Tj ET "
           b"BT /F1 6 Tf 72 100 Td (18 According to the agency's annual statement.) Tj ET")
    result = extract(_pdf(ops), "application/pdf")
    assert not [f for f in result.flagged if f["kind"] == "superscript"]
    assert "18 According" in (result.text or "")
