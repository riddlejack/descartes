"""From stored bytes to stored text, with nothing deciding what counts as content.

- HTML and XML: decoded by an allowlisted charset, then markdownify 1.2.3 over the
  whole document with only `<script>` and `<style>` removed. Headings are ATX
  (`#`) so the outline can find them; markdownify's escaping is off, so the text
  reads as the page does.
- PDF: pypdfium2 5.14.0, the text layer page by page, pages joined by a blank
  line; the outline lists every page with its offsets, plus the PDF's own
  bookmarks if it has them.
- Plain text, CSV, JSON: decoded, otherwise unchanged.

Line endings in stored text are normalised to `\\n`. Offsets everywhere are
Unicode code points in the stored text.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import CaptureStatus

# Invariant: the charset labels we decode, mapped to the codec each means. An
# allowlist, because Python will also decode `utf-7` and other escape-interpreting
# codecs, which can turn bytes into markup the bytes do not contain. Labels follow
# the WHATWG Encoding Standard; ISO-8859-1 and ASCII mean windows-1252 there.
CHARSET_ALLOWLIST: dict[str, str] = {
    "utf-8": "utf-8", "utf8": "utf-8", "unicode-1-1-utf-8": "utf-8",
    "utf-16": "utf-16", "utf-16le": "utf-16-le", "utf-16be": "utf-16-be",
    "iso-8859-1": "cp1252", "latin1": "cp1252", "l1": "cp1252", "iso8859-1": "cp1252",
    "ascii": "cp1252", "us-ascii": "cp1252", "windows-1252": "cp1252", "cp1252": "cp1252",
    "iso-8859-2": "iso8859-2", "iso-8859-3": "iso8859-3", "iso-8859-4": "iso8859-4",
    "iso-8859-5": "iso8859-5", "iso-8859-6": "iso8859-6", "iso-8859-7": "iso8859-7",
    "iso-8859-8": "iso8859-8", "iso-8859-9": "cp1254", "iso-8859-10": "iso8859-10",
    "iso-8859-13": "iso8859-13", "iso-8859-14": "iso8859-14", "iso-8859-15": "iso8859-15",
    "iso-8859-16": "iso8859-16",
    "windows-1250": "cp1250", "windows-1251": "cp1251", "windows-1253": "cp1253",
    "windows-1254": "cp1254", "windows-1255": "cp1255", "windows-1256": "cp1256",
    "windows-1257": "cp1257", "windows-1258": "cp1258",
    "koi8-r": "koi8-r", "koi8-u": "koi8-u", "macintosh": "mac-roman",
    "shift_jis": "shift_jis", "sjis": "shift_jis", "windows-31j": "cp932",
    "euc-jp": "euc_jp", "euc-kr": "euc_kr", "gbk": "gbk", "gb2312": "gbk",
    "gb18030": "gb18030", "big5": "big5",
}

# Resource limit: leading bytes searched for a `<meta charset>` declaration.
_META_SNIFF_BYTES = 4096
_META_CHARSET = re.compile(rb"""<meta[^>]{0,512}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:+-]+)""", re.IGNORECASE)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9_.:+-]+)\"?", re.IGNORECASE)
_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
XML_TYPES = frozenset({"text/xml", "application/xml", "application/rss+xml",
                       "application/atom+xml", "application/xbrl+xml"})
TEXT_TYPES = frozenset({"text/plain", "text/csv", "application/json", "text/markdown",
                        "text/tab-separated-values", "application/ld+json"})
PDF_TYPE = "application/pdf"


@dataclass
class Extraction:
    status: CaptureStatus
    content_type: str
    text: str | None = None
    outline: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    charset: str | None = None
    extractor: dict[str, str] = field(default_factory=dict)
    detail: str | None = None
    pages: int | None = None


def _version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def extractor_versions() -> dict[str, str]:
    """Every extractor version that shapes stored text; part of the cache key."""
    return {
        "markdownify": _version("markdownify"),
        "beautifulsoup4": _version("beautifulsoup4"),
        "pypdfium2": _version("pypdfium2"),
        "stemma_extract": EXTRACT_RULES_VERSION,
    }


# Invariant: bumped whenever this module's rules change the text it produces.
EXTRACT_RULES_VERSION = "3"


def sniff_content_type(declared: str | None, body: bytes, name_hint: str = "") -> str:
    """The media type to extract by. A PDF signature beats a wrong header."""
    media = (declared or "").split(";", 1)[0].strip().lower()
    head = body[:1024].lstrip()
    if body[:5] == b"%PDF-" or (b"%PDF-" in body[:1024] and media in ("", "application/octet-stream")):
        return PDF_TYPE
    lower_hint = name_hint.lower()
    if media in ("", "application/octet-stream", "binary/octet-stream"):
        if lower_hint.endswith(".pdf"):
            return PDF_TYPE
        low = head[:512].lower()
        if low.startswith(b"<!doctype html") or b"<html" in low:
            return "text/html"
        if low.startswith(b"<?xml"):
            return "application/xml"
        for suffix, kind in ((".html", "text/html"), (".htm", "text/html"),
                             (".xml", "application/xml"), (".txt", "text/plain"),
                             (".csv", "text/csv"), (".json", "application/json"),
                             (".md", "text/markdown")):
            if lower_hint.endswith(suffix):
                return kind
    return media or "application/octet-stream"


def _decode(body: bytes, header_type: str | None, markup: bool) -> tuple[str | None, str | None, list[str]]:
    """Charset order: BOM, then the Content-Type header, then `<meta>`, then strict
    UTF-8, then windows-1252 with replacement (warned). Returns (text, label, warnings)."""
    warnings: list[str] = []
    for bom, label in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16le"),
                       (codecs.BOM_UTF16_BE, "utf-16be")):
        if body.startswith(bom):
            codec = CHARSET_ALLOWLIST[label]
            return body[len(bom):].decode(codec, errors="replace"), label, warnings
    declared: str | None = None
    if header_type:
        found = _HEADER_CHARSET.search(header_type)
        if found:
            declared = found.group(1).lower()
    if declared is None and markup:
        found_meta = _META_CHARSET.search(body[:_META_SNIFF_BYTES])
        if found_meta:
            declared = found_meta.group(1).decode("ascii", "replace").lower()
    if declared is not None:
        chosen = CHARSET_ALLOWLIST.get(declared)
        if chosen is None:
            return None, declared, warnings
        text = body.decode(chosen, errors="replace")
        if "\ufffd" in text and chosen == "utf-8":
            warnings.append("invalid_bytes_for_declared_charset_replaced")
        return text, declared, warnings
    try:
        return body.decode("utf-8"), "utf-8", warnings
    except UnicodeDecodeError:
        warnings.append("charset_undeclared_decoded_as_windows-1252")
        return body.decode("cp1252", errors="replace"), "windows-1252", warnings


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def html_to_text(markup: str) -> str:
    from bs4 import BeautifulSoup
    from markdownify import MarkdownConverter

    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    converter = MarkdownConverter(
        heading_style="ATX",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
        bullets="-",
    )
    text = converter.convert_soup(soup)
    text = _normalise_newlines(text)
    # Collapse runs of blank lines that markdownify leaves between blocks.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + ("\n" if text.strip() else "")


def markdown_outline(text: str) -> list[dict[str, Any]]:
    """Every ATX heading with its section's offsets (to the next heading at the same
    or a higher level, or the end)."""
    heads: list[tuple[int, int, str]] = []
    offset = 0
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            match = _ATX.match(line)
            if match:
                heads.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line) + 1
    outline: list[dict[str, Any]] = []
    for index, (start, level, title) in enumerate(heads):
        end = len(text)
        for later_start, later_level, _ in heads[index + 1:]:
            if later_level <= level:
                end = later_start
                break
        outline.append({"anchor": f"h{index + 1}", "kind": "heading", "level": level,
                        "title": title[:200], "start": start, "end": end})
    return outline


def pdf_to_text(body: bytes) -> tuple[str, list[dict[str, Any]], int, list[str]]:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    warnings: list[str] = []
    pdf = pdfium.PdfDocument(body)
    try:
        parts: list[str] = []
        outline: list[dict[str, Any]] = []
        offset = 0
        page_starts: list[int] = []
        count = len(pdf)
        for index in range(count):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                page_text = _normalise_newlines(textpage.get_text_bounded())
            finally:
                textpage.close()
                page.close()
            if index > 0:
                parts.append("\n\n")
                offset += 2
            page_starts.append(offset)
            parts.append(page_text)
            outline.append({"anchor": f"p{index + 1}", "kind": "page", "level": 1,
                            "title": f"Page {index + 1}", "start": offset,
                            "end": offset + len(page_text)})
            offset += len(page_text)
        try:
            for number, item in enumerate(pdf.get_toc(max_depth=4)):
                page_index = item.page_index
                if page_index is None or not 0 <= page_index < count:
                    continue
                start = page_starts[page_index]
                outline.append({"anchor": f"t{number + 1}", "kind": "bookmark",
                                "level": int(item.level) + 1, "title": str(item.title)[:200],
                                "start": start, "end": outline[page_index]["end"]})
        except Exception:  # noqa: BLE001 - bookmarks are a convenience; failure is warned
            warnings.append("pdf_bookmarks_unreadable")
        return "".join(parts), outline, count, warnings
    finally:
        pdf.close()


def _sanity(text: str, body_len: int, markup: bool) -> tuple[CaptureStatus, list[str]]:
    if not text.strip():
        return CaptureStatus.EMPTY_TEXT, ["extraction_empty"]
    if markup:
        chars = len(text.strip())
        if chars < limits.SHORT_TEXT_MIN_CHARS:
            return CaptureStatus.SHORT_TEXT, [
                f"extraction_short: {chars} chars of text from {body_len} bytes"
            ]
        if body_len >= limits.SHORT_TEXT_RATIO_MIN_BYTES and chars / body_len < limits.SHORT_TEXT_MIN_RATIO:
            return CaptureStatus.SHORT_TEXT, [
                f"extraction_short: {chars} chars of text from {body_len} bytes"
            ]
    return CaptureStatus.OK, []


def extract(body: bytes, declared_type: str | None, name_hint: str = "") -> Extraction:
    """Extract stored text from bytes. Never raises for bad input; returns a status."""
    content_type = sniff_content_type(declared_type, body, name_hint)
    versions = extractor_versions()
    if content_type == PDF_TYPE:
        extractor = {"name": "pypdfium2", "version": versions["pypdfium2"],
                     "rules": EXTRACT_RULES_VERSION}
        try:
            text, outline, pages, warnings = pdf_to_text(body)
        except Exception as exc:  # noqa: BLE001 - any extractor failure becomes a typed status
            return Extraction(CaptureStatus.EXTRACTION_FAILED, content_type,
                              extractor=extractor, detail=f"pypdfium2: {exc}")
        status, flags = _sanity(text, len(body), markup=False)
        if status is CaptureStatus.EMPTY_TEXT:
            flags.append("pdf_has_no_text_layer: OCR is not available")
        elif pages and len(text.strip()) / pages < limits.PDF_MIN_CHARS_PER_PAGE:
            status = CaptureStatus.SHORT_TEXT
            flags.append(f"extraction_short: {len(text.strip())} chars over {pages} pages; "
                         "pages may be scanned images and OCR is not available")
        return Extraction(status, content_type, text, outline, warnings + flags, None,
                          extractor, pages=pages)
    markup = content_type in HTML_TYPES or content_type in XML_TYPES
    if markup or content_type in TEXT_TYPES or content_type.startswith("text/"):
        decoded, label, warnings = _decode(body, declared_type, markup)
        if decoded is None:
            return Extraction(CaptureStatus.UNSUPPORTED_CHARSET, content_type, charset=label,
                              detail=f"charset {label!r} is not on the allowlist")
        if markup:
            extractor = {"name": "markdownify", "version": versions["markdownify"],
                         "beautifulsoup4": versions["beautifulsoup4"],
                         "rules": EXTRACT_RULES_VERSION}
            try:
                text = html_to_text(decoded)
            except Exception as exc:  # noqa: BLE001 - any extractor failure becomes a typed status
                return Extraction(CaptureStatus.EXTRACTION_FAILED, content_type, charset=label,
                                  extractor=extractor, detail=f"markdownify: {exc}")
            outline = markdown_outline(text)
        else:
            extractor = {"name": "decode", "version": EXTRACT_RULES_VERSION,
                         "rules": EXTRACT_RULES_VERSION}
            text = _normalise_newlines(decoded)
            outline = []
        status, flags = _sanity(text, len(body), markup)
        return Extraction(status, content_type, text, outline, warnings + flags, label, extractor)
    return Extraction(CaptureStatus.UNSUPPORTED_CONTENT_TYPE, content_type,
                      detail=f"no text extractor for {content_type}")
