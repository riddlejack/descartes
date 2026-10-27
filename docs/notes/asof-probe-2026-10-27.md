# Live probe of as-of discovery, 27 October 2026

**Result: nothing dated on or after the cutoff came back through browsing or the
FRED series.** The probe ran at as_of 2026-10-01, the CoreWeave demo's date, so
everything had to be dated strictly before 1 October 2026 (00:00 UTC). It called the
broker's tools directly; it started no session and no research run. The raw record is
`asof-probe-2026-10-27.json` beside this note; the script was
`asof_probe.py`, kept in the session's scratch folder.

## Browsing archived pages (works)
- The investor site, `investors.coreweave.com/` and its press-releases page, has no
  archived capture before the cutoff ("CDX lists no capture"). In this demo the investor
  site is reachable only through the newsroom or through filings.
- `www.coreweave.com/newsroom` came back as the archive's copy of 24 September 2026,
  17:46:21 UTC (Memento-Datetime the same), five days before the cutoff.
- `archived_links` listed 151 links on it: news articles, product pages, whitepapers.
- Three links were captured through the archive in turn. The news article came back as
  the copy of 29 September 2026, 18:22:29 UTC; the newsroom again as 24 September.
- No capture, replay date or Memento-Datetime was on or after the cutoff.

## Market series (FRED works; stooq refused)
- FRED, with the end date set by the host to 30 September: DGS10 (10-year Treasury
  yield, 16,892 rows), BAMLH0A0HYM2 (high-yield spread, 774 rows) and SOFR (2,217 rows)
  each ended on 2026-09-30. Every stored row is dated before the cutoff.
- stooq refused both CoreWeave (crwv.us) and NVIDIA (nvda.us): stooq now answers its CSV
  download with a JavaScript proof-of-work page that checks for a browser. The tool
  refused it as not a series and stored nothing. Getting past that page would mean
  defeating a bot check, which we do not do. Daily share prices need another source.

## EDGAR full-text search (not run)
`sec_search` needs the SEC contact identity, `STEMMA_SEC_IDENTITY`, which is not set in
this checkout; the orchestrator supplies it at run time. This half of the probe waits on
it.

## A fix the probe's review found
While confirming that XBRL facts are filtered by filing date (they are), the review
found that the raw company-concept answer, later facts included, was stored for audit
where a worker can read it. In an as-of run it is no longer stored (commit 365a5ba).
