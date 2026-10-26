"""HTML and XML to stored text: markdownify 1.2.3 over the whole document.

The extractor never deletes or joins (contract 4). Only `<script>` and `<style>`
are removed. Before conversion:
- superscripts and subscripts get visible markers, `^...^` and `~...~`, so
  "$5<sup>1</sup> billion" is stored as "$5^1^ billion", never "$51 billion";
- block-like elements markdownify would run together (header, footer, figure,
  nav, aside, address, details, summary, option, noscript, template, textarea and
  the like) and MathML parts are separated by a space;
- in XML documents, adjacent elements whose text would touch letter-to-letter or
  digit-to-digit are separated by a space;
- **element splits.** At every boundary between two elements' text where the
  characters would touch and either one is a digit, or a separator (`.` `,` `-`)
  next to a digit, a space is put in and the split is recorded. CSS can draw the
  two apart (`display:block`) or together, and code cannot tell which, so this
  fails in the direction that evidences nothing: a split real number makes a quote
  fail, a fused one would invent a figure. `check_quote` refuses a match with an
  edge at a split. One exception: inline XBRL tags (`ix:` elements with no style or
  class of their own) only tag a figure and never change how it is drawn, so text
  joins across one when the character on the outer side is not number-forming
  (not a digit, sign, or separator next to a digit): "$<ix:nonFraction>394,328
  </ix:nonFraction>" stays "$394,328", while two adjacent ix figures, or a digit,
  sign or separator touching one, still split;
- hidden elements (the `hidden` attribute, inline `display:none` or
  `visibility:hidden`, `<template>`) keep their text, and its range is recorded;
- tables with merged cells (`rowspan` or `colspan` above 1) keep their text, and
  the table's range is recorded, since markdownify does not re-align their cells.

**No in-band markers survive.** Ranges are found with marker characters placed
before conversion and removed after. The markers are chosen per document from the
Supplementary Private Use Area, among code points that occur nowhere in the page's
text or attributes, so a page's own characters can never be read as markers or
switch the ranges off. The stored text holds only the page's characters and the
separators above; the ranges are offset lists on the receipt.
"""

from __future__ import annotations

import re
import warnings as warnings_module
from dataclasses import dataclass
from typing import Any

# Elements a browser renders as separate boxes that markdownify would run together.
_SEPARATE = frozenset({
    "header", "footer", "figure", "figcaption", "main", "nav", "aside", "address",
    "details", "summary", "center", "fieldset", "legend", "hgroup", "search", "menu",
    "dialog", "option", "optgroup", "button", "meter", "output", "progress",
    "noscript", "template", "textarea", "caption", "math", "title", "head", "body",
})
# MathML containers whose children sit side by side: a space between each pair.
_MATH_ROWS = frozenset({"math", "mrow", "mstyle", "mpadded", "mphantom", "menclose",
                        "mtd", "semantics", "mfenced"})
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",
                           re.IGNORECASE)
_TABLE_STRUCTURE = frozenset({"table", "thead", "tbody", "tfoot", "tr"})
# Invariant: separators that belong to a number when a digit is beside them.
_NUMBER_JOINERS = ".,-"
# Invariant: where marker characters are drawn from (Supplementary Private Use Area-A).
_MARKER_FIRST, _MARKER_LAST = 0xF0000, 0xFFFFD


@dataclass(frozen=True)
class _Markers:
    hidden_open: str
    hidden_close: str
    table_open: str
    table_close: str
    split: str

    @property
    def all(self) -> tuple[str, ...]:
        return (self.hidden_open, self.hidden_close, self.table_open, self.table_close,
                self.split)


def _choose_markers(soup: Any) -> _Markers:
    """Five private-use characters that occur nowhere in the page's text or
    attribute values (everything markdownify could write out)."""
    seen: set[str] = set(soup.get_text())
    for tag in soup.find_all(True):
        for value in tag.attrs.values():
            seen.update("".join(value) if isinstance(value, list) else str(value))
    chosen: list[str] = []
    for code in range(_MARKER_FIRST, _MARKER_LAST + 1):
        char = chr(code)
        if char not in seen:
            chosen.append(char)
            if len(chosen) == 5:
                return _Markers(*chosen)
    raise ValueError("no free private-use code points for markers")  # pragma: no cover


def _is_hidden(tag: Any) -> bool:
    if tag.name == "template":
        return True
    if tag.has_attr("hidden"):
        return True
    style = tag.get("style")
    return isinstance(style, str) and bool(_HIDDEN_STYLE.search(style))


def _merged_cells(table: Any) -> bool:
    for cell in table.find_all(["td", "th"]):
        for attr in ("rowspan", "colspan"):
            value = str(cell.get(attr, "1")).strip()
            if value.isdigit() and int(value) > 1:
                return True
    return False


def _mark(soup: Any, tag: Any, open_mark: str, close_mark: str) -> None:
    """Place markers at the start and end of `tag`'s text, inside a cell when the
    tag is table structure (markdownify drops text that sits between cells)."""
    first, last = tag, tag
    while first.name in _TABLE_STRUCTURE:
        cell = first.find(["td", "th"])
        if cell is None:
            break
        first = cell
    while last.name in _TABLE_STRUCTURE:
        cells = last.find_all(["td", "th"])
        if not cells:
            break
        last = cells[-1]
    first.insert(0, soup.new_string(open_mark))
    last.append(soup.new_string(close_mark))


def _edge_chars(tag: Any) -> tuple[str, str]:
    text = tag.get_text()
    return (text[:1], text[-1:]) if text else ("", "")


def _separate_xml(soup: Any, ours_string: Any) -> None:
    """In XML, a space between adjacent elements whose text would touch."""
    from bs4 import Tag

    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag):
            continue
        first, last = _edge_chars(tag)
        before = tag.find_previous(string=True)
        own = list(tag.strings)
        after = own[-1].find_next(string=True) if own else tag.find_next(string=True)
        if before is not None and str(before)[-1:].isalnum() and first.isalnum():
            tag.insert_before(ours_string(" "))
        if after is not None and str(after)[:1].isalnum() and last.isalnum():
            tag.insert_after(ours_string(" "))


def _numeric_edge(left: str, right: str) -> bool:
    """Would joining `left` and `right` put a digit, or a separator next to a digit,
    against the other side's text?"""
    a, b = left[-1], right[0]
    if a.isspace() or b.isspace():
        return False
    if a.isdigit() or b.isdigit():
        return True
    if a in _NUMBER_JOINERS and len(left) >= 2 and left[-2].isdigit():
        return True
    return b in _NUMBER_JOINERS and len(right) >= 2 and right[1].isdigit()


_SIGN_CHARS = "+-\u2212"


def _number_forming(char: str, beside: str) -> bool:
    """Could `char` belong to a number: a digit, a sign, or a separator next to a
    digit (`beside` is the character on its other side)?"""
    if char.isdigit() or char in _SIGN_CHARS:
        return True
    return char in ".,'\u00a0\u2009\u202f\u2019" and beside.isdigit()


def _xbrl_join_allowed(previous: Any, node: Any) -> bool:
    """May two strings join across this boundary? Only when the boundary is one
    unstyled inline XBRL (`ix:`) tag being entered or left, and the character on the
    outer side is not number-forming. So "$<ix:nonFraction>394,328</ix:nonFraction>"
    joins, while two adjacent ix figures, or a digit, sign or separator touching an
    ix figure, still split."""
    def unstyled_ix(element: Any) -> bool:
        return ((element.name or "").startswith("ix:") and not element.has_attr("style")
                and not element.has_attr("class"))

    before = list(previous.parents)
    after = list(node.parents)
    left = [p for p in before if p not in after]
    entered = [p for p in after if p not in before]
    text_before, text_after = str(previous), str(node)
    if entered and not left and all(unstyled_ix(p) for p in entered):
        outer, beside = text_before[-1], text_before[-2:-1]
    elif left and not entered and all(unstyled_ix(p) for p in left):
        outer, beside = text_after[0], text_after[1:2]
    else:
        return False
    return not _number_forming(outer, beside)


def _script_box(node: Any) -> Any:
    """The nearest <sup> or <sub> around a string: markdownify marks its edges."""
    for parent in node.parents:
        if parent.name in ("sup", "sub"):
            return parent
    return None


def _split_boundaries(soup: Any, mark: str, ours: set[int]) -> None:
    from bs4.element import NavigableString

    previous: Any = None
    for node in list(soup.descendants):
        if type(node) is not NavigableString:  # comments, CDATA, doctypes are not text
            continue
        text = str(node)
        if not text:
            continue
        if id(node) in ours or (previous is not None and id(previous) in ours):
            previous = node  # beside a separator this extractor put in: already apart
            continue
        if (previous is not None and _numeric_edge(str(previous), text)
                and _script_box(previous) is _script_box(node)
                and not _xbrl_join_allowed(previous, node)):
            node.insert_before(soup.new_string(mark))
        previous = node


def _prepare(soup: Any, xml: bool) -> _Markers:
    """Apply the separations and markers; return the markers used."""
    from bs4 import ProcessingInstruction

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    # `<?xml ...?>` and other processing instructions are markup, not page text.
    for instruction in soup.find_all(string=lambda s: isinstance(s, ProcessingInstruction)):
        instruction.extract()
    markers = _choose_markers(soup)
    ours: set[int] = set()

    def ours_string(text: str) -> Any:
        string = soup.new_string(text)
        ours.add(id(string))
        return string

    for tag in soup.find_all(True):
        name = tag.name.lower() if tag.name else ""
        if name in _SEPARATE:
            tag.insert_before(ours_string(" "))
            tag.insert_after(ours_string(" "))
        children = [c for c in tag.children if getattr(c, "name", None)]
        if name in _MATH_ROWS:
            for child in children[1:]:
                child.insert_before(ours_string(" "))
        if name in ("msup", "msub", "msubsup") and len(children) >= 2:
            marks = {"msup": ["^"], "msub": ["~"], "msubsup": ["~", "^"]}[name]
            for child, mark in zip(children[1:], marks, strict=False):
                child.insert_before(ours_string(mark))
                child.insert_after(ours_string(mark))
        if name == "mfrac" and len(children) == 2:
            children[1].insert_before(ours_string("/"))
    if xml:
        # Never delete: in XML, attributes carry data (an observation's value).
        for tag in soup.find_all(True):
            pairs = [(k, " ".join(v) if isinstance(v, list) else str(v))
                     for k, v in tag.attrs.items() if k != "xmlns" and not k.startswith("xmlns:")]
            if pairs:
                shown = " ".join(f'{k}="{v}"' for k, v in pairs)
                tag.insert(0, ours_string(f"[{shown}] "))
        _separate_xml(soup, ours_string)
    _split_boundaries(soup, markers.split, ours)
    for tag in soup.find_all(True):
        if _is_hidden(tag):
            _mark(soup, tag, markers.hidden_open, markers.hidden_close)
    for table in soup.find_all("table"):
        if _merged_cells(table):
            _mark(soup, table, markers.table_open, markers.table_close)
    return markers


def _collect_ranges(text: str, m: _Markers) -> tuple[str, list[dict[str, Any]], bool]:
    """Remove markers; return the text, ranges in it, and whether they balanced. A
    split mark becomes one space (an `element_split` range) unless the text on
    either side is already whitespace, where it is simply dropped."""
    out: list[str] = []
    ranges: list[dict[str, Any]] = []
    stacks: dict[str, list[int]] = {"hidden": [], "table_merged_cells": []}
    opens = {m.hidden_open: "hidden", m.table_open: "table_merged_cells"}
    closes = {m.hidden_close: "hidden", m.table_close: "table_merged_cells"}
    balanced = True
    position = 0
    for index, char in enumerate(text):
        if char == m.split:
            after = next((c for c in text[index + 1:] if c not in m.all), "")
            if out and not out[-1].isspace() and after and not after.isspace():
                out.append(" ")
                ranges.append({"start": position, "end": position + 1, "kind": "element_split"})
                position += 1
            continue
        if char in opens:
            stacks[opens[char]].append(position)
            continue
        if char in closes:
            kind = closes[char]
            if stacks[kind]:
                start = stacks[kind].pop()
                if not stacks[kind] and position > start:
                    ranges.append({"start": start, "end": position, "kind": kind})
            else:
                balanced = False
            continue
        out.append(char)
        position += 1
    for kind, stack in stacks.items():
        if stack:
            balanced = False
            ranges.append({"start": stack[0], "end": position, "kind": kind})
    ranges.sort(key=lambda r: (r["start"], r["end"]))
    return "".join(out), ranges, balanced


def html_to_text(markup: str, *, xml: bool = False) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Convert; return (text, flagged ranges, warnings)."""
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    from markdownify import MarkdownConverter

    warns: list[str] = []
    with warnings_module.catch_warnings():
        warnings_module.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(markup, "html.parser")
    markers = _prepare(soup, xml)
    converter = MarkdownConverter(
        heading_style="ATX",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
        bullets="-",
        sup_symbol="^",
        sub_symbol="~",
    )
    raw = converter.convert_soup(soup).replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"(?m)^[ \t]+$", "", raw)  # lines left holding only separator spaces
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = raw.strip("\n") + ("\n" if raw.strip() else "")
    text, ranges, balanced = _collect_ranges(raw, markers)
    if not balanced:
        warns.append("region_markers_unbalanced: some ranges may be approximate")
    hidden = sum(1 for r in ranges if r["kind"] == "hidden")
    tables = sum(1 for r in ranges if r["kind"] == "table_merged_cells")
    splits = sum(1 for r in ranges if r["kind"] == "element_split")
    if hidden:
        warns.append(f"hidden_text_kept: {hidden} ranges a reader would not see")
    if tables:
        warns.append(f"tables_with_merged_cells: {tables}; columns may be misaligned")
    if splits:
        warns.append(f"element_splits: {splits} places where a digit met another element's "
                     "text were split; a quote may not start or end at one")
    return text, ranges, warns
