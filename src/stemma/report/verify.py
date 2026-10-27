"""`stemma verify <run>`: re-check a recorded run offline, with no keys and no network.

Exact facts only (AGENTS.md: code judges nothing):
- **Seal.** A released run carries `release/manifest.json` (written by `finish_run`): every
  file's path, size and SHA-256, the register log's sealed length and SHA-256, and the
  patterns of operational files left out. Every file must match it; a file added,
  removed or changed, a link or a special file, fails. With `--expect <sha256>` the
  manifest itself must hash to a digest kept outside the run folder; without it, the
  check is against the run's own manifest and says so. A run without a manifest is
  UNSEALED and never PASSES: changes the checks below cannot see (a verdict in the log,
  a passage of the same length, a script swapped for one giving the same number, a file
  added or removed) would go unseen.
- **Captures.** Every stored source's bytes hash to the SHA-256 on its receipt, at the
  receipt's length.
- **Stored text.** Every receipt that has stored text still has it, at the recorded length.
- **Quotations.** Every quotation every claim rests on is still found in its source's
  stored text, by the same function the register used at filing (part A's
  `find_quote`, with the receipt's flagged ranges), and each figure as written is still
  inside one of its quotations.
- **Calculations.** Every estimated claim's recorded script is re-run in the register's
  sandbox (no network, writes only to a temporary folder) with its inputs' recorded
  numbers and its named documents, and must report exactly the claim's number.
- **The register's log.** It ends with a newline (no unterminated tail), folds from the
  first transaction to the last with no gap, duplicate or malformed line (World Model's
  own fold, in memory), and every claim's id still equals the hash of its content.
- **Fingerprints.** Every file the register recorded a SHA-256 for (submitted drafts,
  frames, final reviews, frozen branch records, the released deliverable) still has it.

Verify reads only. It opens nothing for writing, creates no lock, and works on a
read-only copy. A record in the wrong shape is a named failure, never a crash. A check
that could not run (no sandbox on this machine) makes the result INCOMPLETE.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from stemma.evidence.errors import EvidenceError
from stemma.evidence.quotes import find_quote
from stemma.register import manifest as reg_manifest
from stemma.register.derivation import DerivationError, build_bundle
from stemma.register.filing import rerun_filed
from stemma.report import ledger, runread, safefs
from stemma.report.errors import RunReadError
from stemma.report.runread import FINAL_REVIEW_FILES, RUN_ENTITY, Run

# Resource limit: re-runs of calculation scripts at once (each is one sandboxed process).
RERUN_WORKERS: Final = 8
# Resource limits: Python files, and their bytes together, one re-run copies (the same
# values as the register's re-run, applied to the files the script imports).
CODE_MAX_FILES: Final = 64
CODE_MAX_BYTES: Final = 4 * 1024 * 1024
# Resource limit: bytes of the release manifest read.
MANIFEST_MAX_BYTES: Final = 64 * 1024 * 1024
# Resource limit: bytes of one receipt read.
RECEIPT_MAX_BYTES: Final = 16 * 1024 * 1024
SOURCED_TIERS: Final = frozenset({"observed", "interested_party", "secondary"})
MANIFEST: Final = reg_manifest.MANIFEST
_SHA = re.compile(r"[0-9a-f]{64}")
# Exceptions a malformed record can raise inside a check; each becomes a named failure.
MALFORMED: Final = (KeyError, TypeError, ValueError, AttributeError, IndexError,
                    RecursionError, UnicodeError, OverflowError)


@dataclass
class Section:
    """One line of the result: what was re-checked, how many, and each failure named."""

    key: str
    label: str
    done: int = 0
    failures: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    extra: str = ""


@dataclass
class Seal:
    state: str = "unsealed"  # "unsealed" | "sealed" | "pinned"
    digest: str | None = None


@dataclass
class Result:
    run_id: str
    created_at: str | None
    sections: list[Section]
    seconds: float
    release_state: str
    seal: Seal = field(default_factory=Seal)

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(s.label, f) for s in self.sections for f in s.failures]

    @property
    def not_run(self) -> list[tuple[str, str]]:
        return [(s.label, f) for s in self.sections for f in s.not_run]

    @property
    def outcome(self) -> str:
        if self.failures:
            return "FAIL"
        if self.not_run:
            return "INCOMPLETE"
        return "UNSEALED" if self.seal.state == "unsealed" else "PASS"

    @property
    def exit_code(self) -> int:
        return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3, "UNSEALED": 4}[self.outcome]

    def as_json(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "outcome": self.outcome, "seconds": round(self.seconds, 1),
                "seal": self.seal.state, "manifest_sha256": self.seal.digest,
                "release_state": self.release_state,
                "sections": [{"key": s.key, "label": s.label, "checked": s.done,
                              "failed": len(s.failures), "not_run": len(s.not_run),
                              "failures": s.failures, "not_run_reasons": s.not_run,
                              "summary": summary_line(s)} for s in self.sections]}


def _n(v: int) -> str:
    return f"{v:,}"


def summary_line(s: Section) -> str:
    bad = len(s.failures)
    words = {
        "seal": s.extra,
        "records": s.extra or "the run's records could not be read",
        "captures": f"{_n(s.done)} captures re-hashed against their receipts, {bad} changed",
        "texts": f"{_n(s.done)} stored texts present at their recorded length, {bad} differ",
        "quotations": f"{_n(s.done)} quotations re-checked in the stored text, {bad} missing",
        "calculations": f"{_n(s.done)} calculations re-run in the sandbox, {bad} differ"
                        + (f"; {s.extra}" if s.extra else ""),
        "log": f"{s.extra}; {_n(s.done)} claim ids match their content, {bad} differ",
        "fingerprints": f"{_n(s.done)} recorded files re-hashed (drafts, frames, reviews, "
                        f"branch records), {bad} changed",
    }[s.key]
    if s.key == "quotations" and s.extra:
        words += f"; {s.extra}"
    if s.not_run:
        words += f"; {_n(len(s.not_run))} not run"
    return words


def guarded(key: str, label: str, fn: Callable[..., Section], *args: Any) -> Section:
    """Run one check; an unreadable or malformed record becomes a named failure."""
    try:
        return fn(*args)
    except RunReadError as exc:
        return Section(key, label, failures=[f"cannot be checked: {exc}"])
    except MALFORMED as exc:
        return Section(key, label, failures=[(f"a record is not in its expected shape "
                                             f"({type(exc).__name__}: {str(exc)[:200]})")])


# ----------------------------------------------------------------------- seal
def check_seal(run_dir: Path, log: ledger.Log | None, expect: str | None,
               seal: Seal) -> Section:
    """The run against its release manifest, by the register's own `manifest.check` (the
    code `finish_run` sealed it with); the manifest against a pinned digest when one is
    given. Lines the check records gained after the seal are reported, never read."""
    sec = Section("seal", "Seal")
    raw = safefs.maybe(safefs.read_bytes, run_dir, MANIFEST, MANIFEST_MAX_BYTES)
    if raw is None:
        sec.extra = ("UNSEALED: the run has no release manifest, so a file added, removed "
                     "or changed outside the checks below would not show")
        if expect:
            sec.failures.append(f"--expect was given, but {MANIFEST} does not exist.")
        return sec
    digest = hashlib.sha256(raw).hexdigest()
    seal.digest = digest
    if expect is not None and expect.lower() != digest:
        sec.failures.append(f"{MANIFEST} hashes to {digest}, not to the pinned "
                            f"{expect.lower()}.")
        sec.extra = "the manifest does not match the pinned digest"
        return sec
    seal.state = "pinned" if expect is not None else "sealed"
    sec.failures += [f"{line}." for line in reg_manifest.check(run_dir, expect)]
    try:
        files = json.loads(raw).get("files")
    except (ValueError, RecursionError, AttributeError):
        files = None
    sec.done = len(files) if isinstance(files, list) else 0
    later = reg_manifest.after_seal(run_dir)
    pin = "pinned outside the run" if seal.state == "pinned" else (
        "the run's own manifest, not pinned: pass --expect <sha256> with a digest kept "
        "outside the run folder")
    sec.extra = (f"{_n(sec.done)} sealed files, the log and the check records re-hashed "
                 f"against {pin}"
                 + (f"; {len(later)} check record(s) added after the seal, never read"
                    if later else ""))
    return sec


# ------------------------------------------------------------------- captures
def _where(rec: dict[str, Any]) -> str:
    url = rec.get("final_url") or rec.get("requested_url") or rec.get("path") or ""
    return f"receipt {rec.get('receipt_id')}" + (f" ({url})" if url else "")


def read_receipts(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every receipt, and a failure for each file in store/receipts that is not one."""
    out: list[dict[str, Any]] = []
    bad: list[str] = []
    for name in safefs.listdir(run_dir, "store/receipts"):
        rid = name.removesuffix(".json")
        if not name.endswith(".json") or not runread.RECEIPT_ID.fullmatch(rid):
            bad.append(f"store/receipts/{name} is not a receipt file.")
            continue
        try:
            rec = safefs.read_json(run_dir, f"store/receipts/{name}", RECEIPT_MAX_BYTES)
        except RunReadError as exc:
            bad.append(f"{exc}.")
            continue
        if not isinstance(rec, dict) or rec.get("receipt_id") != rid:
            bad.append(f"store/receipts/{name} does not hold receipt {rid}.")
            continue
        out.append(rec)
    return out, bad


def check_captures(run_dir: Path, receipts: list[dict[str, Any]],
                   bad: list[str]) -> Section:
    caps = Section("captures", "Captures", failures=list(bad))
    hashed: dict[str, tuple[str, int] | str] = {}
    for rec in receipts:
        sha = rec.get("sha256")
        if sha is None:
            continue  # a failed capture stores nothing; its receipt says so
        caps.done += 1
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            caps.failures.append(f"{_where(rec)}: its SHA-256 is not a SHA-256.")
            continue
        if sha not in hashed:
            try:
                hashed[sha] = safefs.sha256(run_dir, f"store/bytes/{sha}")
            except safefs.Missing:
                hashed[sha] = "the stored bytes are missing"
            except RunReadError as exc:
                hashed[sha] = str(exc)
        got = hashed[sha]
        if isinstance(got, str):
            caps.failures.append(f"{_where(rec)}: {got}.")
        elif got[0] != sha:
            caps.failures.append(f"{_where(rec)}: the stored bytes changed. The receipt says "
                                 f"SHA-256 {sha[:16]}…; the bytes now hash to {got[0][:16]}….")
        elif isinstance(rec.get("bytes_len"), int) and got[1] != rec["bytes_len"]:
            caps.failures.append(f"{_where(rec)}: the stored bytes are {_n(got[1])} long; the "
                                 f"receipt says {_n(rec['bytes_len'])}.")
    return caps


def check_texts(run_dir: Path, receipts: list[dict[str, Any]]) -> Section:
    texts = Section("texts", "Stored text")
    for rec in receipts:
        if not rec.get("text_id"):
            continue
        texts.done += 1
        try:
            text = runread.stored_text(run_dir, rec)
        except RunReadError as exc:
            texts.failures.append(f"{_where(rec)}: {exc}.")
            continue
        if text is None:
            texts.failures.append(f"{_where(rec)}: the stored text is missing.")
        elif isinstance(rec.get("text_chars"), int) and len(text) != rec["text_chars"]:
            texts.failures.append(f"{_where(rec)}: the stored text is {_n(len(text))} "
                                  f"characters; the receipt says {_n(rec['text_chars'])}.")
    return texts


# ----------------------------------------------------------------- quotations
def _cid(c: dict[str, Any]) -> str:
    cited = " (cited in the answer)" if c.get("_cited") else ""
    return f"claim c:{str(c['claim_id'])[:12]}{cited}"


@contextmanager
def _text_helpers_cached() -> Iterator[None]:
    """Part A's `find_quote` normalises the whole stored text on every call. That is a
    pure function of the text (its result is only read), so while quotations are
    re-checked it is computed once per text, and the original is restored after."""
    from stemma.evidence import quotes

    saved = quotes.normalise
    quotes.normalise = functools.lru_cache(maxsize=64)(saved)
    try:
        yield
    finally:
        quotes.normalise = saved


def check_quotations(run: Run, receipts: dict[str, dict[str, Any]]) -> Section:
    with _text_helpers_cached():
        return _check_quotations(run, receipts)


def _check_quotations(run: Run, receipts: dict[str, dict[str, Any]]) -> Section:
    sec = Section("quotations", "Quotations")
    sourced = [c for c in run.claims.values() if c.get("tier") in SOURCED_TIERS]
    # One source at a time, so each stored text is read and normalised once.
    items = sorted(((str(item.get("receipt_id")), str(c["claim_id"]), n, item, c)
                    for c in sourced for n, item in enumerate(c.get("support") or [], start=1)),
                   key=lambda t: (t[0], t[1], t[2]))
    current: tuple[str, str | None, str | None] = ("", None, None)  # rid, text, problem
    for rid, _cid_full, n, item, c in items:
        sec.done += 1
        rec = receipts.get(rid)
        if rec is None:
            sec.failures.append(f"{_cid(c)}, support {n}: receipt {rid} is missing.")
            continue
        if current[0] != rid:
            try:
                current = (rid, runread.stored_text(run.dir, rec), None)
                runread.flagged_ranges(rec)
            except RunReadError as exc:
                current = (rid, None, str(exc))
        text, problem = current[1], current[2]
        if problem or text is None:
            sec.failures.append(f"{_cid(c)}, support {n}: {problem or _where(rec) + ' has no stored text'}.")
            continue
        try:
            found = find_quote(text, str(item.get("quote")), runread.flagged_ranges(rec))
        except EvidenceError as exc:
            sec.failures.append(f"{_cid(c)}, support {n}: {exc}.")
            continue
        if not found["found"]:
            sec.failures.append(f"{_cid(c)}, support {n}: the quotation is no longer in "
                                f"the stored text of {_where(rec)}.")
    figures = figures_bad = 0
    for c in sourced:
        value = c.get("value") or {}
        written = value.get("as_written")
        if isinstance(written, str) and written.strip() and c.get("support"):
            figures += 1
            inside = False
            for item in c["support"]:
                try:
                    inside = inside or find_quote(str(item.get("quote")), written)["found"]
                except EvidenceError:
                    pass
            if not inside:
                figures_bad += 1
                sec.failures.append(f"{_cid(c)}: the figure as written, {written!r}, is not "
                                    f"inside any of its quotations.")
    sec.extra = f"{_n(figures)} figures inside their quotation, {figures_bad} missing"
    return sec


# --------------------------------------------------------------- calculations
Rerunner = Callable[..., Any]


def bundle_for(run_dir: Path, script: str) -> Any:
    """The re-run bundle of an `analysis/` script, by the register's own `build_bundle`
    (the code `file_claim` ran), the path checked first."""
    try:
        safefs.parts(script)
    except RunReadError as exc:
        raise DerivationError(f"script {script!r} is not a .py file in analysis/") from exc
    return build_bundle(run_dir, script)


def experiment_key(code: Any, values: dict[str, float], documents: dict[str, str]) -> str:
    """The SHA-256 of everything a re-run reads: its code files, input values and
    documents. Two re-runs with the same key are the same experiment."""
    h = hashlib.sha256()
    for name in sorted(code.files):
        h.update(name.encode() + b"\0" + hashlib.sha256(code.files[name]).digest())
    h.update(json.dumps([code.script, sorted(values.items()),
                         sorted((k, hashlib.sha256(v.encode()).hexdigest())
                                for k, v in documents.items())]).encode())
    return h.hexdigest()


Memo = dict[str, Any]  # experiment key -> the re-run's result


def memoized(fn: Rerunner, memo: Memo | None) -> Rerunner:
    """`fn`, reusing a result when the code, inputs and documents are byte-identical to a
    re-run already done in this process (the tamper demo's second pass)."""
    if memo is None:
        return fn

    def run(code: Any, values: dict[str, float], documents: dict[str, str] | None = None
            ) -> Any:
        key = experiment_key(code, values, documents or {})
        if key not in memo:
            memo[key] = fn(code, values, documents) if documents else fn(code, values)
        return memo[key]

    return run


class _Evidence:
    """The register's Evidence port over stored texts read confined and bounded:
    `read_text` for a re-run's documents, `check_quote` with part A's `find_quote`.
    QuoteCheckFailed when a receipt has no stored text."""

    def __init__(self, run: Run) -> None:
        self.run = run

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        from stemma.register.evidence_port import QuoteCheckFailed

        rec = runread.receipt(self.run.dir, receipt_id)
        text = runread.stored_text(self.run.dir, rec) if rec else None
        if text is None:
            raise QuoteCheckFailed(f"receipt {receipt_id} has no stored text")
        return text

    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        from stemma.register.evidence_port import QuoteCheckFailed

        text = self.read_text(run_dir, receipt_id)
        rec = runread.receipt(self.run.dir, receipt_id) or {}
        try:
            return bool(find_quote(text, quote, runread.flagged_ranges(rec))["found"])
        except (EvidenceError, RunReadError) as exc:
            raise QuoteCheckFailed(str(exc)) from exc


def _rerun_one(run: Run, c: dict[str, Any], rerun_fn: Rerunner,
               memo: Memo | None = None) -> tuple[str | None, str | None, str | None]:
    """(failure, not-run reason, note) for one estimated claim; failure and not-run are
    None when it reproduced. The note says the working copy in analysis/ no longer
    matches the bundle filed with the claim (the re-run used the filed copy)."""
    try:
        return _rerun_checked(run, c, memoized(rerun_fn, memo))
    except RunReadError as exc:
        return f"{_cid(c)}: {exc}.", None, None
    except MALFORMED as exc:
        return (f"{_cid(c)}: a record is not in its expected shape ({type(exc).__name__}).",
                None, None)


def _lookup(run: Run) -> Callable[[str], tuple[str, float]]:
    from stemma.register.filing import Refused

    def lookup(given: str) -> tuple[str, float]:
        inp = run.claim(str(given))
        number = (inp or {}).get("value", {}) or {}
        num = number.get("number") if isinstance(number, dict) else None
        if inp is None or not isinstance(num, int | float) or isinstance(num, bool):
            raise Refused(f"Refused: input {given} is not a claim with a number.")
        try:
            return str(inp["claim_id"]), float(num)
        except OverflowError as exc:
            raise Refused(f"Refused: input {given}'s number is too large for a float.") \
                from exc

    return lookup


def _rerun_checked(run: Run, c: dict[str, Any], fn: Rerunner
                   ) -> tuple[str | None, str | None, str | None]:
    from stemma.register.filing import Refused

    deriv = c.get("derivation") or {}
    value = c.get("value") or {}
    script = deriv.get("script")
    if not isinstance(script, str):
        return f"{_cid(c)}: an estimated claim with no recorded script.", None, None
    filed = deriv.get("bundle_sha256")
    note = None
    try:
        if isinstance(filed, str):
            # v0.4: the bundle filed with the claim, from the store, as file_claim ran it
            rr = rerun_filed(run.dir, c, _lookup(run), _Evidence(run), fn)
            try:
                if bundle_for(run.dir, script).sha256 != filed:
                    note = f"{_cid(c)}: {script} in analysis/ differs from the filed copy"
            except DerivationError:
                note = f"{_cid(c)}: {script} is no longer in analysis/"
        else:
            # A claim filed before bundles were stored: the script as it is now.
            code = bundle_for(run.dir, script)
            lookup = _lookup(run)
            values = dict(lookup(str(g)) for g in deriv.get("inputs") or [])
            documents: dict[str, str] = {}
            for rid in deriv.get("documents") or []:
                rec = runread.receipt(run.dir, rid)
                text = runread.stored_text(run.dir, rec) if rec else None
                if text is None:
                    return ((f"{_cid(c)}: document {rid} named by {script} has no stored "
                            f"text."), None, None)
                documents[str(rid)] = text
            rr = fn(code, values, documents) if documents else fn(code, values)
    except Refused as exc:
        return f"{_cid(c)}: {str(exc).removeprefix('Refused: ')}", None, None
    except DerivationError as exc:
        if "sandbox-exec is not available" in str(exc):
            return None, f"{_cid(c)}: {exc}", None
        return f"{_cid(c)}: {script} could not be re-run: {exc}.", None, None
    if rr.number is None:
        last = [ln.strip() for ln in str(rr.stderr_tail).splitlines() if ln.strip()]
        why = f" Its last error line: {last[-1][:200]}" if last else ""
        return ((f"{_cid(c)}: {script} reported no result on re-run (exit {rr.exit_code})."
                f"{why}"), None, note)
    want = value.get("number")
    if not isinstance(want, int | float) or rr.number != float(want):
        return ((f"{_cid(c)}: {script} now reports {rr.number!r}; the claim says "
                f"{value.get('number')!r}."), None, note)
    return None, None, note


def check_calculations(run: Run, rerun_fn: Rerunner | None = None,
                       workers: int = RERUN_WORKERS, memo: Memo | None = None) -> Section:
    from stemma.register.derivation import rerun

    fn = rerun_fn or rerun
    sec = Section("calculations", "Calculations")
    todo = [c for c in run.claims.values() if c.get("tier") == "estimated"]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda c: _rerun_one(run, c, fn, memo), todo))
    drifted = 0
    for failure, not_run, note in results:
        drifted += note is not None
        if not_run:
            sec.not_run.append(not_run)
            continue
        sec.done += 1
        if failure:
            sec.failures.append(failure)
    if drifted:
        sec.extra = (f"{drifted} re-run from the copy filed with the claim, whose working "
                     f"copy in analysis/ has changed since")
    return sec


# ------------------------------------------------------------------ the log
def check_log(log: ledger.Log) -> Section:
    from stemma.register.filing import claim_id_of

    sec = Section("log", "Register log")
    wm = ledger._memory_world(log.lines)
    sec.extra = f"{_n(len(log.lines))} transactions fold with no gap, malformed line or tail"
    for ent in wm.view("graph").query({"type": "claim"}):
        e: dict[str, Any] = dict(ent)
        sec.done += 1
        stored = str((e.get("externalKey") or {}).get("id"))
        body = (e.get("attrs") or {}).get("claim")
        if not isinstance(body, dict) or claim_id_of(body) != stored:
            st = body.get("statement", "") if isinstance(body, dict) else ""
            sec.failures.append(f"claim c:{stored[:12]} ({str(st)[:70]!r}): its content no "
                                f"longer hashes to its id, so it was changed after filing.")
    return sec


# -------------------------------------------------------------- fingerprints
def _fingerprinted(run: Run) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(relative path, recorded SHA-256, what it is) for every file the register
    fingerprinted, and a failure for each fingerprint record in the wrong shape."""
    f = run.run_facts
    out: list[tuple[str, str, str]] = []
    bad: list[str] = []

    def add(rel: str, sha: object, what: str) -> None:
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            bad.append(f"{what}: the recorded SHA-256 is not a SHA-256.")
        else:
            out.append((rel, sha, what))

    for d in f.get("draft_submitted", []):
        v = d.get("version")
        if isinstance(v, int) and not isinstance(v, bool):
            add(f"register/drafts/answer.v{v}.md", d.get("sha256"), f"submitted draft v{v}")
        else:
            bad.append("a draft_submitted record has no version.")
    for fr in f.get("frame_recorded", []):
        kind, rnd = fr.get("kind"), fr.get("round")
        if kind in ("own", "critique") and isinstance(rnd, int) and not isinstance(rnd, bool):
            add(f"register/frames/{kind}.r{rnd}.md", fr.get("sha256"),
                f"blind framer's {kind} frame, round {rnd}")
        else:
            bad.append("a frame_recorded record has no kind or round.")
    for rv in f.get("review_recorded", []):
        name = FINAL_REVIEW_FILES.get(str(rv.get("role")))
        v = rv.get("version")
        if name and isinstance(v, int) and not isinstance(v, bool):
            add(f"reviews/v{v}/{name}", rv.get("sha256"),
                f"{name.removesuffix('.md')} review of v{v}")
        else:
            bad.append("a review_recorded record has no known role or version.")
    for rf in f.get("record_frozen", []):
        add(str(rf.get("snapshot")), rf.get("sha256"),
            f"branch {rf.get('branch_id')} record, round {rf.get('round')}")
    finished = f.get("finished", [])
    if finished:
        add("deliverable/answer.md", finished[-1].get("sha256"), "the deliverable")
    return out, bad


def check_fingerprints(run: Run) -> Section:
    sec = Section("fingerprints", "Fingerprints")
    items, sec.failures = _fingerprinted(run)
    for rel, want, what in items:
        sec.done += 1
        try:
            got, _size = safefs.sha256(run.dir, rel)
        except safefs.Missing:
            sec.failures.append(f"{what} ({rel}) is missing.")
            continue
        except RunReadError as exc:
            sec.failures.append(f"{what}: {exc}.")
            continue
        if got != want:
            sec.failures.append(f"{what} ({rel}) changed after the register fingerprinted it.")
    return sec


# ------------------------------------------------------------------- driver
def verify(run_dir: Path, rerun_fn: Rerunner | None = None,
           workers: int = RERUN_WORKERS, memo: Memo | None = None,
           expect: str | None = None) -> Result:
    """Every check above. Raises RunReadError only when `run_dir` is not a run folder
    (no run.json); anything else unreadable or malformed is a named failure."""
    started = time.monotonic()
    run_dir = Path(run_dir)
    meta = runread.read_json(run_dir, "run.json")
    if not isinstance(meta, dict):
        raise RunReadError(f"{run_dir} has no run.json; is it a run folder?")
    run_id = str(meta.get("run_id") or run_dir.name)
    created = meta.get("created_at") if isinstance(meta.get("created_at"), str) else None
    seal = Seal()
    log: ledger.Log | None = None
    try:
        log = ledger.read_log(run_dir)
    except RunReadError as exc:
        log_problem = str(exc)
    sections = [guarded("seal", "Seal", check_seal, run_dir, log, expect, seal)]
    if log is None:
        sections.append(Section("log", "Register log", failures=[log_problem],
                                extra="the log cannot be read"))
        return Result(run_id, created, sections, time.monotonic() - started,
                      "unknown (the register cannot be read)", seal)
    try:
        run = runread.load(run_dir)
    except (RunReadError, *MALFORMED) as exc:
        sections.append(guarded("log", "Register log", check_log, log))
        sections.append(Section("records", "Run records",
                                failures=[f"the run's records cannot be read: {exc}"]))
        return Result(run_id, created, sections, time.monotonic() - started,
                      "unknown (the run's records cannot be read)", seal)
    for note in run.snapshot.notes:  # what the seal does not cover: never a pass
        sections[0].not_run.append(note)
    for cid in run.cited():
        run.claims[cid]["_cited"] = True  # only for naming failures
    receipts, bad = read_receipts(run.dir)
    sections += [guarded("captures", "Captures", check_captures, run.dir, receipts, bad),
                 guarded("texts", "Stored text", check_texts, run.dir, receipts),
                 guarded("quotations", "Quotations", check_quotations, run,
                         {str(r.get("receipt_id")): r for r in receipts}),
                 guarded("calculations", "Calculations", check_calculations, run, rerun_fn,
                         workers, memo),
                 guarded("log", "Register log", check_log, log),
                 guarded("fingerprints", "Fingerprints", check_fingerprints, run)]
    return Result(run_id, created, sections, time.monotonic() - started,
                  run.release.state.replace("_", " "), seal)


NOT_CHECKED = ("Not re-checked here, because no code can: whether each figure is right "
               "for its sentence, whether a source is credible, and the reviewers' "
               "judgments. Those were the run's checking agents' work, shown in the report.")


def render(result: Result, max_listed: int = 20) -> str:
    created = (result.created_at or "")[:10]
    out = [(f"Verifying run {result.run_id} (RECORDED {created}) offline: no network, "
           f"no keys."), ""]
    width = max(len(s.label) for s in result.sections)
    for s in result.sections:
        mark = ("FAIL" if s.failures else "----" if s.not_run and not s.done
                else "WARN" if s.key == "seal" and result.seal.state == "unsealed" else " ok ")
        out.append(f"  [{mark}] {s.label.ljust(width)}  {summary_line(s)}")
    if result.seal.digest:
        out.append(f"  Manifest SHA-256: {result.seal.digest}")
    out.append("")
    fails = result.failures
    if fails:
        out.append(f"Result: FAIL. {len(fails)} recorded fact{'s' if len(fails) != 1 else ''} "
                   f"did not reproduce:")
        for i, (label, msg) in enumerate(fails[:max_listed], start=1):
            out.append(f"  {i}. {label}: {msg}")
        if len(fails) > max_listed:
            out.append(f"  … and {len(fails) - max_listed} more (--json lists every one).")
        calc = [f for label, f in fails if label == "Calculations"]
        if calc:
            out.append("")
            out.append(f"The {len(calc)} calculation(s) that differ were re-run with the inputs "
                       f"the register used at filing, so a different number, or none, means "
                       f"the script or a module it imports is no longer what ran at filing: "
                       f"it was changed after the claim was filed.")
        out.append("")
        out.append("Release: REFUSED. The run's answer rests on these records, and they no "
                   "longer match what was checked.")
    elif result.not_run:
        reasons = sorted({msg.split(": ", 1)[-1] for _l, msg in result.not_run})
        out.append(f"Result: INCOMPLETE. {len(result.not_run)} check(s) could not run here: "
                   + "; ".join(reasons))
    elif result.seal.state == "unsealed":
        out.append(f"Result: UNSEALED. Every check above reproduced ({result.seconds:.0f} s), "
                   f"but the run has no release manifest, so this is not a pass. Release "
                   f"state: {result.release_state}.")
    else:
        pinned = ("against the pinned manifest" if result.seal.state == "pinned"
                  else "against the run's own manifest (not pinned)")
        out.append(f"Result: PASS, {pinned}. Every recorded fact above reproduced "
                   f"({result.seconds:.0f} s). Release state: {result.release_state}.")
    out.append("")
    out.append(NOT_CHECKED)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stemma verify", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--expect", metavar="SHA256",
                    help="the release manifest's SHA-256, kept outside the run folder")
    args = ap.parse_args(argv)
    if args.expect is not None and not _SHA.fullmatch(args.expect.lower()):
        print("stemma verify: --expect takes a SHA-256 (64 hex characters)", file=sys.stderr)
        return 2
    try:
        result = verify(args.run_dir, expect=args.expect)
    except RunReadError as exc:
        print(f"stemma verify: cannot read the run: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_json(), indent=1) if args.json else render(result))
    return result.exit_code


__all__ = ["MANIFEST", "RUN_ENTITY", "Result", "Section", "bundle_for", "check_seal", "main",
           "render", "verify"]


if __name__ == "__main__":
    sys.exit(main())
