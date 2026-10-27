"""A small Markdown renderer for the run's own texts (deliverable, reviews, verdicts).

It covers what the run's agents write: headings, paragraphs, bullet and numbered lists,
tables, emphasis, inline code, fenced code and links.

Inline text is split, from the raw source, into tokens: code spans, links, claim tags
and plain text. Each token is escaped at the moment it is inserted, and nothing escaped
is ever put back inside an attribute. A link becomes a link only for an http(s) URL
made of characters that cannot leave a quoted attribute (no quote, backtick, angle
bracket, space or backslash), and that URL is escaped again when inserted. No
placeholder is used, so no text in the source can collide with one. Claim tags
(`[[c:...]]`) are handed to a callback that builds its own escaped HTML; `[[nc:...]]`
tags are dropped (they are for the mechanical reader).
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Final

from stemma.register import tags

TagRenderer = Callable[[str], str]  # 12-hex claim id -> HTML

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# One inline token: a code span, a link, or a claim tag (all on the raw source).
_TOKEN = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\[(?P<ltext>[^\[\]\n]{1,500})\]\((?P<url>https?://[^\s()<>\"'`\\]{1,2000})\)"
    r"|\[\[c:(?P<tag>[0-9a-f]{12})\]\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"
                     r"|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
# Resource limit: characters of one text rendered (a longer text is cut, and says so).
MAX_CHARS: Final = 2_000_000


def _no_tag(short: str) -> str:
    return ""


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def _plain(text: str) -> str:
    """Escaped text with emphasis. Emphasis adds only <strong> and <em>, never an
    attribute, so it is safe on escaped text."""
    out = esc(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)


def inline(text: str, tag: TagRenderer = _no_tag) -> str:
    text = tags.NC_TAG.sub("", text)
    text = re.sub(r"[ \t]+(?=\[\[c:)", " ", text)
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        out.append(_plain(text[pos:m.start()]))
        if m.group("code") is not None:
            out.append(f"<code>{esc(m.group('code'))}</code>")
        elif m.group("url") is not None:
            out.append(f'<a href="{esc(m.group("url"))}" rel="noopener noreferrer">'
                       f'{_plain(m.group("ltext"))}</a>')
        else:
            out.append(tag(m.group("tag")))
        pos = m.end()
    out.append(_plain(text[pos:]))
    return "".join(out)


def _cells(row: str) -> list[str]:
    row = row.strip().removeprefix("|").removesuffix("|")
    return [c.strip() for c in row.split("|")]


def render(text: str, tag: TagRenderer = _no_tag, heading_shift: int = 0) -> str:
    """Markdown to HTML. `heading_shift` demotes headings (a `#` becomes `h{1+shift}`)."""
    cut = len(text) > MAX_CHARS
    lines = text[:MAX_CHARS].replace("\r\n", "\n").split("\n")
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
            out.append(f"<pre><code>{esc(chr(10).join(body))}</code></pre>")
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
    if cut:
        out.append(f"<p><em>(The text is cut at {MAX_CHARS:,} characters, a resource "
                   f"limit.)</em></p>")
    return "\n".join(out)
