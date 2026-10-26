"""Figure tags in drafts and the deliverable (CONTRACTS section 5, "Tags").

Each figure is followed by `[[c:<first 12 hex of claim_id>]]`; a number that is not a
figure by `[[nc:<why>]]`. A segment is a sentence, list item, table cell or heading. A
number token is a run of digits with optional sign, separators, decimal point, percent
sign or scale suffix, found with every tag removed first. Code requires each segment
holding a number token to carry a tag; whether each tag covers each number is the
evidence auditor's judgment, and `nc` tags are the mechanical reader's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

C_TAG = re.compile(r"\[\[c:([0-9a-f]{12})\]\]")
NC_TAG = re.compile(r"\[\[nc:([^\]\n]+)\]\]")
ANY_TAG = re.compile(r"\[\[(?:c|nc):[^\]\n]*\]\]")
NUMBER_TOKEN = re.compile(
    r"[+\-−]?[$£€¥]?\d[\d,.  ']*(?:%|\s?(?:bn|mn|m|k|tn|trn|x))?",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'“(\[A-Z0-9])")


@dataclass(frozen=True)
class Segment:
    line: int  # 1-based line in the file
    kind: str  # heading | list_item | table_cell | sentence
    text: str

    @property
    def claim_tags(self) -> list[str]:
        return C_TAG.findall(self.text)

    @property
    def nc_tags(self) -> list[str]:
        return NC_TAG.findall(self.text)

    @property
    def number_tokens(self) -> list[str]:
        return NUMBER_TOKEN.findall(ANY_TAG.sub(" ", self.text))

    @property
    def tagged(self) -> bool:
        return bool(ANY_TAG.search(self.text))


def segments(markdown: str) -> list[Segment]:
    """Split markdown into segments. Fenced blocks are segments line by line."""
    out: list[Segment] = []
    para: list[tuple[int, str]] = []
    in_fence = False

    def flush() -> None:
        if not para:
            return
        first = para[0][0]
        text = " ".join(t.strip() for _, t in para)
        for sentence in _SENTENCE_END.split(text):
            if sentence.strip():
                out.append(Segment(first, "sentence", sentence.strip()))
        para.clear()

    for n, line in enumerate(markdown.splitlines(), start=1):
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            if line.strip():
                out.append(Segment(n, "sentence", line.strip()))
            continue
        if not line.strip():
            flush()
        elif _HEADING.match(line):
            flush()
            out.append(Segment(n, "heading", _HEADING.sub("", line).strip()))
        elif line.lstrip().startswith("|"):
            flush()
            if not _TABLE_RULE.match(line):
                cells = line.strip().strip("|").split("|")
                out.extend(Segment(n, "table_cell", c.strip()) for c in cells if c.strip())
        elif _LIST_ITEM.match(line):
            flush()
            out.append(Segment(n, "list_item", _LIST_ITEM.sub("", line).strip()))
        else:
            para.append((n, line))
    flush()
    return out


def untagged_number_segments(markdown: str) -> list[Segment]:
    return [s for s in segments(markdown) if s.number_tokens and not s.tagged]


def claim_tags(markdown: str) -> list[str]:
    """Every `c` tag's 12-hex prefix, in order of appearance."""
    return C_TAG.findall(markdown)
