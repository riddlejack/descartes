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


def _page(textpage: Any, raw: Any) -> tuple[str, list[str | None]]:
    count = raw.FPDFText_CountChars(textpage.raw)
    chars: list[str] = []
    flags: list[str | None] = []
    r, g, b, a = (ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint())
    index = 0
    while index < count:
        code = raw.FPDFText_GetUnicode(textpage.raw, index)
        width = 1
        if 0xD800 <= code <= 0xDBFF and index + 1 < count:
            low = raw.FPDFText_GetUnicode(textpage.raw, index + 1)
            if 0xDC00 <= low <= 0xDFFF:
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                width = 2
        if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            char = "�"
        else:
            char = chr(code)
        why: str | None = None
        obj = raw.FPDFText_GetTextObject(textpage.raw, index)
        if obj and raw.FPDFTextObj_GetTextRenderMode(obj) in _INVISIBLE_MODES:
            why = "invisible_pdf_text"
        elif raw.FPDFText_GetFillColor(textpage.raw, index, r, g, b, a) and (
                a.value == 0 or min(r.value, g.value, b.value) >= _WHITE_MIN):
            why = "white_pdf_text" if a.value else "invisible_pdf_text"
        chars.append(char)
        flags.append(why)
        index += width
    return "".join(chars), flags


def _newlines(text: str, flags: list[str | None]) -> tuple[str, list[str | None]]:
    out: list[str] = []
    kept: list[str | None] = []
    index = 0
    while index < len(text):
        char = text[index]
        out.append("\n" if char == "\r" else char)
        kept.append(flags[index])
        if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 1
        index += 1
    return "".join(out), kept


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
        for index in range(count):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                page_text, flags = _newlines(*_page(textpage, raw))
            finally:
                textpage.close()
                page.close()
            if index > 0:
                parts.append("\n\n")
                offset += 2
            page_starts.append(offset)
            parts.append(page_text)
            flagged.extend(_ranges(flags, offset, page_text))
            outline.append({"anchor": f"p{index + 1}", "kind": "page", "level": 1,
                            "title": f"Page {index + 1}", "start": offset,
                            "end": offset + len(page_text)})
            offset += len(page_text)
            if offset > max_chars:
                raise TooMuchText(f"text passed the limit of {max_chars} characters")
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
