"""Which charset to decode a page's bytes with, and decoding with it.

Order (WHATWG Encoding and HTML standards): a byte-order mark wins; then the
Content-Type header's `charset`; then, for HTML, the prescan of the first 1024
bytes for a real `<meta>` element; for XML, the `<?xml ... encoding=?>`
declaration; then strict UTF-8; then windows-1252, warned.

The prescan follows the standard's algorithm: comments are skipped, attributes are
read as attributes (so `content='charset=...'` on a description, or `<meta` inside
an attribute value, counts for nothing), and a `<meta>` declaring any UTF-16
variant means UTF-8 (a page that can be prescanned as ASCII is not UTF-16), and
`x-user-defined` means windows-1252. One deliberate difference: the contents of
`<script>` and `<style>` elements are skipped, so a string inside a script is
never read as a declaration.

Only labels on `CHARSET_ALLOWLIST` are decoded; anything else is a typed
`unsupported_charset`, because Python also decodes `utf-7` and other
escape-interpreting codecs that can turn bytes into markup they do not contain.
"""

from __future__ import annotations

import codecs
import re

# Invariant: the charset labels we decode, mapped to the codec each means. Labels
# follow the WHATWG Encoding Standard; ISO-8859-1 and ASCII mean windows-1252 there.
CHARSET_ALLOWLIST: dict[str, str] = {
    "utf-8": "utf-8", "utf8": "utf-8", "unicode-1-1-utf-8": "utf-8",
    "utf-16": "utf-16-le", "utf-16le": "utf-16-le", "utf-16be": "utf-16-be",
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
_UTF16 = frozenset({"utf-16", "utf-16le", "utf-16be"})
# Resource limit: bytes the prescan reads (the standard's figure).
PRESCAN_BYTES = 1024
_HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:+-]+)""", re.IGNORECASE)
_XML_DECL = re.compile(rb"""^<\?xml[^>]{0,200}?encoding\s*=\s*["']([A-Za-z0-9_.:+-]+)["']""",
                       re.IGNORECASE)
_SPACE = b" \t\n\x0c\r"


def _label(raw: str) -> str:
    return raw.strip().strip("\"'").strip().lower()


def _get_attribute(data: bytes, pos: int) -> tuple[bytes, bytes, int] | None:
    """The standard's 'get an attribute': (name, value, new position), or None at '>'."""
    n = len(data)
    while pos < n and (data[pos:pos + 1] in (b"/",) or data[pos] in _SPACE):
        pos += 1
    if pos >= n or data[pos:pos + 1] == b">":
        return None
    name = bytearray()
    while pos < n:
        c = data[pos:pos + 1]
        if c == b"=" and name:
            pos += 1
            break
        if c in (b"/", b">") or c[0] in _SPACE:
            while pos < n and data[pos] in _SPACE:
                pos += 1
            if data[pos:pos + 1] != b"=":
                return bytes(name).lower(), b"", pos
            pos += 1
            break
        name += c.lower()
        pos += 1
    while pos < n and data[pos] in _SPACE:
        pos += 1
    if pos >= n:
        return bytes(name), b"", pos
    quote = data[pos:pos + 1]
    value = bytearray()
    if quote in (b'"', b"'"):
        pos += 1
        end = data.find(quote, pos)
        if end == -1:
            return bytes(name), bytes(data[pos:]).lower(), n
        return bytes(name), bytes(data[pos:end]).lower(), end + 1
    while pos < n and data[pos] not in _SPACE and data[pos:pos + 1] != b">":
        value += data[pos:pos + 1].lower()
        pos += 1
    return bytes(name), bytes(value), pos


def _from_content(content: bytes) -> bytes | None:
    """The standard's 'extract a character encoding from a meta element'."""
    index = content.find(b"charset")
    while index != -1:
        rest = content[index + 7:].lstrip(_SPACE)
        if rest.startswith(b"="):
            rest = rest[1:].lstrip(_SPACE)
            if rest[:1] in (b'"', b"'"):
                end = rest.find(rest[:1], 1)
                return rest[1:end] if end != -1 else None
            match = re.match(rb"[^\s;]+", rest)
            return match.group(0) if match else None
        index = content.find(b"charset", index + 7)
    return None


def prescan(body: bytes) -> str | None:
    """The charset a real `<meta>` in the first 1024 bytes declares, or None."""
    data = body[:PRESCAN_BYTES]
    n = len(data)
    pos = 0
    while pos < n:
        if data.startswith(b"<!--", pos):
            end = data.find(b"-->", pos + 4)
            pos = n if end == -1 else end + 3
            continue
        low = data[pos:pos + 6].lower()
        if low[:5] == b"<meta" and len(low) == 6 and (low[5] in _SPACE or low[5:6] == b"/"):
            pos += 5
            charset: bytes | None = None
            http_equiv = False
            content: bytes | None = None
            seen: set[bytes] = set()
            while True:
                got = _get_attribute(data, pos)
                if got is None:
                    break
                name, value, pos = got
                if name in seen:
                    continue
                seen.add(name)
                if name == b"http-equiv" and value == b"content-type":
                    http_equiv = True
                elif name == b"content" and content is None:
                    content = value
                elif name == b"charset" and charset is None:
                    charset = value
            candidate = charset if charset is not None else (
                _from_content(content) if http_equiv and content is not None else None)
            if candidate:
                label = _label(candidate.decode("ascii", "replace"))
                if label in _UTF16:
                    return "utf-8"
                if label == "x-user-defined":
                    return "windows-1252"
                return label
            continue
        if data[pos:pos + 1] == b"<" and re.match(rb"</?[A-Za-z]", data[pos:pos + 3]):
            closing = data[pos + 1:pos + 2] == b"/"
            match = re.match(rb"</?([A-Za-z][^\s/>]*)", data[pos:])
            name = match.group(1).lower() if match else b""
            pos += len(match.group(0)) if match else 1
            while True:
                got = _get_attribute(data, pos)
                if got is None:
                    break
                pos = got[2]
            pos += 1  # past '>'
            if not closing and name in (b"script", b"style"):
                end = data.lower().find(b"</" + name, pos)
                pos = n if end == -1 else end
            continue
        if data.startswith((b"<!", b"</", b"<?"), pos):
            end = data.find(b">", pos + 2)
            pos = n if end == -1 else end + 1
            continue
        pos += 1
    return None


def choose(body: bytes, header_type: str | None, kind: str) -> str | None:
    """The charset label `decode` would use, without keeping the text."""
    return decode(body, header_type, kind)[1]


def decode(body: bytes, header_type: str | None, kind: str) -> tuple[str | None, str | None, list[str]]:
    """Decode by the order above. `kind` is "html", "xml" or "text". Returns
    (text or None if the declared charset is not allowed, label, warnings)."""
    warnings: list[str] = []
    for bom, label in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16le"),
                       (codecs.BOM_UTF16_BE, "utf-16be")):
        if body.startswith(bom):
            return body[len(bom):].decode(CHARSET_ALLOWLIST[label], errors="replace"), label, warnings
    declared: str | None = None
    if header_type:
        found = _HEADER_CHARSET.search(header_type)
        if found:
            declared = _label(found.group(1))
    if declared is None and kind == "xml":
        found_xml = _XML_DECL.match(body.lstrip()[:300])
        if found_xml:
            declared = _label(found_xml.group(1).decode("ascii", "replace"))
    if declared is None and kind == "html":
        declared = prescan(body)
    if declared is not None:
        chosen = CHARSET_ALLOWLIST.get(declared)
        if chosen is None:
            return None, declared, warnings
        text = body.decode(chosen, errors="replace")
        if "�" in text:
            warnings.append(f"invalid_bytes_for_charset_{declared}_replaced")
        return text, declared, warnings
    try:
        return body.decode("utf-8"), "utf-8", warnings
    except UnicodeDecodeError:
        warnings.append("charset_undeclared_decoded_as_windows-1252")
        return body.decode("cp1252", errors="replace"), "windows-1252", warnings
