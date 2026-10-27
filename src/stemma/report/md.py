"""A small Markdown renderer for the run's own texts (deliverable, reviews, verdicts).

It covers what the run's agents write: headings, paragraphs, bullet and numbered lists,
tables, emphasis, inline code, fenced code and links. Every character of the source is
HTML-escaped before any markup is added, and a link becomes one only for http(s) URLs,
so a source that holds HTML or script shows as text. Claim tags (`[[c:...]]`) are handed
to a callback; `[[nc:...]]` tags are dropped (they are for the mechanical reader).
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable

from stemma.register import tags

TagRenderer = Callable[[str], str]  # 12-hex claim id -> HTML

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_CTAG_ESC = re.compile(r"\[\[c:([0-9a-f]{12})\]\]")


def _no_tag(short: str) -> str:
    return ""


def inline(text: str, tag: TagRenderer = _no_tag) -> str:
    text = tags.NC_TAG.sub("", text)
    text = re.sub(r"[ \t]+(?=\[\[c:)", " ", text)
    out = html.escape(text, quote=False)
    codes: list[str] = []

    def keep_code(m: re.Match[str]) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE.sub(keep_code, out)
    # The URL is already escaped for text; only a double quote still needs it.
    out = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}" '
                              f'rel="noopener noreferrer">{m.group(1)}</a>', out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)
    out = _CTAG_ESC.sub(lambda m: tag(m.group(1)), out)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], out)


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row.removeprefix("|")
    row = row.removesuffix("|")
    return [c.strip() for c in row.split("|")]


def render(text: str, tag: TagRenderer = _no_tag, heading_shift: int = 0) -> str:
    """Markdown to HTML. `heading_shift` demotes headings (a `#` becomes `h{1+shift}`)."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    lists: list[tuple[str, int]] = []  # open list tags and their indents
    i = 0

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para), tag)}</p>")
            para.clear()

    def close_lists(to_indent: int = -1) -> None:
        while lists and lists[-1][1] > to_indent:
            out.append(f"</li></{lists.pop()[0]}>")

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            close_lists()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(body))}</code></pre>")
            i += 1
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        h = _HEADING.match(stripped)
        if h:
            flush_para()
            close_lists()
            level = min(6, len(h.group(1)) + heading_shift)
            out.append(f"<h{level}>{inline(h.group(2), tag)}</h{level}>")
            i += 1
            continue
        if (stripped.startswith("|") and i + 1 < len(lines)
                and _TABLE_SEP.match(lines[i + 1])):
            flush_para()
            close_lists()
            head = _cells(stripped)
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c, tag)}</th>" for c in head)
            body_rows = "".join("<tr>" + "".join(f"<td>{inline(c, tag)}</td>" for c in r)
                                + "</tr>" for r in rows)
            out.append(f'<div class="table"><table><thead><tr>{th}</tr></thead>'
                       f"<tbody>{body_rows}</tbody></table></div>")
            continue
        b = _BULLET.match(line)
        n = None if b else _NUMBERED.match(line)
        if b or n:
            flush_para()
            m = b or n
            assert m is not None
            indent = len(m.group(1).expandtabs(4))
            kind = "ul" if b else "ol"
            content = m.group(2) if b else m.group(3)
            if lists and indent > lists[-1][1]:
                out.append(f"<{kind}><li>")
                lists.append((kind, indent))
            else:
                close_lists(indent)
                if lists and lists[-1][1] == indent and lists[-1][0] == kind:
                    out.append("</li><li>")
                else:
                    if lists and lists[-1][1] == indent:
                        out.append(f"</li></{lists.pop()[0]}>")
                    out.append(f"<{kind}><li>")
                    lists.append((kind, indent))
            out.append(inline(content, tag))
            i += 1
            continue
        if lists and line.startswith((" ", "\t")):
            out.append(" " + inline(stripped, tag))  # a list item's continuation line
            i += 1
            continue
        close_lists()
        if stripped.startswith(">"):
            flush_para()
            out.append(f"<blockquote>{inline(stripped.lstrip('> '), tag)}</blockquote>")
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush_para()
    close_lists()
    return "\n".join(out)
