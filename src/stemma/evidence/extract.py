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

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from stemma.evidence import charset, htmltext, limits
from stemma.evidence.errors import CaptureStatus

_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
# Invariant: characters that take no space on the page (zero-width marks, soft hyphen).
_ZERO_WIDTH = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AUTOLINK = re.compile(r"<[a-z][a-z0-9+.-]*:[^>\s]*>", re.IGNORECASE)

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
    # Regions of the stored text a reader would not see, or tables whose cells the
    # converter may have misaligned: [{start, end, kind}].
    flagged: list[dict[str, Any]] = field(default_factory=list)


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
EXTRACT_RULES_VERSION = "5"


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


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def html_to_text(markup: str) -> str:
    """The stored text of an HTML document (ranges and warnings dropped)."""
    return htmltext.html_to_text(markup)[0]


def visible_words(text: str, flagged: list[dict[str, Any]] | None = None) -> int:
    """Words a reader would see: hidden ranges, link and image syntax (URLs) and
    zero-width characters left out."""
    if flagged:
        kept: list[str] = []
        cursor = 0
        for region in sorted(flagged, key=lambda r: int(r["start"])):
            if region["kind"] in ("hidden", "invisible_pdf_text", "white_pdf_text"):
                kept.append(text[cursor:int(region["start"])])
                cursor = max(cursor, int(region["end"]))
        kept.append(text[cursor:])
        text = " ".join(kept)
    text = _MD_IMAGE.sub(r" \1 ", text)
    text = _MD_LINK.sub(r" \1 ", text)
    text = _AUTOLINK.sub(" ", text)
    text = _ZERO_WIDTH.sub("", text)
    return len(re.findall(r"\w+", text))


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


class PdfExtractionError(Exception):
    pass


def _pdf_worker_argv() -> list[str]:
    """The child process that runs PDFium (see `pdfworker`). `-I`: isolated mode,
    so the child ignores PYTHON* variables and the current folder."""
    return [sys.executable, "-I", "-m", "stemma.evidence.pdfworker", str(limits.PDF_MAX_TEXT_CHARS)]


def pdf_to_text(body: bytes) -> tuple[str, list[dict[str, Any]], int, list[str],
                                      list[dict[str, Any]]]:
    """Run PDFium in a child process under a recorded time limit and output limit."""
    try:
        done = subprocess.run(_pdf_worker_argv(), input=body, capture_output=True,
                              timeout=limits.PDF_EXTRACT_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PdfExtractionError(
            f"PDF extraction passed its time limit of {limits.PDF_EXTRACT_TIMEOUT_S} s") from exc
    if len(done.stdout) > limits.PDF_MAX_OUTPUT_BYTES:
        raise PdfExtractionError("PDF extraction output passed its byte limit")
    try:
        result = json.loads(done.stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        detail = done.stderr.decode("utf-8", "replace")[-300:]
        raise PdfExtractionError(
            f"PDF worker exited {done.returncode} without a result: {detail}") from exc
    if "error" in result:
        kind = result["error"]
        detail = str(result.get("detail", ""))
        if kind == "too_much_text":
            raise PdfExtractionError(f"{detail} (output limit)")
        raise PdfExtractionError(detail)
    ok = result["ok"]
    warnings: list[str] = list(ok["warnings"])
    flagged: list[dict[str, Any]] = list(ok["flagged"])
    if b"%%EOF" not in body[-2048:]:
        warnings.append("pdf_truncated: no end-of-file marker; pages may be missing")
    invisible = sum(1 for r in flagged if r["kind"] == "invisible_pdf_text")
    white = sum(1 for r in flagged if r["kind"] == "white_pdf_text")
    if invisible:
        warnings.append(f"invisible_pdf_text_kept: {invisible} ranges")
    if white:
        warnings.append(f"white_pdf_text_kept: {white} ranges")
    return str(ok["text"]), list(ok["outline"]), int(ok["pages"]), warnings, flagged


def _sanity(text: str, body_len: int, markup: bool,
            flagged: list[dict[str, Any]]) -> tuple[CaptureStatus, list[str]]:
    """Extraction sanity flags. They mark a receipt loudly and trigger the browser
    fallback; they never discard text."""
    if not text.strip():
        return CaptureStatus.EMPTY_TEXT, ["extraction_empty"]
    words = visible_words(text, flagged)
    if words == 0:
        return CaptureStatus.EMPTY_TEXT, [
            "extraction_empty: no visible words (only markup, links, hidden or zero-width text)"]
    if markup:
        if words < limits.SHORT_TEXT_MIN_WORDS:
            return CaptureStatus.SHORT_TEXT, [
                f"extraction_short: {words} visible words from {body_len} bytes"]
        visible_chars = len(_ZERO_WIDTH.sub("", _MD_LINK.sub(r"\1", _MD_IMAGE.sub(r"\1", text))))
        if (body_len >= limits.SHORT_TEXT_RATIO_MIN_BYTES
                and visible_chars / body_len < limits.SHORT_TEXT_MIN_RATIO):
            return CaptureStatus.SHORT_TEXT, [
                f"extraction_short: {visible_chars} visible chars from {body_len} bytes"]
    return CaptureStatus.OK, []


def extract(body: bytes, declared_type: str | None, name_hint: str = "") -> Extraction:
    """Extract stored text from bytes. Never raises for bad input; returns a status."""
    content_type = sniff_content_type(declared_type, body, name_hint)
    versions = extractor_versions()
    if content_type == PDF_TYPE:
        extractor = {"name": "pypdfium2", "version": versions["pypdfium2"],
                     "rules": EXTRACT_RULES_VERSION}
        try:
            text, outline, pages, warnings, pdf_flagged = pdf_to_text(body)
        except PdfExtractionError as exc:
            return Extraction(CaptureStatus.EXTRACTION_FAILED, content_type,
                              extractor=extractor, detail=f"pypdfium2: {exc}")
        status, flags = _sanity(text, len(body), False, pdf_flagged)
        visible = len(_ZERO_WIDTH.sub("", text).strip())
        if status is CaptureStatus.EMPTY_TEXT:
            flags.append("pdf_has_no_text_layer: OCR is not available")
        elif pages and visible / pages < limits.PDF_MIN_CHARS_PER_PAGE:
            status = CaptureStatus.SHORT_TEXT
            flags.append(f"extraction_short: {visible} chars over {pages} pages; "
                         "pages may be scanned images and OCR is not available")
        if any(w.startswith("pdf_truncated") for w in warnings) and status is CaptureStatus.OK:
            status = CaptureStatus.SHORT_TEXT
        return Extraction(status, content_type, text, outline, warnings + flags, None,
                          extractor, pages=pages, flagged=pdf_flagged)
    xml = content_type in XML_TYPES or content_type == "application/xhtml+xml"
    markup = content_type in HTML_TYPES or content_type in XML_TYPES
    if markup or content_type in TEXT_TYPES or content_type.startswith("text/"):
        kind = "xml" if xml else ("html" if markup else "text")
        decoded, label, warnings = charset.decode(body, declared_type, kind)
        if decoded is None:
            return Extraction(CaptureStatus.UNSUPPORTED_CHARSET, content_type, charset=label,
                              detail=f"charset {label!r} is not on the allowlist")
        flagged: list[dict[str, Any]] = []
        if markup:
            extractor = {"name": "markdownify", "version": versions["markdownify"],
                         "beautifulsoup4": versions["beautifulsoup4"],
                         "rules": EXTRACT_RULES_VERSION}
            try:
                text, flagged, convert_warnings = htmltext.html_to_text(decoded, xml=xml)
            except Exception as exc:  # noqa: BLE001 - any extractor failure becomes a typed status
                return Extraction(CaptureStatus.EXTRACTION_FAILED, content_type, charset=label,
                                  extractor=extractor, detail=f"markdownify: {exc}")
            warnings += convert_warnings
            outline = markdown_outline(text)
        else:
            extractor = {"name": "decode", "version": EXTRACT_RULES_VERSION,
                         "rules": EXTRACT_RULES_VERSION}
            text = _normalise_newlines(decoded)
            outline = []
        status, flags = _sanity(text, len(body), markup, flagged)
        return Extraction(status, content_type, text, outline, warnings + flags, label, extractor,
                          flagged=flagged)
    return Extraction(CaptureStatus.UNSUPPORTED_CONTENT_TYPE, content_type,
                      detail=f"no text extractor for {content_type}")
