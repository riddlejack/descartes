"""The leakage audit of a reality-graded run: did anything the run took in show the answer?

A reality-graded run answers from a web frozen at `as_of` and is graded against what
became public later. This command reads, offline and with no model call, everything
the run itself took in or said, and reports every place that shows

- a date on or after `as_of` (captures come from copies dated strictly before it, so a
  date on `as_of` itself is already past the frozen web), or
- a target figure or phrase from a pre-registered file (values the owner fixed before
  grading, as ones that appear only in the graded filing or in reporting after
  `as_of`).

Each hit names its file, line, JSON path, offsets, the text as written, what it was
read as, a snippet, and where it came from: content captured from outside, a tool's
result, a prompt, or text a model wrote. The audit judges nothing. Whether a hit means
the run saw the answer is the grader's call; the report gives the facts to make it.

What it reads, all of it the run's own:
- the evidence store: every stored text, every receipt, and the stored bytes of text
  formats (bytes of other formats are listed as not read: agents read their stored
  text, which is read);
- the task folder (the question and any attachments);
- each launched session's saved stream (`.stemma/sessions/<name>.stream.jsonl`);
- the transcript of every session `events.jsonl` says the driver launched, under the
  harness folder `run.json` names, and its subagents' transcripts (workers' web
  searches are recorded there);
- tool output the harness saved to a session's temporary folder (`.stemma-tmp/`);
- the driver's own logs: `events.jsonl`, the other `.jsonl` files under `.stemma/`,
  and each session's error output.
Files agents wrote (drafts, records, the deliverable) are not read on their own: each
was written by a tool call the transcripts hold.

Values stamped at run time are not source content and are skipped, each counted in the
report: fields named in `RUN_TIME_KEYS`, an HTTP response's own Date and Expires
headers, a value that is exactly the `as_of` date (an echo of the cutoff), and
identifiers the harness makes from the clock (run ids, receipt ids).

Anything expected and not readable (a launched session with no stream or no
transcript, a file that cannot be read) makes the report incomplete and says what.
Exit codes: 0 complete, no hits; 1 complete, with hits; 3 incomplete; 2 the audit could
not start (bad input, said in the message).
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from stemma.driver.settings import SESSION_TMP
from stemma.driver.verify import find_transcript

EXIT_CLEAN: Final = 0
EXIT_HITS: Final = 1
EXIT_BAD_INPUT: Final = 2
EXIT_INCOMPLETE: Final = 3

SNIPPET_CHARS: Final = 80  # resource limit: context kept on each side of a hit

# Fields whose values the harness, the driver or the evidence service stamp at run time
# (invariant: the names those parts write; none holds content from a source).
RUN_TIME_KEYS: Final = frozenset({
    "timestamp", "created_at", "fetched_at", "original_fetched_at", "at",
    "started_at", "ended_at", "resetsAt", "resets_at"})
# Keys holding identifiers or signatures, never content (invariant: harness formats).
ID_KEYS: Final = frozenset({
    "uuid", "parentUuid", "leafUuid", "sessionId", "session_id", "requestId",
    "request_id", "tool_use_id", "signature", "promptId"})
# Keys holding the harness's own measurements of a session, never content (invariant:
# harness formats); token counts would otherwise read as figures.
MEASURE_KEYS: Final = frozenset({
    "usage", "modelUsage", "total_cost_usd", "duration_ms", "duration_api_ms",
    "num_turns", "durationMs", "ttftMs"})
# An HTTP response's own time, not the source's (invariant: RFC 9110 header names).
RUN_TIME_HEADERS: Final = frozenset({"date", "expires"})
# Identifiers made from the clock at run time (invariant: `new_run_id`,
# `new_receipt_id`); masked before reading dates and figures.
_RUN_TIME_IDS: Final = re.compile(
    r"\b\d{8}-\d{6}-[0-9a-f]{6}\b|\br_\d{8}T\d{6}_[0-9a-f]{12}\b")
# Stored bytes read as text (invariant: media types that are text).
_TEXT_TYPES: Final = ("text/", "application/json", "application/xml",
                      "application/xhtml+xml", "application/ld+json",
                      "application/rss+xml", "application/atom+xml")

SCALES: Final = {"one": Decimal(1), "thousand": Decimal(10) ** 3,
                 "million": Decimal(10) ** 6, "billion": Decimal(10) ** 9,
                 "trillion": Decimal(10) ** 12}


class AuditError(Exception):
    """The audit cannot start; the message says why."""


# Targets ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Figure:
    id: str
    value: Decimal      # the total, scale applied
    quantum: Decimal    # the last registered digit's place, scale applied
    sig: int            # significant digits registered
    min_sig: int        # a number written with fewer digits is not compared
    unit: str           # "plain" | "percent"
    forms: tuple[str, ...]


@dataclass(frozen=True)
class Phrase:
    id: str
    text: str


@dataclass(frozen=True)
class Targets:
    as_of: date
    figures: tuple[Figure, ...]
    phrases: tuple[Phrase, ...]
    sha256: str
    path: str


def _digits(intpart: str, dec: str) -> tuple[Decimal, int]:
    """(quantum, significant digits) of a number as written. Trailing zeros of a whole
    number are not counted ("4,520" has three); after a decimal point they are."""
    if dec:
        return Decimal(1).scaleb(-len(dec)), len((intpart + dec).lstrip("0"))
    whole = intpart.lstrip("0")
    bare = whole.rstrip("0")
    return Decimal(10) ** (len(whole) - len(bare)), len(bare)


def load_targets(path: Path) -> Targets:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise AuditError(f"targets file {path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"as_of", "figures", "phrases", "note"}:
        raise AuditError("targets file must be an object with as_of, figures, phrases "
                         "(and an optional note)")
    as_of = _date_arg(data.get("as_of"), "targets as_of")
    seen: set[str] = set()

    def ident(entry: dict[str, Any], where: str) -> str:
        tid = entry.get("id")
        if not isinstance(tid, str) or not tid.strip() or tid in seen:
            raise AuditError(f"{where}: each target needs a unique non-empty id")
        seen.add(tid)
        return tid

    figures = []
    for i, f in enumerate(data.get("figures", [])):
        where = f"figures[{i}]"
        if not isinstance(f, dict) or set(f) - {"id", "value", "unit", "scale", "min_sig",
                                                 "forms", "note"}:
            raise AuditError(f"{where}: keys are id, value, unit, scale, min_sig, forms, note")
        tid = ident(f, where)
        m = re.fullmatch(r"-?(\d+)(?:\.(\d+))?", str(f.get("value", "")).replace(",", ""))
        if m is None:
            raise AuditError(f"{where}: value must be a decimal string such as \"4.52\"")
        scale = f.get("scale", "one")
        unit = f.get("unit", "plain")
        if scale not in SCALES or unit not in ("plain", "percent"):
            raise AuditError(f"{where}: scale is one of {sorted(SCALES)}; unit is plain "
                             "or percent")
        quantum, sig = _digits(m.group(1), m.group(2) or "")
        if sig == 0:
            raise AuditError(f"{where}: a target of zero cannot be told apart from text")
        min_sig = f.get("min_sig", sig)
        if not isinstance(min_sig, int) or isinstance(min_sig, bool) or min_sig < 1:
            raise AuditError(f"{where}: min_sig must be a positive whole number")
        forms = f.get("forms", [])
        if not isinstance(forms, list) or not all(isinstance(x, str) and x.strip()
                                                  for x in forms):
            raise AuditError(f"{where}: forms must be a list of non-empty strings")
        total = abs(Decimal(m.group(0))) * SCALES[scale]
        figures.append(Figure(tid, total, quantum * SCALES[scale], sig, min_sig, unit,
                              tuple(forms)))
    phrases = []
    for i, p in enumerate(data.get("phrases", [])):
        where = f"phrases[{i}]"
        if not isinstance(p, dict) or set(p) - {"id", "text", "note"}:
            raise AuditError(f"{where}: keys are id, text, note")
        tid = ident(p, where)
        text = p.get("text")
        if not isinstance(text, str) or not _normalise(text)[0].strip():
            raise AuditError(f"{where}: text must be a non-empty string")
        phrases.append(Phrase(tid, text))
    if not figures and not phrases:
        raise AuditError("targets file names no figure and no phrase")
    return Targets(as_of, tuple(figures), tuple(phrases),
                   hashlib.sha256(raw).hexdigest(), str(path))


def _date_arg(value: object, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise AuditError(f"{what} must be a date YYYY-MM-DD, got {value!r}") from None


# Reading text ----------------------------------------------------------------------

_DROP: Final = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff*_`")
_FOLD: Final = {"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
                "\u201c": '"', "\u201d": '"', "\u201e": '"', "-": " ", "\u2010": " ",
                "\u2011": " ", "\u2012": " ", "\u2013": " ", "\u2014": " ",
                "\u2015": " ", "\u2212": " "}


def _normalise(text: str) -> tuple[str, list[int]]:
    """Text for phrase matching, with each character's offset in the original: case
    folded, NFKC, quotation marks straightened, hyphens and dashes as spaces, runs of
    space as one, emphasis marks and invisible characters dropped."""
    out: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        if ch in _DROP:
            continue
        for c in unicodedata.normalize("NFKC", _FOLD.get(ch, ch)).casefold():
            if c.isspace():
                if out and out[-1] == " ":
                    continue
                c = " "
            out.append(c)
            where.append(i)
    return "".join(out), where


_SEPS: Final = ",\u2009\u202f\u00a0"
NUMBER: Final = re.compile(
    rf"(?<![\w.{_SEPS}])(?:[$\u00a3\u20ac\u00a5]\s?)?"
    rf"(?:(?P<int>\d{{1,3}}(?:[{_SEPS}]\d{{3}})+|\d+)(?:\.(?P<dec>\d+))?|\.(?P<dot>\d+))"
    r"(?:(?P<pct>\s?%|\s?per\s?cent\b)"
    r"|\s?(?P<suf>bn|mn|mm|MM|trn|tn|bil|k|K|m|M|b|B|T)(?![A-Za-z])"
    r"|[\s-](?P<word>thousand|million|billion|trillion)s?\b)?")
_SUFFIX: Final = {"k": "thousand", "m": "million", "mn": "million", "mm": "million",
                  "b": "billion", "bn": "billion", "bil": "billion", "t": "trillion",
                  "tn": "trillion", "trn": "trillion"}


@dataclass(frozen=True)
class Num:
    start: int
    end: int
    total: Decimal
    quantum: Decimal
    sig: int
    unit: str
    scaled: bool       # written with a scale word or suffix
    written: str


def numbers(text: str) -> list[Num]:
    """Numbers written in digits, with their scale and unit as written."""
    out = []
    for m in NUMBER.finditer(text):
        intpart = re.sub(rf"[{_SEPS}]", "", m.group("int") or "")
        dec = m.group("dec") or m.group("dot") or ""
        try:
            value = Decimal((intpart or "0") + ("." + dec if dec else ""))
        except InvalidOperation:
            continue
        quantum, sig = _digits(intpart, dec)
        word = m.group("word") or _SUFFIX.get((m.group("suf") or "").lower())
        scale = SCALES[word] if word else Decimal(1)
        out.append(Num(m.start(), m.end(), value * scale, quantum * scale, sig,
                       "percent" if m.group("pct") else "plain", word is not None,
                       m.group(0)))
    return out


_UNITS: Final = {w: i for i, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
     "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
     "seventeen", "eighteen", "nineteen"])}
_TENS: Final = {w: 10 * (i + 2) for i, w in enumerate(
    ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"])}
_BIG: Final = {"thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9,
               "trillion": 10 ** 12}
_WORD: Final = re.compile(r"[A-Za-z]+")
_GLUE: Final = re.compile(r"[\s-]+")


def word_numbers(text: str) -> list[Num]:
    """Numbers written in words: "four point five two billion", "two hundred and forty",
    "four and a half million", "a billion", "twelve percent"."""
    tokens = [(m.start(), m.end(), m.group(0).lower()) for m in _WORD.finditer(text)]
    out: list[Num] = []
    i = 0
    while i < len(tokens):
        got = _parse_words(text, tokens, i)
        if got is None:
            i += 1
            continue
        num, i = got
        out.append(num)
    return out


def _parse_words(text: str, tokens: list[tuple[int, int, str]],
                 i: int) -> tuple[Num, int] | None:
    first = tokens[i][2]
    if first not in _UNITS and first not in _TENS and first != "a":
        return None
    total = Decimal(0)
    group = Decimal(0)    # the part below the next scale word
    last = ""             # kind of the last token: unit, tens, hundred, big
    decimals = ""
    any_number = False
    start, end = tokens[i][0], tokens[i][1]
    j = i
    while j < len(tokens):
        s, e, w = tokens[j]
        if j > i and not _GLUE.fullmatch(text[tokens[j - 1][1]:s]):
            break
        nxt = tokens[j + 1][2] if j + 1 < len(tokens) else ""
        if w in _UNITS and last in ("", "tens", "hundred", "big") and not decimals:
            if last == "tens" and _UNITS[w] >= 10:
                break
            group += _UNITS[w]
            last = "unit"
        elif w in _TENS and last in ("", "hundred", "big") and not decimals:
            group += _TENS[w]
            last = "tens"
        elif w == "hundred" and last in ("unit", "tens", "a") and not decimals:
            group = (group or Decimal(1)) * 100
            last = "hundred"
        elif w in _BIG and last in ("unit", "tens", "hundred", "a", "point"):
            if decimals:
                group += Decimal("0." + decimals)
                decimals = ""
            total += (group or Decimal(1)) * _BIG[w]
            group = Decimal(0)
            last = "big"
        elif w == "a" and last == "" and (nxt == "hundred" or nxt in _BIG):
            group = Decimal(1)
            last = "a"
        elif w == "and" and last in ("hundred", "big", "unit", "tens") and (
                nxt in _UNITS or nxt in _TENS or nxt == "a"):
            if nxt == "a" and j + 2 < len(tokens) and tokens[j + 2][2] == "half" \
                    and last in ("unit", "tens"):
                group += Decimal("0.5")
                j += 3
                end = tokens[j - 1][1]
                last = "point"
                any_number = True
                continue
            if nxt == "a":
                break
            j += 1
            continue
        elif w == "point" and last in ("unit", "tens") and nxt in _UNITS \
                and _UNITS[nxt] < 10:
            k = j + 1
            while (k < len(tokens) and tokens[k][2] in _UNITS and _UNITS[tokens[k][2]] < 10
                   and _GLUE.fullmatch(text[tokens[k - 1][1]:tokens[k][0]])):
                decimals += str(_UNITS[tokens[k][2]])
                k += 1
            end = tokens[k - 1][1]
            j = k
            last = "point"
            any_number = True
            continue
        else:
            break
        any_number = any_number or w != "a"
        end = e
        j += 1
    if not any_number or last == "a":
        return None
    if decimals:
        group += Decimal("0." + decimals)
    value = total + group
    unit = "plain"
    pct = re.match(r"\s?(%|per\s?cent\b)", text[end:])
    if pct:
        unit = "percent"
        end += pct.end()
    norm = value.normalize()
    tup = norm.as_tuple()
    exp = tup.exponent if isinstance(tup.exponent, int) else 0
    sig = len(tup.digits) if value else 0
    return (Num(start, end, value, Decimal(1).scaleb(exp), sig, unit, last == "big",
                text[start:end]), j)


def _same(a: Decimal, qa: Decimal, b: Decimal, qb: Decimal) -> bool:
    """Equal at the coarser of the two precisions."""
    q = max(qa, qb)
    return (a / q).quantize(Decimal(1), ROUND_HALF_UP) == (b / q).quantize(
        Decimal(1), ROUND_HALF_UP)


def _figure_hits(text: str, fig: Figure, nums: list[Num]) -> Iterator[tuple[Num, str]]:
    for n in nums:
        if n.sig < fig.min_sig:
            continue
        if n.unit == fig.unit and _same(n.total, n.quantum, fig.value, fig.quantum):
            yield n, "as written"
        elif fig.unit == "percent" and n.unit == "plain" and not n.scaled and _same(
                n.total, n.quantum, fig.value, fig.quantum):
            yield n, "a percent target written without a percent sign"
        elif (fig.unit == "plain" and n.unit == "plain" and not n.scaled
              and not _looks_like_year(n)):
            for name in ("thousand", "million", "billion"):
                k = SCALES[name]
                if k <= fig.value and _same(n.total * k, n.quantum * k, fig.value,
                                            fig.quantum):
                    yield n, f"written without a scale; equal if in {name}s"
                    break


def _looks_like_year(n: Num) -> bool:
    return (n.quantum <= 1 and n.total == n.total.to_integral_value()
            and 1900 <= n.total <= 2100 and "," not in n.written)


# Dates -----------------------------------------------------------------------------

_MONTHS: Final = ("january", "february", "march", "april", "may", "june", "july",
                  "august", "september", "october", "november", "december")


def _month_pattern() -> str:
    names = []
    for full in _MONTHS:
        for form in {full, full[:3], "sept" if full == "september" else full}:
            names += [form.title(), form.upper()]
    alts = "|".join(sorted(set(names), key=len, reverse=True))
    return rf"(?P<mon>{alts})\.?(?![A-Za-z])"


_MON: Final = _month_pattern()
_DATE_FORMS: Final = (
    ("iso", re.compile(r"(?<![\d.])(?P<y>(?:19|20|21)\d\d)([-/.])(?P<m>\d{1,2})\2"
                       r"(?P<d>\d{1,2})(?!\d)")),
    ("compact", re.compile(r"(?<![A-Za-z0-9])(?P<y>(?:19|20|21)\d\d)(?P<m>\d\d)(?P<d>\d\d)"
                           r"(?:\d{6}|\d{4})?(?!\d)")),
    ("month day year", re.compile(_MON + r"\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+"
                                  r"(?P<y>\d{4})(?!\d)")),
    ("day month year", re.compile(r"(?<![\w.])(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
                                  + _MON + r",?\s+(?P<y>\d{4})(?!\d)")),
    ("numeric", re.compile(r"(?<![\w/.-])(?P<a>\d{1,2})([/-])(?P<b>\d{1,2})\2"
                           r"(?P<y>\d{4}|\d{2})(?![\w/-])"
                           r"|(?<![\w/.-])(?P<a2>\d{1,2})\.(?P<b2>\d{1,2})\.(?P<y2>\d{4})"
                           r"(?![\w.])")),
    ("month year", re.compile(_MON + r",?\s+(?P<y>\d{4})(?!\d)")),
)


def _month(name: str) -> int:
    low = name.lower().rstrip(".")
    return next(i + 1 for i, m in enumerate(_MONTHS) if m.startswith(low[:3]))


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def dates(text: str) -> Iterator[tuple[int, int, date, str]]:
    """(start, end, earliest day meant, how it was read) for each date written; a month
    and year means its first day. A numeric date that reads both ways round gives the
    later reading, and names both."""
    taken = bytearray(len(text))  # 1 where an earlier form already read a date
    for form, rx in _DATE_FORMS:
        for m in rx.finditer(text):
            if any(taken[m.start():m.end()]):
                continue
            g = m.groupdict()
            day: date | None
            how = form
            if form == "numeric":
                a, b, y = (g["a"], g["b"], g["y"]) if g["a"] else (g["a2"], g["b2"], g["y2"])
                year = int(y) + (2000 if len(y) == 2 else 0)
                readings = {r for r in (_mk(year, int(a), int(b)), _mk(year, int(b), int(a)))
                            if r is not None}
                if not readings:
                    continue
                day = max(readings)
                if len(readings) > 1:
                    how = ("numeric, day and month either way round: "
                           + " or ".join(sorted(r.isoformat() for r in readings)))
            elif form in ("iso", "compact"):
                day = _mk(int(g["y"]), int(g["m"]), int(g["d"]))
            elif form == "month year":
                day = _mk(int(g["y"]), _month(g["mon"]), 1)
            else:
                day = _mk(int(g["y"]), _month(g["mon"]), int(g["d"]))
            if day is None:
                continue
            taken[m.start():m.end()] = b"\x01" * (m.end() - m.start())
            yield m.start(), m.end(), day, how


# Hits ------------------------------------------------------------------------------

@dataclass
class Hit:
    kind: str            # "date" | "figure" | "phrase"
    target: str | None   # the target's id (figures and phrases)
    found: str           # the text as written
    reading: str         # what it was read as
    detail: str          # how it matched
    source: str          # which record: capture_text, receipt, transcript, ...
    origin: str          # captured, stored_bytes, task, tool_result, model, prompt, harness
    file: str
    line: int | None
    path: str            # JSON path inside the line or file; "" for plain text
    start: int
    end: int
    snippet: str
    session: str | None = None
    tool: str | None = None
    receipts: list[str] = field(default_factory=list)


@dataclass
class Where:
    source: str
    origin: str
    file: str
    line: int | None = None
    path: str = ""
    session: str | None = None
    tool: str | None = None
    receipts: list[str] = field(default_factory=list)


# Origins of content that came from outside the run (invariant: the origins below).
_CAME_IN: Final = frozenset({"captured", "stored_bytes", "tool_result"})


class Scanner:
    def __init__(self, targets: Targets, run_id: str) -> None:
        self.t = targets
        self.run_id = run_id
        self.hits: list[Hit] = []
        self.skipped: Counter[str] = Counter()
        self._phrases = [(p, _normalise(p.text)[0].strip()) for p in targets.phrases]
        self._forms = [(f, form, _normalise(form)[0].strip())
                       for f in targets.figures for form in f.forms]

    def _mask(self, text: str) -> str:
        def blank(m: re.Match[str]) -> str:
            self.skipped["run-time identifier"] += 1
            return " " * len(m.group(0))
        text = _RUN_TIME_IDS.sub(blank, text)
        if self.run_id and self.run_id in text:
            self.skipped["run-time identifier"] += text.count(self.run_id)
            text = text.replace(self.run_id, " " * len(self.run_id))
        return text

    def text(self, raw: str, where: Where) -> None:
        if raw.strip() == self.t.as_of.isoformat():
            self.skipped["value equal to as_of (an echo of the cutoff)"] += 1
            return
        text = self._mask(raw)

        def hit(kind: str, target: str | None, s: int, e: int, reading: str,
                detail: str) -> None:
            lo, hi = max(0, s - SNIPPET_CHARS), min(len(raw), e + SNIPPET_CHARS)
            self.hits.append(Hit(
                kind, target, raw[s:e], reading, detail, where.source, where.origin,
                where.file, where.line, where.path, s, e,
                " ".join(raw[lo:hi].split()), where.session, where.tool,
                list(where.receipts)))

        for s, e, day, how in dates(text):
            if day == self.t.as_of and where.origin not in _CAME_IN:
                self.skipped["the as_of date in a task, prompt or model text"] += 1
            elif day >= self.t.as_of:
                rel = "on as_of" if day == self.t.as_of else "after as_of"
                hit("date", None, s, e, day.isoformat(), f"{rel}; read as {how}")
        if self.t.figures:
            nums = numbers(text) + word_numbers(text)
            for fig in self.t.figures:
                for n, how in _figure_hits(text, fig, nums):
                    hit("figure", fig.id, n.start, n.end, f"{n.total} ({n.unit})", how)
        if self._phrases or self._forms:
            norm, idx = _normalise(text)
            for p, needle in self._phrases:
                for s, e in _find(norm, needle):
                    hit("phrase", p.id, idx[s], idx[e - 1] + 1, p.text, "phrase")
            for fig, form, needle in self._forms:
                for s, e in _find(norm, needle):
                    hit("figure", fig.id, idx[s], idx[e - 1] + 1, form,
                        "a registered form")

    def value(self, value: Any, where: Where, path: str = "") -> None:
        """Every string and number in a JSON value; JSON held in a string is opened."""
        if isinstance(value, dict):
            base64 = value.get("type") == "base64"
            for k, v in value.items():
                if k in RUN_TIME_KEYS:
                    self.skipped[f"run-time field {k}"] += 1
                elif k in ID_KEYS:
                    self.skipped[f"identifier field {k}"] += 1
                elif k in MEASURE_KEYS:
                    self.skipped[f"harness measurement {k}"] += 1
                elif base64 and k == "data":
                    self.skipped["base64 data (an image or file block)"] += 1
                else:
                    self.value(v, where, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            headers = path.endswith("headers")
            for i, v in enumerate(value):
                if (headers and isinstance(v, list) and len(v) == 2
                        and isinstance(v[0], str) and v[0].lower() in RUN_TIME_HEADERS):
                    self.skipped[f"HTTP {v[0].lower()} header"] += 1
                    continue
                self.value(v, where, f"{path}[{i}]")
        elif isinstance(value, str):
            nested = _as_json(value)
            if nested is not None:
                self.value(nested, where, f"{path}<json>")
            else:
                self.text(value, _at(where, path))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self.text(json.dumps(value), _at(where, path))


def _at(where: Where, path: str) -> Where:
    return Where(where.source, where.origin, where.file, where.line, path, where.session,
                 where.tool, where.receipts)


def _as_json(text: str) -> Any:
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        out = json.loads(s)
    except ValueError:
        return None
    return out if isinstance(out, (dict, list)) else None


def _find(hay: str, needle: str) -> Iterator[tuple[int, int]]:
    """Occurrences not inside a longer word or number."""
    if not needle:
        return
    i = hay.find(needle)
    while i >= 0:
        j = i + len(needle)
        before = hay[i - 1] if i else ""
        after = hay[j] if j < len(hay) else ""
        after2 = hay[j + 1] if j + 1 < len(hay) else ""
        ok = not (needle[0].isalnum() and before.isalnum()) and not (
            needle[-1].isalnum() and after.isalnum())
        if needle[-1].isdigit() and after in ".," and after2.isdigit():
            ok = False
        if needle[0].isdigit() and before in ".,":
            ok = False
        if ok:
            yield i, j
        i = hay.find(needle, i + 1)


# The run ---------------------------------------------------------------------------

@dataclass
class Report:
    run_dir: str
    run_id: str
    as_of: str
    targets_file: str
    targets_sha256: str
    complete: bool
    gaps: list[str]
    read: dict[str, int]
    not_read: list[str]
    skipped: dict[str, int]
    hits: list[Hit]


def _origin_of_line(obj: dict[str, Any]) -> str:
    kind = obj.get("type")
    if kind == "assistant" or kind == "result":
        return "model"
    if kind == "user":
        return "model" if obj.get("isCompactSummary") else "prompt"
    return "harness"


def _scan_jsonl(sc: Scanner, path: Path, rel: str, source: str, session: str | None,
                gaps: list[str]) -> None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        gaps.append(f"{rel}: not readable ({exc})")
        return
    tools: dict[str, str] = {}
    for n, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not raw.strip():
            continue
        where = Where(source, "harness", rel, n, session=session)
        try:
            obj = json.loads(raw)
        except ValueError:
            sc.skipped["lines that are not JSON (read as plain text)"] += 1
            sc.text(raw, where)
            continue
        if not isinstance(obj, dict):
            sc.value(obj, where)
            continue
        origin = _origin_of_line(obj)
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for k, v in obj.items():
            if k == "message" and isinstance(v, dict):
                for mk, mv in v.items():
                    if mk == "content":
                        continue
                    if mk in ID_KEYS or mk in RUN_TIME_KEYS or mk in MEASURE_KEYS or mk == "id":
                        sc.skipped[f"harness field message.{mk}"] += 1
                        continue
                    sc.value(mv, Where(source, origin, rel, n, session=session),
                             f"message.{mk}")
            elif k in ("toolUseResult", "tool_use_result"):
                sc.value(v, Where(source, "tool_result", rel, n, session=session,
                                  tool=_result_tool(content, tools)), k)
            elif k in RUN_TIME_KEYS:
                sc.skipped[f"run-time field {k}"] += 1
            elif k in ID_KEYS:
                sc.skipped[f"identifier field {k}"] += 1
            elif k in MEASURE_KEYS:
                sc.skipped[f"harness measurement {k}"] += 1
            else:
                sc.value(v, Where(source, origin, rel, n, session=session), k)
        if isinstance(content, str):
            sc.text(content, Where(source, origin, rel, n, "message.content", session))
        elif isinstance(content, list):
            for i, block in enumerate(content):
                p = f"message.content[{i}]"
                if not isinstance(block, dict):
                    sc.value(block, Where(source, origin, rel, n, session=session), p)
                    continue
                btype = block.get("type")
                if btype == "tool_use" and isinstance(block.get("id"), str):
                    tools[block["id"]] = str(block.get("name"))
                if btype == "tool_result":
                    tool = tools.get(str(block.get("tool_use_id")))
                    sc.value(block, Where(source, "tool_result", rel, n, session=session,
                                          tool=tool), p)
                else:
                    sc.value({k: v for k, v in block.items() if k != "id"},
                             Where(source, origin, rel, n, session=session,
                                   tool=str(block.get("name")) if btype == "tool_use"
                                   else None), p)


def _result_tool(content: Any, tools: dict[str, str]) -> str | None:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return tools.get(str(block.get("tool_use_id")))
    return None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def audit(run_dir: Path, as_of: date, targets: Targets,
          claude_config: Path | None = None) -> Report:
    if targets.as_of != as_of:
        raise AuditError(f"targets file as_of {targets.as_of} is not the audit's {as_of}")
    try:
        run = _read_json(run_dir / "run.json")
    except (OSError, ValueError) as exc:
        raise AuditError(f"{run_dir} has no readable run.json: {exc}") from exc
    if not isinstance(run, dict):
        raise AuditError("run.json is not an object")
    if str(run.get("as_of")) != as_of.isoformat():
        raise AuditError(f"run.json as_of is {run.get('as_of')!r}, not {as_of}: this run "
                         "was not frozen at the date given")
    run_id = str(run.get("run_id") or run_dir.name)
    harness = run.get("harness")
    if claude_config is None and isinstance(harness, dict) and harness.get("config_dir"):
        claude_config = Path(str(harness["config_dir"]))
    sc = Scanner(targets, run_id)
    gaps: list[str] = []
    read: Counter[str] = Counter()
    not_read: list[str] = []

    # The evidence store.
    store = run_dir / "store"
    by_sha: dict[str, list[dict[str, Any]]] = {}
    receipts = sorted((store / "receipts").glob("*.json")) if store.is_dir() else []
    for p in receipts:
        rel = str(p.relative_to(run_dir))
        try:
            rec = _read_json(p)
        except (OSError, ValueError) as exc:
            gaps.append(f"{rel}: not readable JSON ({exc})")
            continue
        if isinstance(rec, dict) and isinstance(rec.get("sha256"), str):
            by_sha.setdefault(rec["sha256"], []).append(rec)
        rid = rec.get("receipt_id") if isinstance(rec, dict) else None
        sc.value(rec, Where("receipt", "captured", rel, receipts=[str(rid)]))
        read["receipt"] += 1

    def labels(sha: str) -> list[str]:
        return [f"{r.get('receipt_id')} {r.get('final_url') or r.get('requested_url') or ''}"
                .strip() for r in by_sha.get(sha, [])]

    for p in sorted((store / "text").glob("*.txt")) if store.is_dir() else []:
        rel = str(p.relative_to(run_dir))
        try:
            body = p.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            gaps.append(f"{rel}: not readable as UTF-8 ({exc})")
            continue
        sha = p.stem
        _lines(sc, body, Where("capture_text", "captured", rel, receipts=labels(sha)))
        read["capture_text"] += 1
    for p in sorted((store / "bytes").iterdir()) if (store / "bytes").is_dir() else []:
        rel = str(p.relative_to(run_dir))
        recs = by_sha.get(p.name, [])
        ctype = next((str(r.get("content_type") or "") for r in recs
                      if r.get("content_type")), "")
        if not ctype.startswith(_TEXT_TYPES):
            not_read.append(f"{rel}: {ctype or 'unknown type'}, not text (its stored "
                            "text, if any, is read)")
            continue
        charset = next((str(r["charset"]) for r in recs if r.get("charset")), "utf-8")
        try:
            body = p.read_bytes().decode(charset, "replace")
        except (OSError, LookupError) as exc:
            gaps.append(f"{rel}: not readable ({exc})")
            continue
        _lines(sc, body, Where("capture_bytes", "stored_bytes", rel, receipts=labels(p.name)))
        read["capture_bytes"] += 1
    if not store.is_dir():
        not_read.append("store/: none (the run captured nothing)")
    elif (store / "index.sqlite").exists():
        not_read.append("store/index.sqlite: an index derived from the stored texts")

    # The task, and tool output the harness saved to a session's own temporary folder.
    for folder, source, origin in (("task", "task", "task"),
                                   (SESSION_TMP, "session_tmp", "tool_result")):
        for p in sorted((run_dir / folder).rglob("*")):
            if not p.is_file() or p.name == ".keep":
                continue
            rel = str(p.relative_to(run_dir))
            try:
                body = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                not_read.append(f"{rel}: not UTF-8 text")
                continue
            except OSError as exc:
                gaps.append(f"{rel}: not readable ({exc})")
                continue
            _lines(sc, body, Where(source, origin, rel))
            read[source] += 1

    # Sessions: streams and transcripts of every session the driver launched.
    launched: dict[str, str] = {}
    events = run_dir / "events.jsonl"
    if not events.is_file():
        gaps.append("events.jsonl: missing, so the run's sessions cannot be listed")
    else:
        for line in events.read_text("utf-8", "replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if (isinstance(e, dict) and e.get("event") == "session_launched"
                    and isinstance(e.get("session_id"), str)):
                launched.setdefault(e["session_id"], str(e.get("session")))
        if not launched:
            gaps.append("events.jsonl records no launched session")
    streams = run_dir / ".stemma" / "sessions"
    for name in sorted(set(launched.values())):
        p = streams / f"{name}.stream.jsonl"
        rel = str(p.relative_to(run_dir))
        if not p.is_file():
            gaps.append(f"{rel}: missing for launched session {name}")
            continue
        _scan_jsonl(sc, p, rel, "session_stream", name, gaps)
        read["session_stream"] += 1
    for sid, name in sorted(launched.items()):
        if claude_config is None or not claude_config.is_dir():
            gaps.append(f"transcript of {name} ({sid}): no harness folder "
                        f"({claude_config}) to find it in")
            continue
        t = find_transcript(claude_config, run_dir, None, sid)
        if t is None:
            gaps.append(f"transcript of {name} ({sid}): not found under {claude_config}")
            continue
        _scan_jsonl(sc, t, str(t), "transcript", name, gaps)
        read["transcript"] += 1
        for sub in sorted((t.with_suffix("") / "subagents").glob("agent-*.jsonl")):
            _scan_jsonl(sc, sub, str(sub), "subagent_transcript", name, gaps)
            read["subagent_transcript"] += 1

    # The driver's own logs.
    logs = [events] if events.is_file() else []
    logs += [p for p in sorted((run_dir / ".stemma").rglob("*.jsonl"))
             if not p.name.endswith(".stream.jsonl")] if (run_dir / ".stemma").is_dir() else []
    for p in logs:
        rel = str(p.relative_to(run_dir))
        _scan_jsonl(sc, p, rel, "driver_log", None, gaps)
        read["driver_log"] += 1
    for p in sorted(streams.glob("*.stderr")) if streams.is_dir() else []:
        rel = str(p.relative_to(run_dir))
        try:
            body = p.read_text("utf-8", "replace")
        except OSError as exc:
            gaps.append(f"{rel}: not readable ({exc})")
            continue
        _lines(sc, body, Where("session_stderr", "harness", rel))
        read["session_stderr"] += 1

    return Report(str(run_dir), run_id, as_of.isoformat(), targets.path, targets.sha256,
                  not gaps, gaps, dict(read), not_read, dict(sc.skipped), sc.hits)


def _lines(sc: Scanner, body: str, where: Where) -> None:
    """A whole text file (so a phrase or date may run across lines); each hit gets the
    line it starts on, and offsets in the file."""
    first = len(sc.hits)
    sc.text(body, where)
    breaks = [i for i, c in enumerate(body) if c == "\n"]
    for h in sc.hits[first:]:
        h.line = bisect.bisect_left(breaks, h.start) + 1


# Output ----------------------------------------------------------------------------

_INPUTS: Final = ("captured", "stored_bytes", "task", "tool_result", "prompt", "harness")


def summary(report: Report, targets: Targets) -> str:
    """The report in plain words, for the owner."""
    out = [f"# Leakage audit of run {report.run_id}", ""]
    hits = report.hits
    if report.complete:
        out.append("Every record the audit expected was read.")
    else:
        out.append("**This audit is incomplete.** These records could not be read, so "
                   "a clean result would prove nothing about them:")
        out += [f"- {g}" for g in report.gaps]
    out.append("")
    out.append(f"The web was frozen at {report.as_of}. The audit looked for dates on or "
               "after that day, and for the targets registered in "
               f"{report.targets_file} (sha256 {report.targets_sha256}).")
    out.append("")
    dates_in = [h for h in hits if h.kind == "date"]
    out.append("## Dates on or after the cutoff")
    out.append("")
    if not dates_in:
        out.append("None was found.")
    else:
        out.append(f"{len(dates_in)} places show one. Where they came from:")
        for origin, n in Counter(h.origin for h in dates_in).most_common():
            out.append(f"- {_say(origin)}: {n}")
    out.append("")
    out.append("## Targets")
    out.append("")
    ids = [f.id for f in targets.figures] + [p.id for p in targets.phrases]
    for tid in ids:
        mine = [h for h in hits if h.target == tid]
        if not mine:
            out.append(f"- {tid}: not found anywhere the audit read.")
            continue
        came_in = sum(h.origin in _INPUTS for h in mine)
        wrote = len(mine) - came_in
        out.append(f"- {tid}: found in {len(mine)} places; {came_in} in material the run "
                   f"took in, {wrote} in text a model wrote. First seen as "
                   f"\"{mine[0].found}\" in {mine[0].file}"
                   + (f", line {mine[0].line}" if mine[0].line else "") + ".")
    out.append("")
    out.append("Every hit, with its file, line, JSON path, offsets and snippet, is in the "
               "JSON report beside this file. The audit judges nothing: whether a hit "
               "means the run saw the answer is for the grader to say.")
    return "\n".join(out) + "\n"


def _say(origin: str) -> str:
    return {"captured": "captured pages and their receipts",
            "stored_bytes": "stored bytes of captures (agents read their text instead)",
            "task": "the task folder", "tool_result": "tool results",
            "prompt": "prompts and notices given to a session",
            "harness": "harness and driver records",
            "model": "text a model wrote"}.get(origin, origin)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -I -m stemma.audit.leakage",
                                 description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--targets", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path,
                    help="folder for leakage.json and leakage.md (never overwritten)")
    ap.add_argument("--claude-config", type=Path, default=None,
                    help="the harness folder holding the transcripts (default: run.json's)")
    args = ap.parse_args(argv)
    try:
        as_of = _date_arg(args.as_of, "--as-of")
        targets = load_targets(args.targets)
        report = audit(args.run_dir.resolve(), as_of, targets, args.claude_config)
        args.out.mkdir(parents=True, exist_ok=True)
        js, md = args.out / "leakage.json", args.out / "leakage.md"
        for p in (js, md):
            if p.exists():
                raise AuditError(f"{p} exists; the audit never overwrites a report")
        js.write_text(json.dumps(asdict(report), indent=1, ensure_ascii=False) + "\n",
                      "utf-8")
        md.write_text(summary(report, targets), "utf-8")
    except AuditError as exc:
        print(f"leakage audit: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(f"{len(report.hits)} hits; complete: {report.complete}; report: {js}")
    if not report.complete:
        return EXIT_INCOMPLETE
    return EXIT_HITS if report.hits else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
