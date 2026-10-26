# Part A: the evidence service, builder's notes

Branch `claude/part-a-evidence-077a8d`. Code in `src/stemma/evidence/`, tests in
`tests/evidence/`. Every tool of contract section 4 exists as a plain Python method
on `EvidenceService` (`service.py`) and as an MCP tool registered by
`register_tools(server)` in `tools.py`. Part B's register calls
`stemma.evidence.api.check_quote(run_dir, receipt_id, quote)` and
`stemma.evidence.api.read_text(run_dir, receipt_id)`.

## What works, and how it was shown

Unit tests: `pytest tests/evidence` runs 234 tests in about a second, with no
network (a fake transport, a fake SEC backend, invented pages, a PDF built by hand
in `conftest.py`). `ruff check` and `mypy --strict` pass on `src/stemma/evidence`.
The live tests are marked `live` and skipped unless `STEMMA_LIVE=1`; they were run
once on 26 October 2026 (results below).

### Capture (`capture`, `capture_file`)
- One code path from a URL to a receipt. Every attempt writes
  `store/receipts/<receipt_id>.json`, failures included (refused address, 404, 403,
  429, 5xx, timeout, too large, too many redirects, bad charset, unsupported type).
  An error page's bytes are kept (a wall says what it is) but never become text.
- Bytes are written once under their sha256, read-only. A second writer of the
  same bytes checks the file matches; a mismatch is the typed error `store_corrupt`,
  never an overwrite (tested by forging a stored file).
- HTML and XML: markdownify 1.2.3 over the whole document, only `<script>` and
  `<style>` removed, ATX headings, escaping off so the text reads as the page does.
  PDF: pypdfium2 5.14.0, page by page. Each receipt records the extractor and its
  version. A table row stays on one line (tested).
- Loud statuses: `empty_text` and `short_text`. Short is an extraction sanity
  flag (under 200 characters of text, or text under 0.5% of a body of 50 KB or
  more); it keeps the text whole and triggers the browser fallback.
- `capture_file` reads only under the run's `task/` folder, after resolving
  symbolic links (tested with a link pointing outside, `..`, absolute paths).

### Safety of the fetch
- Public web only: http and https on ports 80 and 443; no user name in the URL;
  numeric spellings such as `2130706433` or `127.1` refused; every address a name
  resolves to must be global (IPv4-mapped, 6to4 and Teredo forms unwrapped). The
  rule runs on the first URL and at every redirect hop, and the socket connects to
  the address that was checked, never to a fresh lookup (so a short DNS lifetime
  cannot swap in a private address). Hostile tests: 14 private literals, 5 numeric
  spellings, a redirect to loopback, a redirect to a name resolving privately, a
  name resolving to one public and one private address.
- Recorded limits on every receipt: 64 MiB body (checked against Content-Length
  and while streaming), 5 redirects, 30 s per read, 120 s per attempt. A body past
  its limit is dropped, never stored part-way. An unrequested gzip body is
  decoded with a size cap (a gzip bomb test) and its wire sha256 recorded.
- Charset allowlist (WHATWG labels minus escape-interpreting ones): a page
  declaring `utf-7` gets `unsupported_charset`, no text.
- No proxy, no cookies, `Accept-Encoding: identity`. SEC hosts get the SEC
  identity as User-Agent; other hosts never see it.
- I did not port the old broker's robots.txt handling; the brief did not ask for it.

### Browser fallback (optional extra `evidence-render`, Playwright 1.64.1)
Tried only when a plain HTML capture is `empty_text` or `short_text`, never in
`as_of` mode (archived pages would load live scripts). The rendered page is stored
as its own bytes with its own receipt (`kind: rendered`, `fallback_of` naming the
plain receipt). Every request the browser makes is checked by the same address
rule and aborted if refused. Live run: the World Bank's GDP indicator page came
back `short_text` from the plain fetch (5,980 characters from 1.28 MB, the figures
sit in script data); the rendered capture was `ok` with 24,026 characters; one
browser request was refused (a host whose name did not resolve at all; unresolvable names are refused like private ones).
Known gap: the browser resolves names itself, so its check and its
connection are two lookups; the plain fetch does not have this gap.

### Reading in parts, search, quote checks
- `outline(sha256)`: headings (HTML) or pages and bookmarks (PDF), each with start
  and end offsets. `read(sha256, start, length)` returns at most 20,000 code
  points with `next_start`; `read_section(sha256, anchor)` returns one section,
  capped the same way. Reading every part in turn reproduces the stored text
  exactly (tested).
- `search_store`: SQLite FTS5. `words` uses the porter tokenizer over chunks of
  about 1,500 characters (ranked, snippet, the chunk's offsets). `exact` uses the
  trigram tokenizer to find candidate documents, then an exact, case-sensitive
  substring search gives each hit's offsets. Agent queries are quoted, so FTS5
  syntax cannot be injected (tested with `" OR *`, `NEAR(...)` and others).
- `check_quote`: exact match after one documented normalisation
  (`whitespace-runs-to-one-space;typographic-quotes-to-ascii;token-and-quantity-edges;v4`):
  every run of Unicode whitespace becomes one space; curly and typographic quotation
  marks become straight ones; a match must sit on token boundaries and never start
  or end in the middle of a number (a digit, or digit-separator-digit, across
  either edge, where `,` `.` `'` and the no-break and thin spaces are separators;
  and not just after a lone minus or plus sign). So "250 jobs" is not found in
  "1,250 jobs", "$1" not in "$1.5 billion", "25%" not in "0.25%", "3.2%" not in
  "-3.2%". Nothing else changes (not case, dashes or digits). Offsets are in the
  stored text, before normalisation. Each match lists the flagged `regions` it
  lies in (hidden HTML, invisible or white PDF text, a table with merged cells),
  and `warnings` says so: a warning for the checker, never a refusal. A receipt
  without text raises `no_text`. Exact search uses the same whitespace and
  quotation-mark normalisation, so a no-break space matches a space in both.
- Stored text uses `\n` line endings; offsets are Unicode code points throughout.

### The request-keyed cache (`$STEMMA_CACHE`)
Keyed by kind, URL as asked, `as_of` and extractor versions. Only successful
responses are cached. A second run gets cached bytes only for the same request,
copied into its own store, with `served_from_cache: true` and the original fetch
time. A tampered entry (bytes not matching their recorded sha256) is ignored with a
warning and fetched afresh. All tested.

### `as_of` mode and the archive question (contract section 10)
**Answer: the Internet Archive's CDX API, followed by an `id_` replay, gave a copy
dated before the date in 28 of 30 attempts; its availability API is unfit for this
use, because in 15 of the same 30 it pointed at a copy dated on or after the date.**

How the service does it: when `run.json` has an `as_of` date, `capture` asks CDX
for captures up to the day before, keeping only those the archive recorded as HTTP
200, takes the newest, and fetches it as `/web/<timestamp>id_/<url>` (the bytes as
the site served them, no toolbar or rewriting). Every hop must stay inside the
archive with a timestamp before `as_of`, and a `Memento-Datetime` header must be
before it too; otherwise the capture fails `no_snapshot_before_as_of`. An archive
that is down or rate limited fails `archive_unavailable` after three tries. Each
archive receipt records the lookup, the snapshot timestamp and its age in days.
Hostile tests cover a CDX row dated on `as_of`, a redirect to a later capture, a
redirect out of the archive and a later Memento date.

The live study (`test_live_archive_reliability`, run once): 15 public URLs
(government statistics pages and a PDF, central banks, an international body, an
encyclopedia article, company home and investor pages, a newspaper front page, a
SEC search page, a reserved example domain) at two dates (1 March 2020, 1 July
2023), 30 attempts.
- 28 succeeded. Both failures were the SEC search page (a dynamic page with a
  query string), which CDX listed no capture of: a true "no snapshot", typed.
- One CDX lookup needed a second try (the archive was slow); the longest attempt
  took 35 s, most took 2 to 5 s.
- "Before the date" is not "recent": the investor page's newest good capture
  before 1 July 2023 was from 30 August 2018. That is why receipts carry the
  snapshot's age; judging whether it is current enough is the checker's job.
- The availability API (`archive.org/wayback/available`) returned a later copy in
  15 of 30 and no copy at all in 9 cases where CDX found one. It is not used.
- Not tested: other archives (archive.today, national libraries via Memento
  aggregators). The Internet Archive alone covered every page that had any capture.

Also in `as_of` mode: SEC filings are fetched from SEC itself (they are dated and
immutable) but refused with `filed_after_as_of` if filed on or after the date;
`sec_filings` and `sec_facts` default `filed_before` to `as_of` and never look past
it. The raw XBRL concept answer (which lists later filings too) is kept as bytes
but its text is withheld (`text_withheld_as_of`), so later figures never become
searchable or readable through the tools.

## SEC tools
edgartools 5.62.1 resolves the company and lists filings (with filing dates and
primary documents); every byte an agent reasons about is fetched by our own
fetcher and stored with a receipt. `sec_facts` reads SEC's company-concept data,
which lists every fact once per filing that reported it, so restatements stay
visible as separate rows (never collapsed to the latest figure); `filed_before`
filters on each row's own filing date. Each fact carries `filing_receipt_id`, the
receipt of the filing's primary document, captured for the newest 12 distinct
filings (a resource limit); facts from older filings get `null` and a warning
saying to call `sec_capture`. Unit tests use an invented company whose fiscal-2022
revenue is restated in the next filing. `STEMMA_SEC_IDENTITY` ("Name email") is
required; without it the SEC tools fail `sec_identity_not_configured`. edgartools' own
lookup cache goes under `$STEMMA_CACHE/edgartools` (or the run's
`store/derived/`), never the home folder.

**Shown live once (26 October 2026)** with the owner-approved SEC contact
identity, passed only through `STEMMA_SEC_IDENTITY` at run time (the orchestrator
relayed the owner's ruling). The value is never committed or written to any file:
receipts record only `sec_identity_header_sent: true`, and a search of the repo,
its history, the live outputs and the run folder (edgartools' cache included)
found it nowhere. With the variable unset, every SEC tool fails
`sec_identity_not_configured` before any request; there is no default (tested).
Results, in 6.5 s: `sec_filings("AAPL", form="10-K", filed_before="2025-01-01")`
listed 31 filings, newest filed 1 November 2024; `sec_capture` stored its primary
document (1,503,889 bytes, 269,262 characters, `ok`, 0.34 s);
`sec_facts(..., "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
filed_before="2025-01-01", form="10-K")` returned 34 as-filed facts from 6
filings, every one with its filing's receipt; 10 periods appear in more than one
filing, each version kept as its own row. A filing's HTML has no heading tags, so `sec_outline.py` adds Item anchors for
10-K, 10-Q and 8-K captures (and amendments): Item heading lines outside tables,
the last per (Part, Item) so a contents list loses to the body, cross-references
such as "Item 7 of this report" ignored, each section ending at the next Item or
Part. Anchors are `item-7`, `item-1a`, `item-2.02`, or `part-ii-item-1` where a
10-Q repeats an id across parts; `read_section` jumps to them. Unit-tested on
invented 10-K, 10-Q and 8-K shapes (styled headings, a contents table, a
plain-text contents list, a title on the next line). Live (Apple, filed before
2025): the 10-K got all 27 Part and Item anchors, Item 7 running 19,218
characters; the 10-Q got Part I and Part II items told apart; the 8-K got Items
2.02 and 9.01. A filing captured first through plain `capture` keeps the outline
of that first extraction, without Items.

## Docling table view (optional extra `evidence-tables`)
`tables(sha256)` converts the stored bytes with Docling once, keeps the result in
`store/derived/<sha256>.tables.json` with Docling's version, and returns it marked
as a derived view never used for quoting. Without Docling it returns
`not_installed` (unit-tested, as is the cached path with a fake converter).
Shown once for real, in a separate environment in my scratchpad (Docling 2.142.1,
about 1.2 GB with PyTorch): an invented HTML table came back as one Markdown table
with every figure (39 s, mostly loading models); the 39-page BLS release PDF gave
31 tables in 114 s, with headers such as "Sept. 2025 | July 2026 | Aug. 2026 |
Sept. 2026" kept as columns. Its output passed the 200,000-character return limit
(Docling pads cells with spaces), so the call said `truncated: true`. A call that
long blocks that tool call for two minutes; it is not run unless asked for.

## Hostile probe by an independent helper
A fresh helper (Opus 5.5, not the author) read the contract and the code and wrote
70 probes against the guards, using fakes only. 32 failed at first, in 13 findings.
Twelve are fixed, each with a regression test in `tests/evidence/test_hostile.py`:
- a Wayback redirect to a year-only timestamp (`/web/2025id_/`) was accepted as
  before `as_of`; partial and invalid timestamps are now refused;
- the same bytes arriving under another content type or charset crashed with no
  receipt; the first extraction of a set of bytes now stands and later receipts
  say they reused it;
- non-ASCII URLs crashed the real transport; hosts are now IDNA-encoded and paths
  and queries percent-encoded;
- a Memento date without a time zone, or an impossible timestamp, crashed;
- `capture_file` crashed on a NUL or lone surrogate in the path, an unreadable
  file and a symbolic-link loop, and accepted a hard link to a file outside
  `task/`; all now give typed receipts, and multiply-linked files are refused;
- a cache entry copied under another request's key was served; entries now name
  their key;
- a NUL in a search query raised a SQLite error; it is now `bad_query`;
- quotes matched inside numbers ("5%" inside "15%"); the token-boundary rule above;
- a 40-page PDF with only page numbers came back `ok`; PDFs averaging under 100
  characters a page are now `short_text` with a "may be scanned" warning;
- site-local IPv6 (`fec0::/10`) was allowed; now refused;
- in dated SEC answers, SEC's `frame` field (reassigned to the latest filing) is
  dropped;
- ids with a trailing newline passed the patterns; patterns now match whole.
One stays open: an agent able to write `$STEMMA_CACHE` could rewrite both files of
an entry consistently and have forged bytes served. Code cannot prevent that; the
cache folder must be outside agents' write access (see below). The helper did not
probe the browser fallback; I added WebSocket blocking and a redirect-hop check,
and its separate DNS lookup remains a known gap.

## Live run, 26 October 2026 (outputs kept outside the repo, in my scratchpad)
- Statistics page (BLS employment situation release, HTML): `ok`, 61,940 bytes,
  22,589 characters, 0.25 s. The PDF of the same release: `ok`, 580,018 bytes,
  39 pages, 155,561 characters, 0.39 s. In both, an exact search for a figure found
  it at the right offsets and a quotation around it checked.
- `as_of` 2024-01-01 capture of the same page: `ok`, snapshot of 31 December 2023
  00:21 UTC, Memento date matching, 2.5 s.
- Browser fallback and archive study: above.

## Contract points for the orchestrator
- `sec_facts` takes one optional argument beyond the contract, `form` (for
  example `10-K`), to keep only facts from that form. Callers that omit it get the
  contract's behaviour.
- `sec_facts` captures at most 12 filings per call for the per-fact receipts; the
  rest are named and left to `sec_capture`. If facts must always carry a receipt,
  the limit needs raising (each 10-K is about 1 to 2 MB and a few seconds).
- Optional extras: I added `evidence-render` (Playwright) and `evidence-tables`
  (Docling) groups beside `evidence` in `pyproject.toml`, since the brief calls
  both optional. Playwright also needs `playwright install chromium-headless-shell`.
- `.env.example` should gain the name `STEMMA_SEC_IDENTITY=` with no value (I do not own
  that file).
- `$STEMMA_CACHE` must be tool-only (no Bash write, no Write or Edit), like
  `store/`. Section 6 does not list it today.
- In `as_of` runs, part B may want to deny agents' Read of `store/bytes/` (raw
  bytes of withheld SEC answers); the tools never show them.
- Receipt ids look like `r_20261026T040948_8c50c0396d96` (UTC time plus random).
- Tools taking `sha256` for stored text also accept a `text_id`
  (`sha256~<variant>`); receipts carry `text_id`.
- The browser fallback is a run setting, `run.json` `"evidence":
  {"browser_fallback": true}`, off by default (driver, part B).
- `check_quote` raises typed errors for unusable input (`unknown_receipt`,
  `bad_receipt_id`, `no_text`, `empty_quote`) and returns `found: false` only when
  the text exists and the quote is not in it.

## Merge-gate review fixes (26 October 2026)
A non-author review of `b67296e` found ten problems and three suspected ones. Each
is now reproduced as an attack in `tests/evidence/test_review_gate.py` (56 tests),
and the reviewer's own 12 probe tests all pass (they failed 8 of 12 before):
1. Quotes matched inside numbers: the number-edge rule above.
2. markdownify fused figures: superscripts and subscripts are stored as `^...^`
   and `~...~` (so "$5<sup>1</sup> billion" is "$5^1^ billion"); block-like
   elements (header, footer, figure, nav, aside, address, details, summary,
   option, title and more), MathML parts, namespaced elements and touching XML
   elements are separated by a space. `<?xml?>` processing instructions are
   markup and no longer appear as text. Nothing in the page's text is deleted.
3. Charset: the WHATWG prescan (`charset.py`): real `<meta>` elements only (not
   comments, attributes or script strings), UTF-16 in a meta means UTF-8,
   `x-user-defined` means windows-1252, quoted header labels read, and the XML
   declaration's encoding used for XML.
4. Truncated bodies: the body must match Content-Length; a truncated gzip
   stream or trailing non-gzip bytes fail typed; every gzip member is decoded. A
   PDF without an end-of-file marker is `short_text` with a warning.
5. The total deadline now holds across connect, TLS, headers and every body
   read: a watchdog shuts the socket at the deadline (tested with drip-fed headers
   and body on a loopback server).
6. `capture_file` walks the path one component at a time with `O_NOFOLLOW`
   (`openat`), so no symbolic link is followed at any level; the receipt's
   `source_path` is the path actually opened. A 3-second symlink-swap race finds
   nothing outside `task/`.
7. "Implausibly short" counts visible words (link targets, image sources, hidden
   ranges and zero-width characters left out): fewer than 40 is `short_text` and
   tries the browser; none is `empty_text`.
8. `sec_facts(form="10-K")` keeps 10-K/A facts, labelled by their own form, with
   a warning counting them.
9. A Content-Length that is not ASCII digits is a typed `transport_error` with a
   receipt. Any unexpected fault inside a capture now leaves an `internal_error`
   receipt instead of escaping.
10. Hidden HTML (the `hidden` attribute, inline `display:none` or
   `visibility:hidden`, `<template>`), invisible PDF text (render mode 3, or
   transparent) and white PDF text are kept, with their ranges in the receipt's
   `flagged_ranges`; so are tables with `rowspan` or `colspan`. `check_quote`
   warns on a match inside any of them.
Suspected, all real and fixed: in `as_of` runs a ticker or name lookup warns that
today's mapping was used (pass the CIK); archive redirect hops must be https;
exact search normalises like `check_quote`. Found while fixing: fullwidth dots
and digits (`127。0。0。1`) now meet the address rules after IDNA mapping, before any
lookup; a page's `<title>` no longer runs into its first body text.
Live re-run after the fixes: the BLS page and PDF `ok`; Apple's 10-K `ok` with 27
Item anchors, 63 merged-cell tables and its hidden XBRL header flagged; the
browser fallback still `ok`.

## Cross-family review fixes (26 October 2026)
A Codex review of `cbefb29` found eight problems and two suspected ones, all real.
Each is reproduced first in `tests/evidence/test_review_codex.py` (36 tests; 28
failed before the fixes):
1. PDF visibility flags: text is now built per PDFium character index, so a NUL
   glyph (stored as U+FFFD) cannot misalign or switch off the flags, and render
   mode 7 (clip only) counts as invisible with mode 3.
2. Quote edges: no match may start or end where the neighbouring character would
   change the quantity or its sign: a decimal point (".5"), a sign after a space
   or in an exponent ("-$5", "1e-3"), a currency sign ("$5", "5€"), a percent or
   per-mille sign, an exponent marker. Mirror cases at the end are covered.
   Normalisation is now `...;token-and-quantity-edges;v4`.
3. Digits from two different elements never meet: a space is put between them
   and recorded as an `element_split` range; `check_quote` refuses a match with an
   edge at a split (CSS may show the digits joined), and warns on one spanning it.
4. TLS: the watchdog now shuts the wrapped socket (wrapping had detached the raw
   one), so a TLS header drip stops at the deadline (tested over real TLS on
   loopback with a throwaway certificate).
5. The browser fallback is off unless `run.json` says
   `"evidence": {"browser_fallback": true}`. When on, Chromium's resolution is
   pinned to the checked address (`--host-resolver-rules`, every other name
   unresolvable) and every request but the main-frame navigation to the checked
   URL is aborted. Negative result, live: the World Bank page that the earlier
   fallback recovered (5,980 to 24,026 characters) now stays `short_text`, since
   its figures come from external scripts (13 requests refused).
6. Every response header is read for the checks (up to a recorded 256 KiB; past
   it, a typed failure); the receipt records a bounded copy. An archive replay must
   carry a Memento-Datetime and every one must be before `as_of` (a missing one
   refuses). Live: Wayback sends one, and the `as_of` capture still works.
7. A non-success status in the browser path is a typed failure (`forbidden`,
   `not_found`, `http_error`) with no text.
8. The final attachment component opens with `O_NONBLOCK`, so a FIFO under
   `task/` is refused at once as not a regular file.
Suspected, both real and fixed: PDFium now runs in a child process
(`pdfworker.py`, `python -I`) under a recorded 120-second limit and a text limit;
and stored text is keyed by the bytes plus the content type, the charset chosen
and the SEC form (`text_id`: the sha256, or `sha256~<variant>` for another
reading of the same bytes), so the same bytes read another way get their own text
instead of reusing the first. `outline`, `read`, `read_section` and `search_store`
accept a `text_id` wherever they take a sha256; hits carry both.

**Known limits, recorded rather than fixed.**
- A PDF whose ToUnicode map lies (a glyph drawn as "1" but mapped to "9", so a
  visible 1,250 is stored as 9,250) cannot be caught from the text layer. The
  claim checker can view the page image with the Read tool.
- Hiding by CSS classes, off-screen positioning, zero font size or text coloured
  like its background in HTML is not detected (stylesheets are removed, and only
  inline styles are read).
- Inline elements whose digits meet are now split and recorded (item 3 above),
  so a quote cannot start or end there even where the page shows them joined.
- The prescan skips `<script>` and `<style>` contents, a deliberate difference
  from the standard (a string in a script never declares a charset).
- Range edges can be off by surrounding whitespace.

## For the merge review: where I am least sure, and where to attack
**Least sure of.**
- Table fidelity. markdownify flattens `rowspan` and `colspan` without
  re-aligning cells (now flagged, and warned on a quote), and pypdfium2 reads
  multi-column PDF tables in text order (not flagged).
- The stdlib transport. It was shown on fakes and about 25 live hosts only.
- The Item outline. It has run on one company's recent filings, and never on
  old plain-text filings.
- The edgartools listing. It is read column by column, and only one company
  has gone through it live.

**Inputs not tested.**
- PDFs: scanned PDFs needing OCR, encrypted PDFs live, and invisible or
  white-on-white text (the old broker marked invisible PDF text; this does not).
- HTML hidden by CSS (`display:none`), which is kept as text.
- Live pages that are not UTF-8, and live JATS XML.
- Bodies near the 64 MiB limit, and IPv6-only hosts.
- Brotli bodies (refused, typed).
- SEC: filings before 2001, 10-K/A, and company names that resolve ambiguously.

**Where I would attack, to get wrong text or a wrong figure in silently.**
- Plant a figure in hidden HTML or invisible PDF text. It becomes quotable,
  "observed" text.
- Use a table with spanning cells, so the quoted row binds the figure to the
  wrong column.
- Get the archive to hold a pre-date 200 capture that was really a parked page,
  a soft 404 or a holding page. It passes every date check.
- Name the company ambiguously. `sec_facts` trusts edgartools' resolution of the
  company to a CIK, and SEC's `filed` field.
- The browser fallback, if a run enables it: Chromium's resolver is pinned and only
  the main document loads, but the page's inline scripts still run.
