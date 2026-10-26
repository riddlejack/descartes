"""Regression tests for the Codex review of 8e59532 (data files and PDF markers).
Each reproduces the attack."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from test_review_codex import _pdf

from stemma.evidence import limits
from stemma.evidence.datafiles import csv_dialect, json_text
from stemma.evidence.errors import CaptureStatus
from stemma.evidence.extract import XLSX_TYPE, extract
from stemma.evidence.htmltext import html_to_text
from stemma.evidence.quotes import find_quote

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def xlsx(sheet_xml: str, *, shared: list[str] | None = None, styles: str | None = None,
         extra_sheets: int = 0) -> bytes:
    """A minimal XLSX written by hand, so a test controls every byte of the XML."""
    names = ["Data"] + [f"S{i}" for i in range(extra_sheets)]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        sheets = "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                         for i, n in enumerate(names))
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">'
                   f"<sheets>{sheets}</sheets></workbook>")
        rels = "".join(f'<Relationship Id="rId{i + 1}" Type="{REL}/worksheet" '
                       f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(names)))
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.'
                   f'openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{MAIN}">{sheet_xml}</worksheet>')
        for i in range(extra_sheets):
            z.writestr(f"xl/worksheets/sheet{i + 2}.xml",
                       f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>')
        if shared is not None:
            items = "".join(f"<si><t>{s}</t></si>" for s in shared)
            z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{items}</sst>')
        if styles is not None:
            z.writestr("xl/styles.xml", f'<styleSheet xmlns="{MAIN}">{styles}</styleSheet>')
    return out.getvalue()


def text_of(body: bytes) -> str:
    result = extract(body, XLSX_TYPE)
    assert result.status is CaptureStatus.OK, (result.status, result.detail)
    return result.text or ""


# 1. PDF markers: per run baseline and direction; every split edge is warned ------------

def test_rotated_number_not_split() -> None:
    ops = (b"BT /F1 10 Tf 12 TL 72 720 Td (Body text of the report at the usual size, at "
           b"length for the check.) Tj ET BT /F1 10 Tf 0.7071 0.7071 -0.7071 0.7071 300 300 Tm "
           b"(1250) Tj ET")
    result = extract(_pdf(ops), "application/pdf")
    text = result.text or ""
    assert "1250" in text and "1 250" not in text
    assert not [f for f in result.flagged if f["kind"] == "superscript"]


def test_small_raise_of_small_marker_split() -> None:
    ops = (b"BT /F1 10 Tf 12 TL 72 720 Td (Body text of the report at the usual size, at "
           b"length.) Tj T* (Outlays were 1,250) Tj 6 Tf 1.5 Ts (3) Tj 10 Tf 0 Ts ( in all.) Tj ET")
    result = extract(_pdf(ops), "application/pdf")
    assert "1,2503" not in (result.text or "")
    assert any(f["kind"] == "superscript" for f in result.flagged)


def test_quote_edge_at_superscript_split_warned() -> None:
    text = "as of December 31, 2025. 18\nAccording"
    flagged = [{"start": 24, "end": 25, "kind": "superscript_split"},
               {"start": 25, "end": 27, "kind": "superscript"}]
    ended = find_quote(text, "as of December 31, 2025.", flagged)
    assert ended["found"] and any("split" in w for w in ended["warnings"])


# 2. Workbook limits checked by streaming, before anything is built ------------------------

def test_merged_cells_counted_before_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "SHEET_MAX_CELLS", 1)
    body = xlsx('<dimension ref="A1:AZ100"/><sheetData><row r="1"><c r="A1"><v>1</v></c>'
                '</row></sheetData><mergeCells count="1"><mergeCell ref="A1:AZ100"/></mergeCells>')
    result = extract(body, XLSX_TYPE)
    assert result.status is CaptureStatus.EXTRACTION_FAILED and "merged" in (result.detail or "")


def test_cells_counted_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "SHEET_MAX_CELLS", 5)
    cells = "".join(f'<c r="A{i}"><v>{i}</v></c>' for i in range(1, 50))
    result = extract(xlsx(f'<sheetData><row r="1">{cells}</row></sheetData>'), XLSX_TYPE)
    assert result.status is CaptureStatus.EXTRACTION_FAILED and "cells" in (result.detail or "")


# 4. Values exactly as written; dates beside, never instead ------------------------------

def test_numbers_exactly_as_written() -> None:
    text = text_of(xlsx('<sheetData><row r="1"><c r="A1"><v>9007199254740993</v></c>'
                        '<c r="B1"><v>1.10</v></c><c r="C1"><v>1E-3</v></c></row></sheetData>'))
    assert "A1: 9007199254740993" in text and "B1: 1.10" in text and "C1: 1E-3" in text


def test_date_serials_kept_with_rendering_beside() -> None:
    styles = ('<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>'
              '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>')
    text = text_of(xlsx('<sheetData><row r="1"><c r="A1" s="1"><v>59</v></c><c r="B1" s="1">'
                        '<v>60</v></c><c r="C1" s="1"><v>45382.5</v></c></row></sheetData>',
                        styles=styles))
    assert "A1: 59 [format yyyy-mm-dd; date 1900-02-28]" in text
    assert "B1: 60 [format yyyy-mm-dd; date 1900-02-29, which exists only in Excel" in text
    assert "C1: 45382.5 [format yyyy-mm-dd; date 2024-03-31T12:00:00]" in text


# 6. No formula cell dropped -----------------------------------------------------------

def test_formulas_of_every_kind_kept() -> None:
    text = text_of(xlsx(
        '<sheetData><row r="1">'
        '<c r="A1"><f>B2+B3</f><v>21</v></c>'
        '<c r="B1"><f>C2*2</f></c>'
        '<c r="C1"><f t="array" ref="C1:C3">SUM(D1:D3*E1:E3)</f><v>14</v></c>'
        '<c r="D1"><f t="shared" ref="D1:D3" si="0">E1+1</f><v>2</v></c>'
        '</row><row r="2"><c r="D2"><f t="shared" si="0"/><v>3</v></c></row></sheetData>'))
    assert "A1: 21 [formula =B2+B3]" in text
    assert "B1: [no cached value] [formula =C2*2]" in text
    assert "C1: 14 [array formula =SUM(D1:D3*E1:E3) over C1:C3]" in text
    assert "D1: 2 [formula =E1+1, shared over D1:D3]" in text
    assert "D2: 3 [shared formula, from the group of D1]" in text


# 7. A hidden column range flags every column in it -------------------------------------

def test_hidden_column_range_flags_all() -> None:
    body = xlsx('<cols><col min="1" max="3" hidden="1"/></cols><sheetData><row r="1">'
                '<c r="A1"><v>1</v></c><c r="B1"><v>2</v></c><c r="C1"><v>3</v></c>'
                '<c r="D1"><v>4</v></c></row></sheetData>')
    result = extract(body, XLSX_TYPE)
    text = result.text or ""
    hidden = {text[f["start"]:f["end"]] for f in result.flagged if f["kind"] == "hidden"}
    assert hidden == {"A1: 1", "B1: 2", "C1: 3"}


def test_shared_strings_rich_text_and_inline() -> None:
    body = xlsx('<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                '<c r="B1" t="inlineStr"><is><t>Inline 7</t></is></c>'
                '<c r="C1" t="b"><v>1</v></c><c r="D1" t="e"><v>#DIV/0!</v></c></row></sheetData>',
                shared=["Region"])
    text = text_of(body)
    assert "A1: Region\tB1: Inline 7\tC1: TRUE\tD1: #DIV/0!" in text


# 3. JSON output bounded while it is written ------------------------------------------------

def test_json_expansion_stopped_while_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = "[" * 200 + "]" * 200  # 400 bytes that indent to megabytes of spaces
    monkeypatch.setattr(limits, "JSON_MAX_OUTPUT_FACTOR", 4)
    text, details, warnings = json_text(raw)
    assert text == raw and details["pretty_printed"] is False
    assert any("size limit" in w for w in warnings)


# 5. XML attributes are kept --------------------------------------------------------------

def test_xml_attribute_values_kept() -> None:
    text, _, _ = html_to_text("<message><obs time='2024' value='1250.5'>North</obs>"
                              "<obs value='980'/></message>", xml=True)
    assert '1250.5' in text and "980" in text and "2024" in text and "North" in text
    sdmx = extract(b"<message><obs value='1250.5'>North</obs></message>",
                   "application/vnd.sdmx.genericdata+xml")
    assert "1250.5" in (sdmx.text or "")


# 8. A CSV dialect is detected correctly or recorded ambiguous --------------------------

@pytest.mark.parametrize("sample, expected", [
    ("North;1,25\nSouth;2,50\nEast;3,75\n", ";"),
    ("a,b,c\n1,2,3\n4,5,6\n", ","),
    ("a\tb\n1\t2\n", "\t"),
])
def test_csv_dialect(sample: str, expected: str) -> None:
    dialect, _ = csv_dialect(sample, False)
    assert dialect["delimiter"] == expected, dialect


def test_csv_ambiguous_recorded() -> None:
    dialect, warnings = csv_dialect("a;b,c\nd;e,f\n", False)
    assert dialect["delimiter"] is None and set(dialect["candidates"]) == {",", ";"}
    assert any("ambiguous" in w for w in warnings)


# Suspected: XLS number formats -------------------------------------------------------------

def test_xls_value_carries_format() -> None:
    from stemma.evidence.sheetworker import _xls_value

    assert _xls_value(0.1234, "0.0%", False, 0) == "0.1234 [format 0.0%]"
    assert _xls_value(60.0, "d-mmm-yy", True, 0).startswith("60 [format d-mmm-yy; date 1900-02-29")
    assert _xls_value(10.0, "General", False, 0) == "10"
    assert json.dumps(True)
