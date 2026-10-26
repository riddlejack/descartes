"""Stored text for data files: CSV and TSV, JSON (spreadsheets are in `sheetworker`).

Same rules as pages: nothing deleted or fused, offsets in code points, a typed
failure when the content cannot be read, the extractor and its version recorded.

- **CSV and TSV** are stored verbatim (only line endings normalised), so the
  delimiter structure is exactly the file's. The dialect Python's `csv.Sniffer`
  detects (delimiter, quote character, row and column counts) is recorded on the
  receipt; if none can be detected that is said, and the text is still kept.
- **JSON** is pretty-printed deterministically: two-space indent, keys in their
  source order, every duplicate key kept, and every number written exactly as the
  source wrote it (`1.10` stays `1.10`, `1e5` stays `1e5`; Python's own
  re-serialisation would change both). Strings are written with JSON escapes
  where needed and other characters as themselves. Nesting deeper than
  `JSON_MAX_DEPTH`, or a document that is not valid JSON, is stored verbatim with
  a warning saying why it was not pretty-printed.
"""

from __future__ import annotations

import csv
import json
import re
from typing import Any

from stemma.evidence import limits

JSON_RULES_VERSION = "1"


class _Raw(str):
    """A number or constant exactly as the source wrote it."""


class _Pairs(list[tuple[str, Any]]):
    """A JSON object as its key-value pairs in order, duplicates included."""


def _depth(text: str, cap: int) -> int:
    """Nesting depth outside strings, stopping once past `cap`."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > cap:
                    return deepest
        elif char in "]}":
            depth -= 1
    return deepest


def _string(value: str) -> str:
    try:
        value.encode("utf-8")
        return json.dumps(value, ensure_ascii=False)
    except UnicodeEncodeError:  # a lone surrogate from a \\ud800 escape: keep it escaped
        return json.dumps(value, ensure_ascii=True)


class _TooLong(Exception):
    pass


class _Out:
    """The pretty text as it is written, stopped at the recorded limit."""

    def __init__(self, limit: int) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.limit = limit

    def add(self, piece: str) -> None:
        self.length += len(piece)
        if self.length > self.limit:
            raise _TooLong
        self.parts.append(piece)


def _emit(value: Any, level: int, out: _Out) -> None:
    pad = "  " * (level + 1)
    if isinstance(value, _Pairs):
        if not value:
            out.add("{}")
            return
        out.add("{\n")
        for index, (key, item) in enumerate(value):
            out.add(pad + _string(key) + ": ")
            _emit(item, level + 1, out)
            out.add(",\n" if index < len(value) - 1 else "\n")
        out.add("  " * level + "}")
    elif isinstance(value, list):
        if not value:
            out.add("[]")
            return
        out.add("[\n")
        for index, item in enumerate(value):
            out.add(pad)
            _emit(item, level + 1, out)
            out.add(",\n" if index < len(value) - 1 else "\n")
        out.add("  " * level + "]")
    elif isinstance(value, _Raw):
        out.add(str(value))
    elif isinstance(value, str):
        out.add(_string(value))
    elif value is True:
        out.add("true")
    elif value is False:
        out.add("false")
    elif value is None:
        out.add("null")
    else:  # pragma: no cover - the parser yields only the types above
        raise TypeError(type(value).__name__)


def json_text(decoded: str) -> tuple[str, dict[str, Any], list[str]]:
    """(stored text, details for the receipt, warnings)."""
    depth = _depth(decoded, limits.JSON_MAX_DEPTH)
    if depth > limits.JSON_MAX_DEPTH:
        return decoded, {"pretty_printed": False}, [
            f"json_not_pretty_printed: nesting deeper than {limits.JSON_MAX_DEPTH}; kept verbatim"]
    try:
        parsed = json.loads(decoded, parse_float=_Raw, parse_int=_Raw, parse_constant=_Raw,
                            object_pairs_hook=_Pairs)
    except (ValueError, RecursionError) as exc:
        return decoded, {"pretty_printed": False}, [
            f"json_not_pretty_printed: not valid JSON ({str(exc)[:120]}); kept verbatim"]
    # Indentation can grow a small, deep document a great deal: the output is
    # bounded while it is written, never measured after.
    limit = min(limits.DATA_MAX_TEXT_CHARS,
                limits.JSON_MAX_OUTPUT_FACTOR * len(decoded) + limits.JSON_OUTPUT_ALLOWANCE)
    out = _Out(limit)
    try:
        _emit(parsed, 0, out)
    except _TooLong:
        return decoded, {"pretty_printed": False}, [
            (f"json_not_pretty_printed: pretty text passed the size limit of {limit} "
            "characters; kept verbatim")]
    text = "".join(out.parts) + "\n"
    return text, {"pretty_printed": True, "depth": depth, "rules": JSON_RULES_VERSION}, []


_CANDIDATES = (",", ";", "\t", "|")


def _consistent_width(rows: list[list[str]]) -> int | None:
    """The column count every non-empty row shares, if more than one."""
    widths = {len(row) for row in rows if any(cell.strip() for cell in row)}
    return widths.pop() if len(widths) == 1 and next(iter(widths), 0) > 1 else None


def csv_dialect(text: str, declared_tsv: bool) -> tuple[dict[str, Any], list[str]]:
    """The delimiter whose split gives every row the same number of columns. Where
    several do, a comma that only ever sits between digits is a decimal comma and
    the semicolon wins; anything else is recorded as ambiguous, never guessed."""
    sample_lines = text[: limits.CSV_SNIFF_CHARS].splitlines()
    if len(text) > limits.CSV_SNIFF_CHARS and len(sample_lines) > 1:
        sample_lines = sample_lines[:-1]  # the last sampled line may be cut short
    candidates = ("\t",) if declared_tsv else _CANDIDATES
    consistent: dict[str, int] = {}
    for delimiter in candidates:
        try:
            rows = list(csv.reader(sample_lines, delimiter=delimiter, quotechar='"'))
        except csv.Error:
            continue
        width = _consistent_width(rows)
        if width is not None:
            consistent[delimiter] = width
    chosen: str | None = None
    if len(consistent) == 1:
        chosen = next(iter(consistent))
    elif set(consistent) >= {",", ";"} and not set(consistent) - {",", ";"}:
        unquoted = re.sub(r'"[^"]*"', "", "\n".join(sample_lines))
        if all(m.group(0)[0].isdigit() and m.group(0)[2].isdigit()
               for m in re.finditer(r".,.", unquoted)):
            chosen = ";"  # every comma is a decimal comma between digits
    if chosen is None:
        if consistent:
            return ({"delimiter": None, "candidates": sorted(consistent)},
                    [(f"csv_dialect_ambiguous: {sorted(consistent)} each split every row "
                     "evenly; text kept as written")])
        return {"delimiter": None}, ["csv_dialect_not_detected: text kept as written"]
    try:
        rows = list(csv.reader(text.splitlines(), delimiter=chosen, quotechar='"'))
    except csv.Error as exc:
        return {"delimiter": chosen, "quotechar": '"'}, [f"csv_rows_not_counted: {exc}"]
    return {"delimiter": chosen, "quotechar": '"', "rows": len(rows),
            "columns_max": max((len(r) for r in rows), default=0)}, []
