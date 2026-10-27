"""Discovery without the live web, for `as_of` runs (CONTRACTS section 0d).

Two ways to find sources, neither of which can show what was published on or after
`as_of`:
- `page_links`: the links in a page already captured, which in an `as_of` run is an
  archived copy dated before `as_of`. A worker browses: an archived front page, its
  links, each captured through the archive in turn.
- `edgar_search_url` and `edgar_hits`: EDGAR full-text search, its end date set by the
  host to the day before `as_of`, never by the caller, and each hit filed before
  `as_of` kept, every other one withheld.

Pure functions over bytes and JSON; the service does the fetching and the storing.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urldefrag, urljoin, urlsplit

from stemma.evidence.errors import EvidenceError

EDGAR_SEARCH_ENDPOINT = "https://efts.sec.gov/LATEST/search-index"
# EDGAR full-text search starts with 2001 filings: the earliest start date it takes.
EDGAR_FIRST_DAY = "2001-01-01"
# Resource limits: links listed from one page, characters of each link's text, hits
# returned by one search page, and search pages an agent may ask for.
LINKS_MAX = 500
LINK_TEXT_MAX_CHARS = 200
EDGAR_PAGE_SIZE = 100
EDGAR_MAX_PAGE = 20
# Resource limit: characters of one full-text query.
EDGAR_QUERY_MAX_CHARS = 500
_FORM = re.compile(r"[A-Z0-9][A-Z0-9/-]{0,15}")
_CIK = re.compile(r"\d{1,10}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A Wayback replay URL; group 2 is the page it is a copy of.
_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/(\d{1,14})(?:[a-z]{2}_)?/(.+)$")


def original_url(url: str) -> str:
    """The page a Wayback replay URL is a copy of; any other URL as it is."""
    match = _WAYBACK.match(url)
    if match is None:
        return url
    inner = match.group(2)
    return inner if re.match(r"^https?://", inner) else "http://" + inner


def page_links(markup: bytes, charset: str | None, base_url: str) -> dict[str, Any]:
    """Every distinct http(s) link in an HTML page, made absolute against the page's own
    URL (or its `<base href>`), with its text. A link into the Wayback Machine is given
    as the page it copies: capturing that page in an `as_of` run takes its archived copy
    dated before `as_of` (section 4), never the one the link names."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser", from_encoding=charset or None)
    base = original_url(base_url)
    tag = soup.find("base", href=True)
    if tag is not None:
        base = urljoin(base, original_url(str(tag["href"]).strip()))
    seen: dict[str, str] = {}
    other = 0
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        url = original_url(urldefrag(urljoin(base, href)).url)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            other += 1  # mailto:, javascript:, data:, tel: and the like
            continue
        text = " ".join(a.get_text(" ", strip=True).split())[:LINK_TEXT_MAX_CHARS]
        if url not in seen or (not seen[url] and text):
            seen[url] = text
    links = [{"url": u, "text": t} for u, t in seen.items()]
    return {"base_url": base, "links": links[:LINKS_MAX],
            "truncated": len(links) > LINKS_MAX, "not_web_links": other}


def edgar_search_url(query: str, as_of: str | None, forms: list[str] | None,
                     cik: str | None, page: int) -> str:
    """The full-text search request. Its end date is the day before `as_of`, set here
    from the run, so no caller argument can move it; every argument is checked and
    quoted, so none can add a parameter."""
    if not isinstance(query, str) or not query.strip():
        raise EvidenceError("bad_query", "say what to search for")
    if len(query) > EDGAR_QUERY_MAX_CHARS:
        raise EvidenceError("bad_query", f"a query is at most {EDGAR_QUERY_MAX_CHARS} characters")
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= EDGAR_MAX_PAGE:
        raise EvidenceError("bad_page", f"page is a whole number from 1 to {EDGAR_MAX_PAGE}")
    params = [("q", query.strip())]
    if as_of is not None:
        if not _DATE.fullmatch(as_of):
            raise EvidenceError("bad_run_json", f"run.json as_of is not a date: {as_of!r}")
        last = (date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
        params += [("dateRange", "custom"), ("startdt", EDGAR_FIRST_DAY), ("enddt", last)]
    if forms:
        if not isinstance(forms, list) or not all(
                isinstance(f, str) and _FORM.fullmatch(f) for f in forms):
            raise EvidenceError("bad_form", "forms are SEC form codes such as 10-K or S-1")
        params.append(("forms", ",".join(forms)))
    if cik is not None:
        if not isinstance(cik, str) or not _CIK.fullmatch(cik):
            raise EvidenceError("bad_cik", "a CIK is up to ten digits")
        params.append(("ciks", cik.zfill(10)))
    if page > 1:
        params.append(("from", str((page - 1) * EDGAR_PAGE_SIZE)))
    return EDGAR_SEARCH_ENDPOINT + "?" + "&".join(
        f"{k}={quote(v, safe='')}" for k, v in params)


def edgar_hits(body: bytes, as_of: str | None) -> list[dict[str, Any]]:
    """The hits of one search answer: accession, form, filing date, filer names, CIKs and
    the matching document. In an `as_of` run a hit filed on or after `as_of`, or with no
    readable filing date, is withheld without a trace: even a count of such hits would
    say that later filings exist. No total and no highlighted text is returned."""
    try:
        data = json.loads(body.decode("utf-8"))
        raw = data["hits"]["hits"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise EvidenceError("search_unreadable",
                            f"EDGAR full-text search answered with something unreadable: "
                            f"{type(exc).__name__}") from exc
    if not isinstance(raw, list):
        raise EvidenceError("search_unreadable", "EDGAR full-text search gave no hit list")
    out = []
    for hit in raw:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(src, dict):
            continue
        filed = src.get("file_date")
        if not isinstance(filed, str) or not _DATE.fullmatch(filed):
            continue
        if as_of is not None and not filed < as_of:
            continue
        hid = str(hit.get("_id") or "")
        accession = str(src.get("adsh") or hid.split(":", 1)[0])
        document = hid.split(":", 1)[1] if ":" in hid else None
        out.append({"accession": accession, "form": src.get("form"), "filing_date": filed,
                    "filers": [str(n) for n in src.get("display_names") or []],
                    "ciks": [str(c) for c in src.get("ciks") or []], "document": document})
    return out
