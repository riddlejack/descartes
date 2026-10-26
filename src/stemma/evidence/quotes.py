"""Exact quotation checks against stored text.

The documented normalisation (`QUOTE_NORMALISATION`), applied to both the quote
and the stored text before an exact, case-sensitive substring match:
1. every run of Unicode whitespace (spaces, tabs, line breaks, no-break spaces)
   becomes one space, and the quote is trimmed at both ends;
2. curly and typographic single quotation marks and apostrophes (‘ ’ ‚ ‛ ′ ‹ ›)
   become `'`; double ones (“ ” „ ‟ ″ « ») become `"`.
Nothing else changes: not case, not dashes, not digits, not Markdown marks.

A match also has to sit on token boundaries, and never start or end in the middle
of a number (contract 4):
- a letter or digit on both sides of either edge is a middle ("5%" in "15%");
- a digit, a number separator (`,` `.` `'` and the no-break and thin spaces) and a
  digit across an edge is a middle: "250 jobs" is not in "1,250 jobs", "$1" is not
  in "$1.5 billion", "25%" is not in "0.25%";
- a match starting at a digit right after a lone sign (`-`, `+`, `−`) starts inside
  that number: "3.2%" is not in "fell -3.2%" (but "2021" is in "2020-2021");
- no match may end just before a combining mark.
A match inside a region the extractor flagged (hidden or invisible text, a table
with merged cells) is still found, with a warning naming the region.
Offsets returned are code points in the stored text, before normalisation.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError

QUOTE_NORMALISATION = (
    "whitespace-runs-to-one-space;typographic-quotes-to-ascii;token-and-number-edges;v3"
)
# Invariant: characters that join the digit groups of one number.
NUMBER_SEPARATORS = frozenset(",.'\u00a0\u2009\u202f\u2019")
_SIGNS = frozenset("-+\u2212")

_QUOTE_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "‹": "'", "›": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
}


def normalise(text: str) -> tuple[str, list[int]]:
    """Normalised text, and for each of its characters the index in `text` it came
    from (a collapsed whitespace run maps to its first character)."""
    out: list[str] = []
    origin: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if not in_space:
                out.append(" ")
                origin.append(index)
                in_space = True
            continue
        in_space = False
        out.append(_QUOTE_MAP.get(char, char))
        origin.append(index)
    return "".join(out), origin


def _digit(text: str, index: int) -> bool:
    return 0 <= index < len(text) and text[index].isdigit()


def _inside_number(text: str, edge: int) -> bool:
    """Is the gap before `text[edge]` inside one number?"""
    if _digit(text, edge - 1) and _digit(text, edge):
        return True
    if (_digit(text, edge - 1) and 0 <= edge < len(text) and text[edge] in NUMBER_SEPARATORS
            and _digit(text, edge + 1)):
        return True
    return bool(edge >= 2 and text[edge - 1] in NUMBER_SEPARATORS and _digit(text, edge - 2)
                and _digit(text, edge))


def _on_boundaries(text: str, start: int, end: int) -> bool:
    if text[start].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    if _inside_number(text, start) or _inside_number(text, end):
        return False
    if (_digit(text, start) and start >= 1 and text[start - 1] in _SIGNS
            and (start == 1 or not text[start - 2].isalnum())):
        return False
    if end < len(text):
        following = text[end]
        if unicodedata.category(following).startswith("M"):
            return False
        if text[end - 1].isalnum() and following.isalnum():
            return False
    return True


_REGION_WARNINGS = {
    "hidden": "in text the page hides from readers (hidden HTML)",
    "invisible_pdf_text": "in PDF text that is drawn invisibly",
    "white_pdf_text": "in PDF text drawn in white",
    "table_merged_cells": "in a table with merged cells whose columns may be misaligned",
}


def find_quote(text: str, quote: str,
               flagged: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(quote, str) or not quote.strip():
        raise EvidenceError("empty_quote", "the quotation is empty")
    needle, _ = normalise(quote)
    needle = needle.strip(" ")
    haystack, origin = normalise(text)
    matches: list[dict[str, Any]] = []
    position = haystack.find(needle)
    total = 0
    while position != -1:
        start, end = origin[position], origin[position + len(needle) - 1] + 1
        if _on_boundaries(text, start, end):
            total += 1
            if len(matches) < limits.QUOTE_MAX_MATCHES:
                regions = sorted({str(r["kind"]) for r in flagged or []
                                  if int(r["start"]) < end and start < int(r["end"])})
                matches.append({"start": start, "end": end, "regions": regions})
        position = haystack.find(needle, position + 1)
    warnings = [f"match at {m['start']}-{m['end']} lies {_REGION_WARNINGS.get(kind, kind)}"
                for m in matches for kind in m["regions"]]
    return {
        "found": total > 0,
        "matches": matches,
        "match_count": total,
        "warnings": warnings,
        "normalisation": QUOTE_NORMALISATION,
    }
