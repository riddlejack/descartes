"""Exact quotation checks against stored text.

The documented normalisation (`QUOTE_NORMALISATION`), applied to both the quote
and the stored text before an exact, case-sensitive substring match:
1. every run of Unicode whitespace (spaces, tabs, line breaks, no-break spaces)
   becomes one space, and the quote is trimmed at both ends;
2. curly and typographic single quotation marks and apostrophes (‘ ’ ‚ ‛ ′ ‹ ›)
   become `'`; double ones (“ ” „ ‟ ″ « ») become `"`.
Nothing else changes: not case, not dashes, not digits, not Markdown marks.

A match also has to sit on word boundaries (a letter or digit on both sides of an
edge refuses it), and **no edge may fall strictly inside a number token**. The
stored text is tokenised once into numbers: an optional sign (not a hyphen joining
a word or range: "19" is its own token in "COVID-19", "2021" in "2020-2021"), an
optional currency prefix, digit groups joined by separators (`,` `.` `'`, no-break
and thin spaces) or a leading decimal point, an optional exponent, and an optional
percent, per-mille or currency suffix. So "250 jobs" is not in "1,250 jobs", "-3
units" not in "1e-3 units", "$5" not in "-$5", "25" not in "25%".
A match inside a region the extractor flagged (hidden or invisible text, a table
with merged cells) is still found, with a warning naming the region. A match may
not start or end at an `element_split` (where digits from two page elements met
and were split, because the page may show them joined); one spanning a split is
found with a warning.
Offsets returned are code points in the stored text, before normalisation.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from functools import lru_cache
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError

QUOTE_NORMALISATION = (
    "whitespace-runs-to-one-space;typographic-quotes-to-ascii;word-and-number-token-edges;v5"
)
# Invariant: characters that join the digit groups of one number.
NUMBER_SEPARATORS = ",.'\u00a0\u2009\u202f\u2019"
_SIGNS = "+\\-\u2212"
_CURRENCY = "".join(chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Sc")
_SEP_CLASS = re.escape(NUMBER_SEPARATORS)
_CUR_CLASS = re.escape(_CURRENCY)
# Invariant: one number token in stored text: an optional sign (only where it cannot
# be a hyphen joining a word or a range), an optional currency prefix, digit
# groups joined by separators or a leading decimal point, an optional exponent,
# and an optional percent, per-mille or currency suffix.
_NUMBER = re.compile(
    rf"(?:(?<![\w{_SEP_CLASS}])[{_SIGNS}])?"
    rf"(?:[{_CUR_CLASS}][{_SIGNS}]?)?"
    rf"(?:\d+(?:[{_SEP_CLASS}]\d+)*|(?<!\d)[.,]\d+)"
    rf"(?:[eE][{_SIGNS}]?\d+)?"
    rf"(?:[%\u2030\u2031{_CUR_CLASS}])?"
)


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


@lru_cache(maxsize=16)
def number_tokens(text: str) -> tuple[tuple[int, int], ...]:
    """Every number token in `text`, as (start, end) offsets, in order."""
    return tuple((m.start(), m.end()) for m in _NUMBER.finditer(text))


def _inside_a_number(tokens: tuple[tuple[int, int], ...], edge: int) -> bool:
    """Does `edge` fall strictly inside a number token?"""
    index = bisect_right(tokens, (edge, len(tokens) + 10**12)) - 1
    if index < 0:
        return False
    start, end = tokens[index]
    return start < edge < end


def _on_boundaries(text: str, start: int, end: int) -> bool:
    if text[start].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    tokens = number_tokens(text)
    if _inside_a_number(tokens, start) or _inside_a_number(tokens, end):
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
    "element_split": "across a place where digits from two page elements were split",
    "superscript": "in a raised marker (a footnote number or superscript in a PDF)",
    "superscript_split": "across the space put in before or after a raised PDF marker",
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
        splits = [r for r in flagged or [] if r["kind"] == "element_split"]
        at_split = any(start == int(r["end"]) or end == int(r["start"]) or
                       start == int(r["start"]) or end == int(r["end"]) for r in splits)
        if _on_boundaries(text, start, end) and not at_split:
            total += 1
            if len(matches) < limits.QUOTE_MAX_MATCHES:
                regions = sorted({str(r["kind"]) for r in flagged or []
                                  if int(r["start"]) < end and start < int(r["end"])})
                matches.append({"start": start, "end": end, "regions": regions})
        position = haystack.find(needle, position + 1)
    warnings = [f"match at {m['start']}-{m['end']} lies {_REGION_WARNINGS.get(kind, kind)}"
                for m in matches for kind in m["regions"]]
    # Every recorded split is an edge the checker is told about: a split can never
    # silently make a smaller number quotable.
    for m in matches:
        for r in flagged or []:
            if r["kind"] == "superscript_split" and (
                    m["start"] == int(r["end"]) or m["end"] == int(r["start"])):
                warnings.append(f"match at {m['start']}-{m['end']} starts or ends at the space "
                                "put in beside a raised PDF marker (a recorded split); check the page")
    return {
        "found": total > 0,
        "matches": matches,
        "match_count": total,
        "warnings": warnings,
        "normalisation": QUOTE_NORMALISATION,
    }
