"""Exact quotation checks against stored text.

The documented normalisation (`QUOTE_NORMALISATION`), applied to both the quote
and the stored text before an exact, case-sensitive substring match:
1. every run of Unicode whitespace (spaces, tabs, line breaks, no-break spaces)
   becomes one space, and the quote is trimmed at both ends;
2. curly and typographic single quotation marks and apostrophes (‘ ’ ‚ ‛ ′ ‹ ›)
   become `'`; double ones (“ ” „ ‟ ″ « ») become `"`.
Nothing else changes: not case, not dashes, not digits, not Markdown marks.

A match also has to sit on token boundaries: if the quote begins with a letter or
digit, the stored character before it may not be a letter or digit; if it ends with
one, the character after it may not be a letter or digit; and no match may end just
before a combining mark. So "5% in the year" is not found in "rose 15% in the year"
and "was 1,2" is not found in "was 1,250". Offsets returned are code points in the
stored text, before normalisation.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError

QUOTE_NORMALISATION = (
    "whitespace-runs-to-one-space;typographic-quotes-to-ascii;token-boundaries;v2"
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


def _on_boundaries(text: str, start: int, end: int) -> bool:
    if text[start].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    if end < len(text):
        following = text[end]
        if unicodedata.category(following).startswith("M"):
            return False
        if text[end - 1].isalnum() and following.isalnum():
            return False
    return True


def find_quote(text: str, quote: str) -> dict[str, Any]:
    if not isinstance(quote, str) or not quote.strip():
        raise EvidenceError("empty_quote", "the quotation is empty")
    needle, _ = normalise(quote)
    needle = needle.strip(" ")
    haystack, origin = normalise(text)
    matches: list[dict[str, int]] = []
    position = haystack.find(needle)
    total = 0
    while position != -1:
        start, end = origin[position], origin[position + len(needle) - 1] + 1
        if _on_boundaries(text, start, end):
            total += 1
            if len(matches) < limits.QUOTE_MAX_MATCHES:
                matches.append({"start": start, "end": end})
        position = haystack.find(needle, position + 1)
    return {
        "found": total > 0,
        "matches": matches,
        "match_count": total,
        "normalisation": QUOTE_NORMALISATION,
    }
