"""Figure tags in drafts and the deliverable (CONTRACTS section 5, "Tags").

Each figure is followed by `[[c:<first 12 hex of claim_id>]]`; a number that is not a
figure by `[[nc:<why>]]`. A segment is a sentence, list item, table cell or heading. A
number token is a run of digits with optional sign, separators, decimal point, percent
sign or scale suffix, found with every tag removed first. Code requires each segment
holding a number token to carry a tag; whether each tag covers each number is the
evidence auditor's judgment, and `nc` tags are the mechanical reader's.

The splitter fails closed: when in doubt it makes more segments, so more numbers must
carry a tag. Every line is its own segment boundary; no character of a line outside a
fence marker, a list or heading marker, a table pipe or a table delimiter row is
dropped. Only `[[c:<12 lowercase hex>]]` and `[[nc:<reason>]]` are tags; anything else
that looks like one is malformed (`malformed_tags`), tags nothing and is not removed
before number tokens are found.
"""

from __future__ import annotations

import html
import re
import sys
import unicodedata
from dataclasses import dataclass


def _class_of(categories: frozenset[str]) -> str:
    """A regex character class body for every code point in the Unicode categories."""
    ranges: list[tuple[int, int]] = []
    for cp in range(sys.maxunicode + 1):
        if unicodedata.category(chr(cp)) in categories:
            if ranges and ranges[-1][1] == cp - 1:
                ranges[-1] = (ranges[-1][0], cp)
            else:
                ranges.append((cp, cp))
    return "".join(f"\\U{a:08x}" if a == b else f"\\U{a:08x}-\\U{b:08x}" for a, b in ranges)


# Numbers that are not decimal digits: vulgar fractions, superscripts, circled digits
# (category No) and letter numbers such as Roman numerals (Nl). `\d` covers Nd, the
# decimal digits of every script.
_OTHER_NUMBER = _class_of(frozenset({"No", "Nl"}))
_NUMERAL = rf"(?:\d|[{_OTHER_NUMBER}])"

# A tag's reason may not hold brackets, so an `nc` reason cannot swallow a `c` tag.
_NC_REASON = r"[^\[\]\n]*[^\[\]\s][^\[\]\n]*"
C_TAG = re.compile(r"\[\[c:([0-9a-f]{12})\]\]")
NC_TAG = re.compile(rf"\[\[nc:({_NC_REASON})\]\]")
ANY_TAG = re.compile(rf"\[\[(?:c:[0-9a-f]{{12}}|nc:{_NC_REASON})\]\]")
# Anything that starts like a tag: `[[`, c or nc in any case, optional spaces, `:`.
_TAG_LIKE = re.compile(r"\[\[\s*n?c\s*:[^\[\]\n]*(?:\]\]?)?", re.IGNORECASE)
NUMBER_TOKEN = re.compile(
    rf"[+\-−]?[$£€¥]?{_NUMERAL}(?:{_NUMERAL}|[,.\u2009\u202f'])*(?:%|\s?(?:bn|mn|m|k|tn|trn|x))?",
    re.IGNORECASE,
)

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
# CommonMark allows at most nine digits in an ordered-list marker (a logical rule of
# the format); a longer run is text and its number must be tagged.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")
_BLOCKQUOTE = re.compile(r"^\s{0,3}(?:>\s?)+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_CELL_PIPE = re.compile(r"(?<!\\)\|")
_HTML_CELL_OPEN = re.compile(r"<t[dh]\b[^>]*>", re.IGNORECASE)
_HTML_CELL_END = re.compile(r"</t[dh]\s*>|<t[dh]\b[^>]*>", re.IGNORECASE)
# A sentence ends at `.`, `!`, `?` or `…` with any closing quotes or brackets, followed
# by whitespace or by a capital or currency sign; at a full-width stop, which needs
# nothing after it; or at an HTML line break.
_SENTENCE_END = re.compile(
    r"[.!?…][\"'”’»)\]]*(?=\s|[A-Z$£€¥])|[。！？؟][\"'”’»)\]」』]*|(?i:<br\b[^>]*>)"
)


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
    def malformed_tags(self) -> list[str]:
        return [m.group(0) for m in _TAG_LIKE.finditer(self.text)
                if not ANY_TAG.fullmatch(m.group(0))]

    @property
    def number_tokens(self) -> list[str]:
        bare = ANY_TAG.sub(" ", self.text)
        found = NUMBER_TOKEN.findall(bare)
        # An HTML entity such as `&frac12;` renders as a number with no digit in it.
        found.extend(t for t in NUMBER_TOKEN.findall(html.unescape(bare)) if t not in found)
        return found

    @property
    def tagged(self) -> bool:
        """A strict tag is present and no malformed one is."""
        return bool(ANY_TAG.search(self.text)) and not self.malformed_tags


def _tag_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in ANY_TAG.finditer(text)]


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a < pos < b for a, b in spans)


def _sentences(text: str) -> list[str]:
    spans = _tag_spans(text)
    out: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        if _inside(m.start(), spans):
            continue
        out.append(text[start:m.end()])
        start = m.end()
    out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def _cells(row: str) -> list[str]:
    """Split a table row on unescaped pipes outside tags; outer pipes are dropped."""
    body = row.strip()
    body = body.removeprefix("|")
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    spans = _tag_spans(body)
    cells: list[str] = []
    start = 0
    for m in _CELL_PIPE.finditer(body):
        if _inside(m.start(), spans):
            continue
        cells.append(body[start:m.start()])
        start = m.end()
    cells.append(body[start:])
    return cells


def _html_pieces(text: str) -> list[tuple[bool, str]]:
    """Split text at HTML table cells: (is_cell, piece). No character is dropped."""
    pieces: list[tuple[bool, str]] = []
    pos = 0
    while (m := _HTML_CELL_OPEN.search(text, pos)) is not None:
        end = _HTML_CELL_END.search(text, m.end())
        stop = end.start() if end else len(text)
        pieces.append((False, text[pos:m.end()]))
        pieces.append((True, text[m.end():stop]))
        pos = stop
        if end and end.group(0).startswith("</"):
            pieces.append((False, end.group(0)))
            pos = end.end()
    pieces.append((False, text[pos:]))
    return pieces


def segments(markdown: str) -> list[Segment]:
    """Split markdown into segments, one or more per line.

    A table runs from a header row with `|` over a delimiter row until a blank line, a
    heading or a fence; inside it every line with `|` is split into cells. Fenced
    lines keep all their text and are split on `|` too, so a misread fence hides no
    cell.
    """
    out: list[Segment] = []

    def emit(n: int, text: str, kind: str) -> None:
        for is_cell, piece in _html_pieces(text):
            for sentence in _sentences(piece):
                out.append(Segment(n, "table_cell" if is_cell else kind, sentence))

    lines = markdown.splitlines()
    fence: str | None = None
    in_table = False
    skip_rule = False
    for i, raw in enumerate(lines):
        n = i + 1
        line = _BLOCKQUOTE.sub("", raw)
        if fence is not None:
            m = _FENCE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not m.group(2).strip():
                fence = None
            else:
                for cell in _cells(raw) if "|" in raw else [raw]:
                    emit(n, cell.strip(), "sentence")
            continue
        m = _FENCE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence, in_table = m.group(1), False
            continue
        if skip_rule:
            skip_rule = False
            continue
        if not line.strip():
            in_table = False
            continue
        if _HEADING.match(line):
            in_table = False
            emit(n, _HEADING.sub("", line, count=1).strip(), "heading")
            continue
        kind, content = "sentence", line
        if _LIST_ITEM.match(line):
            kind, content = "list_item", _LIST_ITEM.sub("", line, count=1)
        nxt = _BLOCKQUOTE.sub("", lines[i + 1]) if i + 1 < len(lines) else ""
        if "|" in content and not in_table and _TABLE_RULE.match(nxt):
            in_table, skip_rule = True, True
        if (in_table and "|" in content) or content.lstrip().startswith("|"):
            if not _TABLE_RULE.match(content):
                for cell in _cells(content):
                    emit(n, cell.strip(), "table_cell")
        else:
            emit(n, content.strip(), kind)
    return out


def untagged_number_segments(markdown: str) -> list[Segment]:
    return [s for s in segments(markdown) if s.number_tokens and not s.tagged]


def malformed_tags(markdown: str) -> list[tuple[int, str]]:
    """(line, raw text) of everything that looks like a tag but is not a strict one."""
    return [(n, m.group(0)) for n, line in enumerate(markdown.splitlines(), start=1)
            for m in _TAG_LIKE.finditer(line) if not ANY_TAG.fullmatch(m.group(0))]


def claim_tags(markdown: str) -> list[str]:
    """Every `c` tag's 12-hex prefix, in order of appearance."""
    return C_TAG.findall(markdown)
