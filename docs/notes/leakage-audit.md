# The leakage audit

## What it answers

When a run predicts something from a web frozen at a cutoff date (its `as_of`), and is
graded against what became public later, the grade has to say whether the run could
have seen the answer. The leakage audit reads everything the run took in or said,
after the run, with no model call, and lists every place that shows a date on or
after the cutoff, or a figure or phrase the owner registered beforehand as the answer.
It judges nothing: each hit comes with where it is and where it came from, and the
grader decides what it means.

## How to run it

```bash
.venv/bin/python -I -m stemma.audit.leakage RUN_FOLDER --as-of 2026-10-19 --targets targets.json --out AUDIT_FOLDER
```

It writes `leakage.json` (every hit) and `leakage.md` (a plain summary) into the
output folder, and never overwrites either. The exit code says how it went: 0 means
everything was read and nothing was found; 1 means everything was read and there are
hits; 3 means something expected could not be read, so the report is incomplete and
names what; 2 means it could not start (for example, the run was not frozen at the
date given), and the message says why.

## The targets file

The targets file is written before grading and not changed after; the report records
its sha256 so the grade can show which file was used. An invented example:

```json
{
  "as_of": "2026-10-19",
  "figures": [
    {"id": "revenue", "value": "4.52", "scale": "billion", "forms": ["4,520"]},
    {"id": "margin", "value": "38.4", "unit": "percent"}
  ],
  "phrases": [{"id": "share_class", "text": "Class C common stock"}]
}
```

A figure matches a number written in the run's material when the two are equal at the
coarser of their precisions, and the written number has at least as many significant
digits as the registered one (`min_sig` lowers that bar for one figure). So with the
revenue above, "$4.52 billion", "4,524 million", "US$4.52bn" and "four point five two
billion" all match, and "$4.5 billion" and "14.52 billion" do not. A number written
with no scale is also reported when it equals the target read in thousands, millions
or billions (the way filings' tables are written), and says so. `forms` lists exact
strings to look for as well. A phrase matches regardless of case, emphasis marks,
quotation-mark style, hyphens and line breaks.

## What it reads

Everything is the run's own: the evidence store (every stored text, every receipt, and
the stored bytes of text formats; agents read the stored text, so other bytes are
listed as not read); the task folder; each launched session's saved stream; the
transcript of every session the driver launched and its subagents' transcripts, which
is where workers' web searches are recorded; tool output the harness saved to a
session's temporary folder; and the driver's own logs. Files agents wrote, such as
drafts and the deliverable, are not read on their own, because the transcripts hold
the tool call that wrote each.

## What it leaves out, and why

A real run happens after its cutoff, so its records are full of dates after it that
say nothing about the sources: the time stamp on each transcript line, a receipt's
fetch time, an HTTP response's own Date header, run ids and receipt ids made from the
clock, and token counts. These are skipped by name, never by guessing, and each kind
is counted in the report. The cutoff date itself is skipped where it is only an echo
(in the task, a prompt or a model's text) and reported where it came in from outside
(a capture or a tool result), because captures come from copies dated strictly before
the cutoff.

Dates are read in these forms: 2026-10-26 (also with slashes or dots, as in news URLs),
20261026 and Wayback's 14-digit time stamps, October 26, 2026, 26 October 2026,
10/26/2026 (when day and month could be either way round, the later reading is used,
and both are named), and November 2026 (which counts from its first day, so "October
2026" with a cutoff of 19 October is not reported). A bare year is never reported: the
web before any cutoff is full of forecasts for later years.

## What has been shown

Shown by tests on invented run folders (`tests/audit/test_leakage.py`, 27 tests): a
clean run with run-time stamps after the cutoff gives no hit and is complete; a date
after the cutoff planted in a capture is found in its stored text and its stored HTML,
with the line and the receipt's URL; a target figure planted in a worker's web search
result is found, named as a WebSearch result; the figure written in words inside a
tool's JSON result is found; a missing transcript or stream makes the report
incomplete; a different cutoff date refuses. Five deliberate breaks of the code (no
word numbers, no subagents, no skipping of run-time stamps, no reading of JSON inside
a tool result, "after" instead of "on or after") each made the tests fail.

Not shown: a run on a real run folder. None was audited here.
