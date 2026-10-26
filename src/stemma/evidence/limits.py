"""Recorded limits of the evidence service.

Each constant is a resource limit (bytes, time, count) or an extraction sanity flag,
as its comment says. None decides whether a source is good: the sanity flags only
mark a receipt loudly and trigger the browser fallback; they never discard text.
Fetch limits are written onto every receipt they governed.
"""

from __future__ import annotations

# Resource limit: largest body accepted, in bytes (a long PDF or a 10-K fits).
MAX_BODY_BYTES = 64 * 1024 * 1024
# Resource limit: largest body after undoing an unrequested gzip or deflate coding.
MAX_DECODED_BYTES = 128 * 1024 * 1024
# Resource limit: redirects followed before the attempt stops.
MAX_REDIRECTS = 5
# Resource limit: seconds one socket read or connect may block.
READ_TIMEOUT_S = 30.0
# Resource limit: seconds one whole capture attempt (every hop) may take.
TOTAL_DEADLINE_S = 120.0
# Resource limit: bytes of response headers accepted (all are read for the checks).
MAX_HEADER_BYTES = 256 * 1024
# Resource limit: bytes of response headers recorded on a receipt.
MAX_RECORDED_HEADER_BYTES = 16 * 1024
# Resource limit: ports a capture may connect to. The public web, nothing else.
ALLOWED_PORTS = frozenset({80, 443})

# Resource limit: seconds between two requests to SEC hosts (SEC allows 10 a second;
# this stays at five).
SEC_MIN_INTERVAL_S = 0.2
# Resource limit: seconds between two requests to the Internet Archive.
ARCHIVE_MIN_INTERVAL_S = 1.0
# Resource limit: attempts at one archive lookup when the archive answers 429 or 5xx.
ARCHIVE_ATTEMPTS = 3

# Extraction sanity flag: an HTML or XML capture with fewer visible words than this
# (markup, link targets, hidden and zero-width text not counted) is marked
# `short_text` (kept whole) and tries the browser fallback.
SHORT_TEXT_MIN_WORDS = 40
# Extraction sanity flag: an HTML capture at least this many bytes long ...
SHORT_TEXT_RATIO_MIN_BYTES = 50_000
# ... whose text is a smaller share of its bytes than this is marked `short_text`.
SHORT_TEXT_MIN_RATIO = 0.005

# Extraction sanity flag: a PDF averaging fewer text characters a page than this is
# marked `short_text` (its pages may be scanned images). Text is kept whole.
PDF_MIN_CHARS_PER_PAGE = 100

# Resource limit: seconds the PDF extraction child process may run.
PDF_EXTRACT_TIMEOUT_S = 120.0
# Resource limit: characters of text one PDF may yield.
PDF_MAX_TEXT_CHARS = 50_000_000
# Resource limit: bytes of JSON the PDF extraction child may write.
PDF_MAX_OUTPUT_BYTES = 8 * PDF_MAX_TEXT_CHARS

# Resource limit: code points returned by one `read` or `read_section` call.
READ_MAX_CHARS = 20_000
# Resource limit: outline entries carried on a receipt (the `outline` tool returns more).
RECEIPT_OUTLINE_MAX = 100
# Resource limit: outline entries returned by the `outline` tool.
OUTLINE_MAX = 2_000
# Resource limit: hits returned by one search.
SEARCH_MAX_HITS = 50
# Resource limit: characters of context each side of a hit in a snippet.
SNIPPET_CONTEXT_CHARS = 80
# Resource limit: target size of the chunks indexed for word search.
WORDS_CHUNK_CHARS = 1_500
# Logical invariant: SQLite's trigram tokenizer cannot match fewer than 3 characters.
EXACT_QUERY_MIN_CHARS = 3
# Resource limit: matches reported by one quote check.
QUOTE_MAX_MATCHES = 20

# Resource limit: SEC filings returned by one `sec_filings` call.
SEC_FILINGS_MAX = 200
# Resource limit: distinct filings `sec_facts` captures to give facts their receipts.
SEC_FACTS_MAX_FILINGS = 12
# Resource limit: facts returned by one `sec_facts` call.
SEC_FACTS_MAX = 1_000

# Resource limit: characters of table Markdown returned by one `tables` call.
TABLES_MAX_CHARS = 200_000
