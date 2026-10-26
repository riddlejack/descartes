"""Spreadsheet text in its own process: openpyxl 3.1.5 for XLSX, xlrd 2.0.2 for XLS.

Run as `python -I -m stemma.evidence.sheetworker <xlsx|xls> <max_chars>
<max_cells> <max_unzipped_bytes>` with the file on standard input; writes one JSON
object to standard output, under the parent's recorded time and output limits.

Every sheet, in workbook order, becomes a section:

    ## Sheet 2: Regional totals

    A1: Region	B1: 2023	C1: 2024
    A2: North	B2: 10	C2: 11

Each line is one row; each non-empty cell is written `<reference>: <value>` and
cells are separated by a tab, so a quotation can name the cell it rests on.
Values are the stored cell values, not Excel's display formatting: a number is
written as the file stores it (a whole number without ".0"), with its number
format in brackets when it is not General (`B3: 0.1234 [0.0%]`), a date or time in
ISO form, a formula's cached value followed by the formula (`C4: 21 [=B2+B3]`). A
formula with no cached value is written `[=… no cached value]` and warned. In
string cells a backslash, tab or line break is written `\\\\`, `\\t` or `\\n`, so
rows stay one line each and nothing is dropped.

Flagged ranges: a hidden sheet, hidden rows and hidden columns are `hidden`;
merged cells are `table_merged_cells` (only the top-left cell holds the value).
A workbook whose unzipped size passes the recorded limit, or with more cells or
text than the limits allow, fails typed.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import zipfile
from typing import Any


class TooMuch(Exception):
    pass


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r\n", "\\n") \
        .replace("\n", "\\n").replace("\r", "\\n")


def _number(value: float) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    return _escape(str(value))


class _Writer:
    def __init__(self, max_chars: int, max_cells: int) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.cells = 0
        self.max_chars = max_chars
        self.max_cells = max_cells
        self.flagged: list[dict[str, Any]] = []
        self.outline: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def write(self, text: str) -> tuple[int, int]:
        start = self.length
        self.parts.append(text)
        self.length += len(text)
        if self.length > self.max_chars:
            raise TooMuch(f"text passed the limit of {self.max_chars} characters")
        return start, self.length

    def cell(self) -> None:
        self.cells += 1
        if self.cells > self.max_cells:
            raise TooMuch(f"workbook passed the limit of {self.max_cells} cells")

    def flag(self, start: int, end: int, kind: str) -> None:
        if end > start:
            self.flagged.append({"start": start, "end": end, "kind": kind})

    def text(self) -> str:
        return "".join(self.parts)


def _sheet_heading(w: _Writer, number: int, name: str) -> int:
    start = w.length
    if number > 1:
        w.write("\n")
    heading_start = w.length
    w.write(f"## Sheet {number}: {_escape(name)}\n\n")
    w.outline.append({"anchor": f"sheet-{number}", "kind": "sheet", "level": 2,
                      "title": f"Sheet {number}: {name}"[:200], "start": heading_start,
                      "end": None})
    return start


def _close_sheet(w: _Writer) -> None:
    w.outline[-1]["end"] = w.length


def _xlsx(body: bytes, w: _Writer, max_unzipped: int) -> None:
    import openpyxl  # type: ignore[import-untyped]
    from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid XLSX file: {exc}") from exc
    unzipped = sum(info.file_size for info in archive.infolist())
    if unzipped > max_unzipped:
        raise TooMuch(f"unzipped size {unzipped} passes the limit of {max_unzipped} bytes")
    values = openpyxl.load_workbook(io.BytesIO(body), data_only=True)
    formulas = openpyxl.load_workbook(io.BytesIO(body), data_only=False)
    uncached = 0
    for number, sheet in enumerate(values.worksheets, start=1):
        formula_sheet = formulas[sheet.title]
        _sheet_heading(w, number, sheet.title)
        body_start = w.length
        hidden_rows = {r for r, d in sheet.row_dimensions.items() if d.hidden}
        hidden_cols = {c for c, d in sheet.column_dimensions.items() if d.hidden}
        merged_tops = {(m.min_row, m.min_col) for m in sheet.merged_cells.ranges}
        # Only stored cells, in row order: iterating the declared range could walk
        # millions of empty cells. `_cells` is openpyxl's own map (pinned version).
        rows: dict[int, list[tuple[int, Any]]] = {}
        for (row, col), cell in sheet._cells.items():
            if cell.value is not None or formula_sheet._cells.get((row, col)) is not None:
                rows.setdefault(row, []).append((col, cell))
        for row in sorted(rows):
            pieces = 0
            for col, cell in sorted(rows[row], key=lambda item: item[0]):
                w.cell()
                ref = f"{get_column_letter(col)}{row}"
                formula_cell = formula_sheet._cells.get((row, col))
                formula = formula_cell.value if formula_cell is not None else None
                is_formula = isinstance(formula, str) and formula.startswith("=")
                if cell.value is None and not is_formula:
                    continue
                if pieces:
                    w.write("\t")
                if cell.value is None:
                    text = f"{ref}: [{_escape(str(formula))} no cached value]"
                    uncached += 1
                else:
                    text = f"{ref}: {_value(cell.value)}"
                    fmt = cell.number_format
                    if isinstance(cell.value, (int, float)) and fmt and fmt != "General":
                        text += f" [{_escape(fmt)}]"
                    if is_formula:
                        text += f" [{_escape(str(formula))}]"
                start, end = w.write(text)
                pieces += 1
                letter = get_column_letter(col)
                if row in hidden_rows or letter in hidden_cols:
                    w.flag(start, end, "hidden")
                if (row, col) in merged_tops:
                    w.flag(start, end, "table_merged_cells")
            if pieces:
                w.write("\n")
        if sheet.sheet_state != "visible":
            w.flag(body_start, w.length, "hidden")
            w.warnings.append(f"sheet {number} ({sheet.title}) is {sheet.sheet_state}")
        _close_sheet(w)
    if uncached:
        w.warnings.append(f"{uncached} formula cells have no cached value")


def _xls(body: bytes, w: _Writer) -> None:
    import xlrd  # type: ignore[import-untyped]

    try:
        book = xlrd.open_workbook(file_contents=body, formatting_info=True)
    except (NotImplementedError, xlrd.XLRDError):
        book = xlrd.open_workbook(file_contents=body)
    for number, sheet in enumerate(book.sheets(), start=1):
        _sheet_heading(w, number, sheet.name)
        body_start = w.length
        hidden_rows = {r for r, info in getattr(sheet, "rowinfo_map", {}).items() if info.hidden}
        hidden_cols = {c for c, info in getattr(sheet, "colinfo_map", {}).items() if info.hidden}
        merged_tops = {(r0, c0) for r0, _r1, c0, _c1 in getattr(sheet, "merged_cells", [])}
        for row in range(sheet.nrows):
            pieces = 0
            for col in range(sheet.ncols):
                cell = sheet.cell(row, col)
                if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    continue
                w.cell()
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        value = xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat()
                    except (ValueError, OverflowError):
                        value = _number(cell.value)
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    value = _number(cell.value)
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    value = "TRUE" if cell.value else "FALSE"
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    value = xlrd.error_text_from_code.get(cell.value, f"#ERROR{cell.value}")
                else:
                    value = _escape(str(cell.value))
                if pieces:
                    w.write("\t")
                ref = f"{xlrd.colname(col)}{row + 1}"
                start, end = w.write(f"{ref}: {value}")
                pieces += 1
                if row in hidden_rows or col in hidden_cols:
                    w.flag(start, end, "hidden")
                if (row, col) in merged_tops:
                    w.flag(start, end, "table_merged_cells")
            if pieces:
                w.write("\n")
        if getattr(sheet, "visibility", 0):
            w.flag(body_start, w.length, "hidden")
            w.warnings.append(f"sheet {number} ({sheet.name}) is hidden")
        _close_sheet(w)


def run(kind: str, body: bytes, max_chars: int, max_cells: int,
        max_unzipped: int) -> dict[str, Any]:
    w = _Writer(max_chars, max_cells)
    if kind == "xlsx":
        _xlsx(body, w, max_unzipped)
    elif kind == "xls":
        _xls(body, w)
    else:
        raise ValueError(f"unknown kind {kind}")
    return {"text": w.text(), "outline": w.outline, "flagged": w.flagged,
            "warnings": w.warnings, "sheets": len(w.outline), "cells": w.cells}


def main() -> None:
    kind, max_chars, max_cells, max_unzipped = sys.argv[1], *map(int, sys.argv[2:5])
    body = sys.stdin.buffer.read()
    try:
        result: dict[str, Any] = {"ok": run(kind, body, max_chars, max_cells, max_unzipped)}
    except TooMuch as exc:
        result = {"error": "too_much", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a typed failure
        result = {"error": "spreadsheet", "detail": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
