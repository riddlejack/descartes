"""HTML and XML to stored text: markdownify 1.2.3 over the whole document.

The extractor never deletes or joins (contract 4). Only `<script>` and `<style>`
are removed. Before conversion:
- superscripts and subscripts get visible markers, `^...^` and `~...~`, so
  "$5<sup>1</sup> billion" is stored as "$5^1^ billion", never "$51 billion";
- block-like elements markdownify would run together (header, footer, figure,
  nav, aside, address, details, summary, option, noscript, template, textarea and
  the like) and MathML tokens are separated by a space, so no two figures fuse;
- in XML documents, adjacent elements whose text would touch letter-to-letter or
  digit-to-digit are separated by a space;
- hidden elements (the `hidden` attribute, inline `display:none` or
  `visibility:hidden`, `<template>`) keep their text, and its range is recorded;
- tables with merged cells (`rowspan` or `colspan` above 1) keep their text, and
  the table's range is recorded, since markdownify does not re-align their cells.
Ranges are recorded by private-use marker characters placed around the element
before conversion and removed after; the markers are ours, not the page's. If a
page already contains those characters, no ranges are recorded and a warning says
so.
"""

from __future__ import annotations

import re
import warnings as warnings_module
from typing import Any

# Invariant: private-use characters marking regions during conversion.
_HIDDEN_OPEN, _HIDDEN_CLOSE = "", ""
_TABLE_OPEN, _TABLE_CLOSE = "", ""
_SPLIT = "\ue004"
_MARKERS = (_HIDDEN_OPEN, _HIDDEN_CLOSE, _TABLE_OPEN, _TABLE_CLOSE, _SPLIT)

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


def _separate_touching(soup: Any, only_namespaced: bool) -> None:
    """Put a space between adjacent elements whose text would touch letter-to-letter
    or digit-to-digit. In HTML only namespaced elements (such as inline XBRL's
    `ix:` tags) are treated so; ordinary inline HTML renders joined in a browser."""
    from bs4 import Tag

    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag):
            continue
        if only_namespaced and ":" not in (tag.name or ""):
            continue
        first, last = _edge_chars(tag)
        before = tag.find_previous(string=True)
        after = tag.find_next(string=True)
        # find_next may land inside the tag itself; look past its last string.
        own = list(tag.strings)
        if own:
            after = own[-1].find_next(string=True)
        if before is not None and str(before)[-1:].isalnum() and first.isalnum():
            tag.insert_before(soup.new_string(" "))
        if after is not None and str(after)[:1].isalnum() and last.isalnum():
            tag.insert_after(soup.new_string(" "))


def _split_meeting_digits(soup: Any, mark: str) -> None:
    """Wherever text from two different elements would meet digit to digit, put a
    split mark between them. CSS can draw them apart (`display:block`) or together,
    and code cannot tell which, so it fails in the direction that evidences
    nothing: a split real number makes a quote fail, a fused one would invent a
    figure. Splits are recorded, and `check_quote` refuses a match with an edge at
    one."""
    from bs4.element import NavigableString

    def script_box(node: Any) -> Any:
        """The nearest <sup> or <sub> around a string: markdownify already marks its
        edges (^...^, ~...~), so digits there never meet."""
        for parent in node.parents:
            if parent.name in ("sup", "sub"):
                return parent
        return None

    previous: Any = None
    for node in list(soup.descendants):
        if type(node) is not NavigableString:  # comments, CDATA, doctypes are not text
            continue
        text = str(node)
        if not text:
            continue
        if (previous is not None and str(previous)[-1:].isdigit() and text[:1].isdigit()
                and previous.parent is not node.parent
                and script_box(previous) is script_box(node)):
            node.insert_before(soup.new_string(mark))
        previous = node


def _prepare(soup: Any, xml: bool) -> bool:
    """Apply the separations and markers. Returns False if markers cannot be used."""
    from bs4 import ProcessingInstruction

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    # `<?xml ...?>` and other processing instructions are markup, not page text.
    for instruction in soup.find_all(string=lambda s: isinstance(s, ProcessingInstruction)):
        instruction.extract()
    usable = not any(marker in soup.get_text() for marker in _MARKERS)
    for tag in soup.find_all(True):
        name = tag.name.lower() if tag.name else ""
        if name in _SEPARATE:
            tag.insert_before(soup.new_string(" "))
            tag.insert_after(soup.new_string(" "))
        children = [c for c in tag.children if getattr(c, "name", None)]
        if name in _MATH_ROWS:
            for child in children[1:]:
                child.insert_before(soup.new_string(" "))
        if name in ("msup", "msub", "msubsup") and len(children) >= 2:
            marks = {"msup": ["^"], "msub": ["~"], "msubsup": ["~", "^"]}[name]
            for child, mark in zip(children[1:], marks, strict=False):
                child.insert_before(soup.new_string(mark))
                child.insert_after(soup.new_string(mark))
        if name == "mfrac" and len(children) == 2:
            children[1].insert_before(soup.new_string("/"))
    _separate_touching(soup, only_namespaced=not xml)
    _split_meeting_digits(soup, _SPLIT if usable else " ")
    if usable:
        for tag in soup.find_all(True):
            if _is_hidden(tag):
                _mark(soup, tag, _HIDDEN_OPEN, _HIDDEN_CLOSE)
        for table in soup.find_all("table"):
            if _merged_cells(table):
                _mark(soup, table, _TABLE_OPEN, _TABLE_CLOSE)
    return usable


def _collect_ranges(text: str) -> tuple[str, list[dict[str, Any]], bool]:
    """Remove markers; return the text, ranges in it, and whether they balanced. A
    split mark becomes one space (recorded as an `element_split` range) unless the
    text on either side is already whitespace, where it is simply dropped."""
    out: list[str] = []
    ranges: list[dict[str, Any]] = []
    stacks: dict[str, list[int]] = {"hidden": [], "table_merged_cells": []}
    balanced = True
    position = 0
    for index, char in enumerate(text):
        if char == _SPLIT:
            after = next((c for c in text[index + 1:] if c not in _MARKERS), "")
            if out and not out[-1].isspace() and after and not after.isspace():
                out.append(" ")
                ranges.append({"start": position, "end": position + 1, "kind": "element_split"})
                position += 1
            continue
        if char in (_HIDDEN_OPEN, _TABLE_OPEN):
            stacks["hidden" if char == _HIDDEN_OPEN else "table_merged_cells"].append(position)
            continue
        if char in (_HIDDEN_CLOSE, _TABLE_CLOSE):
            kind = "hidden" if char == _HIDDEN_CLOSE else "table_merged_cells"
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
    usable = _prepare(soup, xml)
    if not usable:
        warns.append("region_markers_in_source: hidden-text and table ranges not recorded")
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
    # Collapse runs of blank lines that markdownify leaves between blocks, keeping
    # markers where they are (they are removed below).
    raw = re.sub(r"(?m)^[ \t]+$", "", raw)  # lines left holding only separator spaces
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = raw.strip("\n") + ("\n" if raw.strip() else "")
    if not usable:
        return raw, [], warns
    text, ranges, balanced = _collect_ranges(raw)
    if not balanced:
        warns.append("region_markers_unbalanced: some ranges may be approximate")
    hidden = sum(1 for r in ranges if r["kind"] == "hidden")
    tables = sum(1 for r in ranges if r["kind"] == "table_merged_cells")
    if hidden:
        warns.append(f"hidden_text_kept: {hidden} ranges a reader would not see")
    if tables:
        warns.append(f"tables_with_merged_cells: {tables}; columns may be misaligned")
    splits = sum(1 for r in ranges if r["kind"] == "element_split")
    if splits:
        warns.append(f"element_splits: {splits} places where digits from two elements met "
                     "were split; a quote may not start or end at one")
    return text, ranges, warns
