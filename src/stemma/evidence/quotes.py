"""Exact quotation checks against stored text.

The documented normalisation (`QUOTE_NORMALISATION`), applied to both the quote
and the stored text before an exact, case-sensitive substring match:
1. every run of Unicode whitespace (spaces, tabs, line breaks, no-break spaces)
   becomes one space, and the quote is trimmed at both ends;
2. curly and typographic single quotation marks and apostrophes (‘ ’ ‚ ‛ ′ ‹ ›)
   become `'`; double ones (“ ” „ ‟ ″ « ») become `"`.
Nothing else changes: not case, not dashes, not digits, not Markdown marks. Offsets
returned are code points in the stored text, before normalisation.
"""

from __future__ import annotations

from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError

QUOTE_NORMALISATION = "whitespace-runs-to-one-space;typographic-quotes-to-ascii;v1"

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
        total += 1
        if len(matches) < limits.QUOTE_MAX_MATCHES:
            last = position + len(needle) - 1
            matches.append({"start": origin[position], "end": origin[last] + 1})
        position = haystack.find(needle, position + 1)
    return {
        "found": total > 0,
        "matches": matches,
        "match_count": total,
        "normalisation": QUOTE_NORMALISATION,
    }
