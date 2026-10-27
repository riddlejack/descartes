# Live probe of as-of discovery, 27 October 2026

**Result: nothing dated on or after the cutoff came back through browsing, the
FRED series or EDGAR full-text search.** The probe ran at as_of 2026-10-01, the CoreWeave demo's date, so
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
  So for the rehearsal the investor site is reachable only through the newsroom and
  through filings.
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
  defeating a bot check, which we do not do.
- **Limit for the demo: no dated price series for shares.** The orchestrator ruled (27
  October) that no other price source is added tonight: none whose terms have not been
  read, and none that needs a bot check passed. Agents take share prices from archived
  quote pages and dated reporting, captured through the archive before the cutoff, and
  say so where a price matters.

## EDGAR full-text search (works)
Run with the SEC contact identity in the environment only (owner ruling 33; it is in no
file or record, and the receipts record only that the header was sent). The host set
the end date to 30 September.
- "CoreWeave", any form: 100 hits on the first page, filed 2021-06-24 to 2026-09-28
  (10-K, 10-Q, 8-K, S-1 and S-1/A, S-4, 424B prospectuses, 13D and others).
- "CoreWeave", forms S-1, 424B4, 10-Q and 8-K: 100 hits, filed up to 2026-09-25.
- "CoreWeave", CoreWeave's own CIK (1769628): 100 hits, filed 2022-03-01 to 2026-09-22.
- No hit was filed on or after the cutoff.

## A fix the probe's review found
While confirming that XBRL facts are filtered by filing date (they are), the review
found that the raw company-concept answer, later facts included, was stored for audit
where a worker can read it. In an as-of run it is no longer stored (commit 365a5ba).
