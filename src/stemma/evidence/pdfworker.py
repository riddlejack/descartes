"""PDF text extraction in its own process (pypdfium2 5.14.0).

`extract.pdf_to_text` runs this module as `python -I -m stemma.evidence.pdfworker
<max_chars>` with the PDF on standard input, under a recorded time limit, and reads
one JSON object from standard output. A hostile PDF that hangs or floods the parser
then costs a killed child process, not the host.

Text is built character by character from PDFium's character indices, so each
character's visibility flag stays aligned with it whatever the text contains (a
NUL glyph, unpaired surrogates). A character is flagged invisible when its text
object's render mode draws nothing (3, invisible; 7, clip only) or its fill is
fully transparent, and white when its fill colour is white. Flags mark; they
never remove text. Characters PDFium reports as NUL or as invalid code points are
stored as U+FFFD, so nothing is dropped and offsets stay aligned.
"""

from __future__ import annotations

import ctypes
import json
import sys
from typing import Any

# Invariant: PDF text render modes that draw nothing (ISO 32000-1, 9.3.6).
_INVISIBLE_MODES = frozenset({3, 7})
# Extraction flag: a fill colour with every channel at or above this is "white".
_WHITE_MIN = 250


class TooMuchText(Exception):
    pass


class _CharBudget:
    """Characters the whole document may still yield; checked per character, so a
    page cannot grow past the limit before it is caught."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.limit = limit

    def take(self, count: int = 1) -> None:
        self.left -= count
        if self.left < 0:
            raise TooMuchText(f"text passed the limit of {self.limit} characters")


# Extraction flags for raised markers (footnote numbers, superscripts): a glyph
# whose baseline sits at least this share of the line's font size above the
# line's baseline ...
_RAISE_MIN = 0.2
# ... and whose font is at most this share of the line's font size, or that sits
# at least _RAISE_ALONE above the baseline whatever its size, is a raised marker.
_SMALLER_MAX = 0.9
_RAISE_ALONE = 0.33
# A glyph more than this many font sizes above the baseline is on another line.
_SAME_LINE = 1.5


def _page(textpage: Any, raw: Any, budget: _CharBudget) -> tuple[
        str, list[str | None], list[str | None]]:
    """A page's text, per character why it is not visible, and per character a
    mark: "superscript" for a raised marker glyph, "split" for a space put in to
    separate one from the text it follows (as `<sup>` is marked in HTML)."""
    count = raw.FPDFText_CountChars(textpage.raw)
    chars: list[str] = []
    flags: list[str | None] = []
    marks: list[str | None] = []
    r, g, b, a = (ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint())
    ox, oy = ctypes.c_double(), ctypes.c_double()
    base: tuple[float, float] | None = None  # (baseline y, font size) of the line's text
    in_marker = False
    index = 0
    while index < count:
        budget.take()
        code = raw.FPDFText_GetUnicode(textpage.raw, index)
        width = 1
        if 0xD800 <= code <= 0xDBFF and index + 1 < count:
            low = raw.FPDFText_GetUnicode(textpage.raw, index + 1)
            if 0xDC00 <= low <= 0xDFFF:
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                width = 2
        if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            char = "\ufffd"
        else:
            char = chr(code)
        why: str | None = None
        obj = raw.FPDFText_GetTextObject(textpage.raw, index)
        if obj and raw.FPDFTextObj_GetTextRenderMode(obj) in _INVISIBLE_MODES:
            why = "invisible_pdf_text"
        elif raw.FPDFText_GetFillColor(textpage.raw, index, r, g, b, a) and (
                a.value == 0 or min(r.value, g.value, b.value) >= _WHITE_MIN):
            why = "white_pdf_text" if a.value else "invisible_pdf_text"
        raised = False
        if char in "\r\n":
            base = None
        elif not char.isspace() and raw.FPDFText_GetCharOrigin(textpage.raw, index, ox, oy):
            size = float(raw.FPDFText_GetFontSize(textpage.raw, index))
            if base is not None and base[1] > 0:
                lift = (oy.value - base[0]) / base[1]
                raised = lift < _SAME_LINE and (
                    (lift >= _RAISE_MIN and size <= _SMALLER_MAX * base[1])
                    or lift >= _RAISE_ALONE)
            if not raised:
                base = (oy.value, size)
        entering = raised and not in_marker and bool(chars) and not chars[-1].isspace()
        leaving = in_marker and not raised and not char.isspace()
        if entering or leaving:
            chars.append(" ")
            flags.append(None)
            marks.append("split")
        in_marker = raised
        chars.append(char)
        flags.append(why)
        marks.append("superscript" if raised else None)
        index += width
    return "".join(chars), flags, marks


def _newlines(text: str, flags: list[str | None],
              marks: list[str | None]) -> tuple[str, list[str | None], list[str | None]]:
    out: list[str] = []
    kept: list[str | None] = []
    kept_marks: list[str | None] = []
    index = 0
    while index < len(text):
        char = text[index]
        out.append("\n" if char == "\r" else char)
        kept.append(flags[index])
        kept_marks.append(marks[index])
        if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 1
        index += 1
    return "".join(out), kept, kept_marks


def _mark_ranges(marks: list[str | None], base: int) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    start: int | None = None
    for index, mark in enumerate([*marks, None]):
        if mark == "split":
            ranges.append({"start": base + index, "end": base + index + 1,
                           "kind": "superscript_split"})
        if mark == "superscript" and start is None:
            start = index
        elif mark != "superscript" and start is not None:
            ranges.append({"start": base + start, "end": base + index, "kind": "superscript"})
            start = None
    return ranges


def _ranges(flags: list[str | None], base: int, text: str) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    start: int | None = None
    kind: str | None = None
    for index, why in enumerate([*flags, None]):
        if why is None and start is not None and index < len(text) and text[index].isspace():
            continue  # whitespace inside a flagged run does not break it
        if why != kind:
            if kind is not None and start is not None:
                end = index
                while end > start and text[end - 1].isspace():
                    end -= 1
                ranges.append({"start": base + start, "end": base + end, "kind": kind})
            start, kind = (index, why) if why is not None else (None, None)
    return ranges


def run(body: bytes, max_chars: int) -> dict[str, Any]:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]
    from pypdfium2 import raw  # type: ignore[import-untyped,unused-ignore]

    warnings: list[str] = []
    pdf = pdfium.PdfDocument(body)
    try:
        parts: list[str] = []
        outline: list[dict[str, Any]] = []
        flagged: list[dict[str, Any]] = []
        offset = 0
        page_starts: list[int] = []
        count = len(pdf)
        budget = _CharBudget(max_chars)
        for index in range(count):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                page_text, flags, marks = _newlines(*_page(textpage, raw, budget))
            finally:
                textpage.close()
                page.close()
            if index > 0:
                budget.take(2)
                parts.append("\n\n")
                offset += 2
            page_starts.append(offset)
            parts.append(page_text)
            flagged.extend(_ranges(flags, offset, page_text))
            flagged.extend(_mark_ranges(marks, offset))
            outline.append({"anchor": f"p{index + 1}", "kind": "page", "level": 1,
                            "title": f"Page {index + 1}", "start": offset,
                            "end": offset + len(page_text)})
            offset += len(page_text)
        try:
            for number, item in enumerate(pdf.get_toc(max_depth=4)):
                page_index = item.page_index
                if page_index is None or not 0 <= page_index < count:
                    continue
                outline.append({"anchor": f"t{number + 1}", "kind": "bookmark",
                                "level": int(item.level) + 1, "title": str(item.title)[:200],
                                "start": page_starts[page_index],
                                "end": outline[page_index]["end"]})
        except Exception:  # noqa: BLE001 - bookmarks are a convenience; failure is warned
            warnings.append("pdf_bookmarks_unreadable")
        return {"text": "".join(parts), "outline": outline, "pages": count,
                "warnings": warnings, "flagged": flagged}
    finally:
        pdf.close()


def main() -> None:
    max_chars = int(sys.argv[1])
    body = sys.stdin.buffer.read()
    try:
        result: dict[str, Any] = {"ok": run(body, max_chars)}
    except TooMuchText as exc:
        result = {"error": "too_much_text", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a typed failure
        result = {"error": "pdfium", "detail": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
