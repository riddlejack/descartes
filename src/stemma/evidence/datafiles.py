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


def _emit(value: Any, level: int, out: list[str]) -> None:
    pad = "  " * (level + 1)
    if isinstance(value, _Pairs):
        if not value:
            out.append("{}")
            return
        out.append("{\n")
        for index, (key, item) in enumerate(value):
            out.append(pad + _string(key) + ": ")
            _emit(item, level + 1, out)
            out.append(",\n" if index < len(value) - 1 else "\n")
        out.append("  " * level + "}")
    elif isinstance(value, list):
        if not value:
            out.append("[]")
            return
        out.append("[\n")
        for index, item in enumerate(value):
            out.append(pad)
            _emit(item, level + 1, out)
            out.append(",\n" if index < len(value) - 1 else "\n")
        out.append("  " * level + "]")
    elif isinstance(value, _Raw):
        out.append(str(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif value is None:
        out.append("null")
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
    out: list[str] = []
    _emit(parsed, 0, out)
    text = "".join(out) + "\n"
    if len(text) > limits.DATA_MAX_TEXT_CHARS:
        return decoded, {"pretty_printed": False}, [
            "json_not_pretty_printed: pretty text over the size limit; kept verbatim"]
    return text, {"pretty_printed": True, "depth": depth, "rules": JSON_RULES_VERSION}, []


def csv_dialect(text: str, declared_tsv: bool) -> tuple[dict[str, Any], list[str]]:
    """The detected dialect, recorded on the receipt. Detection failing is said."""
    sample = text[: limits.CSV_SNIFF_CHARS]
    delimiters = "\t" if declared_tsv else ",;\t|"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=delimiters)
    except csv.Error:
        return {"delimiter": None}, ["csv_dialect_not_detected: text kept as written"]
    rows = 0
    widest = 0
    try:
        for row in csv.reader(text.splitlines(), dialect):
            rows += 1
            widest = max(widest, len(row))
    except csv.Error as exc:
        return ({"delimiter": dialect.delimiter, "quotechar": dialect.quotechar},
                [f"csv_rows_not_counted: {exc}"])
    return {"delimiter": dialect.delimiter, "quotechar": dialect.quotechar,
            "doublequote": bool(dialect.doublequote), "rows": rows, "columns_max": widest}, []
