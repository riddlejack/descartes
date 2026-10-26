"""Item anchors for SEC 10-K, 10-Q and 8-K captures (and their /A amendments).

A filing's HTML marks its Items with styled text, not heading tags, so the Markdown
outline finds nothing. This finds the Item heading lines in the stored text:
- a candidate is a line, outside any Markdown table row, that starts (after
  Markdown emphasis or heading marks) with "Item <id>" where <id> is `1`, `1A`,
  `7A`, `2.02` and the like, followed by a separator (`.`, `:`, a dash), the end
  of the line, or a capitalised title. "Item 7 of this report ..." is not one;
- "PART I" to "PART IV" lines set the part, so a 10-Q's Part I Item 1 and Part II
  Item 1 are told apart;
- per (part, item) the last candidate wins: a table of contents written as plain
  lines comes before the body, so its entries lose to the body's headings;
- an Item's section runs to the next Item or Part heading, or the end.
Anchors are `item-<id>` (lower case, e.g. `item-1a`, `item-2.02`), or
`part-<roman>-item-<id>` when the same id appears under more than one part.
"""

from __future__ import annotations

import re
from typing import Any

FORMS = frozenset({"10-K", "10-Q", "8-K"})
# Resource limit: longest line considered a heading (body sentences are longer).
_MAX_HEADING_CHARS = 250
_LEAD = re.compile(r"^[\s#>*_]*")
_PART = re.compile(r"^part\s+(iv|i{1,3})\b[\s.:\-–—]*(.{0,120})$", re.IGNORECASE)
_ITEM = re.compile(
    r"^item\s+(\d{1,2}(?:\.\d{2})?[a-c]?)(?=$|[\s.:\-–—*_])"
    r"(?P<rest>.*)$", re.IGNORECASE)
_SEPARATOR = re.compile(r"^[\s*_]*[.:\-–—]")


def applies_to(form: str | None) -> bool:
    return bool(form) and str(form).upper().removesuffix("/A") in FORMS


def _heading_like(rest: str) -> bool:
    if not rest.strip(" \t*_ "):
        return True
    if _SEPARATOR.match(rest):
        return True
    stripped = rest.lstrip(" \t*_ ")
    return stripped[:1].isupper() or stripped[:1] in "[(\"'“"


def _clean(line: str) -> str:
    return re.sub(r"[*_]+", "", re.sub(r"\s+", " ", line)).strip()


def item_outline(text: str) -> list[dict[str, Any]]:
    parts: list[tuple[int, str, str]] = []  # (start, roman, title)
    chosen: dict[tuple[str, str], tuple[int, str]] = {}
    part = ""
    offset = 0
    for line in text.split("\n"):
        start = offset
        offset += len(line) + 1
        if len(line) > _MAX_HEADING_CHARS or line.lstrip().startswith("|"):
            continue
        body = _LEAD.sub("", line)
        part_match = _PART.match(body)
        if part_match and len(body) <= 130:
            part = part_match.group(1).upper()
            parts.append((start, part, _clean(body)))
            continue
        item_match = _ITEM.match(body)
        if item_match and _heading_like(item_match.group("rest")):
            chosen[(part, item_match.group(1).upper())] = (start, _clean(body))
    # An Item seen before any PART line (a contents list) loses to the same Item
    # seen under a part.
    for part_name, item in list(chosen):
        if not part_name and any(p and i == item for p, i in chosen):
            del chosen[(part_name, item)]
    ids = [item for _, item in chosen]
    entries: list[dict[str, Any]] = []
    for (part_name, item), (start, title) in chosen.items():
        anchor = f"item-{item.lower()}"
        if ids.count(item) > 1 and part_name:
            anchor = f"part-{part_name.lower()}-{anchor}"
        entries.append({"anchor": anchor, "kind": "sec_item", "level": 2,
                        "title": title[:200], "start": start, "end": len(text)})
    # Parts are kept only where an Item follows them, so a stray "Part I" in a
    # contents page does not become a section.
    kept_parts = [p for p in parts if any(e["start"] > p[0] for e in entries)]
    last_part_start: dict[str, int] = {}
    for start, roman, _title in kept_parts:
        last_part_start[roman] = start
    for start, roman, title in kept_parts:
        if last_part_start[roman] == start:
            entries.append({"anchor": f"part-{roman.lower()}", "kind": "sec_part", "level": 1,
                            "title": title[:200], "start": start, "end": len(text)})
    entries.sort(key=lambda e: int(e["start"]))
    for index, entry in enumerate(entries):
        for later in entries[index + 1:]:
            if later["level"] <= entry["level"] or entry["kind"] == "sec_item":
                entry["end"] = later["start"]
                break
    return entries
