"""`stemma verify <run>`: re-check a recorded run offline, with no keys and no network.

Exact facts only (AGENTS.md: code judges nothing):
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
- **The register's log.** It folds from the first transaction to the last with no gap,
  duplicate or malformed line (World Model's own fold), and every claim's id still
  equals the hash of its content, so a changed figure, statement or quotation in the log
  shows.
- **Fingerprints.** Every file the register recorded a SHA-256 for (submitted drafts,
  frames, final reviews, frozen branch records, the released deliverable) still has it.

A check that could not run (no sandbox on this machine) is reported as not run, and the
result is INCOMPLETE, never a pass.
"""

from __future__ import annotations

import argparse
import ast
import errno
import functools
import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stemma.evidence.errors import EvidenceError
from stemma.evidence.quotes import find_quote
from stemma.report import runread
from stemma.report.runread import FINAL_REVIEW_FILES, RUN_ENTITY, Run

# Resource limit: re-runs of calculation scripts at once (each is one sandboxed process).
RERUN_WORKERS = 8
# Resource limit: bytes read at a time while hashing a stored source.
HASH_CHUNK = 1 << 20
# Resource limits: Python files, and their bytes together, one re-run copies (the same
# values as the register's re-run, applied to the files the script imports).
CODE_MAX_FILES = 64
CODE_MAX_BYTES = 4 * 1024 * 1024
SOURCED_TIERS = frozenset({"observed", "interested_party", "secondary"})


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
class Result:
    run_id: str
    created_at: str | None
    sections: list[Section]
    seconds: float
    release_state: str

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
        return "INCOMPLETE" if self.not_run else "PASS"

    @property
    def exit_code(self) -> int:
        return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}[self.outcome]

    def as_json(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "outcome": self.outcome, "seconds": round(self.seconds, 1),
                "sections": [{"key": s.key, "label": s.label, "checked": s.done,
                              "failed": len(s.failures), "not_run": len(s.not_run),
                              "failures": s.failures, "not_run_reasons": s.not_run,
                              "summary": summary_line(s)} for s in self.sections]}


def _n(v: int) -> str:
    return f"{v:,}"


def summary_line(s: Section) -> str:
    bad = len(s.failures)
    words = {
        "captures": f"{_n(s.done)} captures re-hashed against their receipts, {bad} changed",
        "texts": f"{_n(s.done)} stored texts present at their recorded length, {bad} differ",
        "quotations": f"{_n(s.done)} quotations re-checked in the stored text, {bad} missing",
        "calculations": f"{_n(s.done)} calculations re-run in the sandbox, {bad} differ",
        "log": f"{s.extra}; {_n(s.done)} claim ids match their content, {bad} differ",
        "fingerprints": f"{_n(s.done)} recorded files re-hashed (drafts, frames, reviews, "
                        f"branch records), {bad} changed",
    }[s.key]
    if s.key == "quotations" and s.extra:
        words += f"; {s.extra}"
    if s.not_run:
        words += f"; {_n(len(s.not_run))} not run"
    return words


# ------------------------------------------------------------------- captures
def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _where(rec: dict[str, Any]) -> str:
    url = rec.get("final_url") or rec.get("requested_url") or rec.get("path") or ""
    return f"receipt {rec.get('receipt_id')}" + (f" ({url})" if url else "")


def check_captures(run_dir: Path, receipts: list[dict[str, Any]]) -> tuple[Section, Section]:
    caps = Section("captures", "Captures")
    texts = Section("texts", "Stored text")
    hashed: dict[str, tuple[str, int] | None] = {}
    for rec in receipts:
        sha = rec.get("sha256")
        if not isinstance(sha, str) or not sha:
            continue  # a failed capture stores nothing; its receipt says so
        caps.done += 1
        if sha not in hashed:
            path = run_dir / "store" / "bytes" / sha
            try:
                hashed[sha] = _sha256_file(path)
            except OSError:
                hashed[sha] = None
        got = hashed[sha]
        if got is None:
            caps.failures.append(f"{_where(rec)}: the stored bytes are missing.")
        elif got[0] != sha:
            caps.failures.append(f"{_where(rec)}: the stored bytes changed. The receipt says "
                                 f"SHA-256 {sha[:16]}…; the bytes now hash to {got[0][:16]}….")
        elif isinstance(rec.get("bytes_len"), int) and got[1] != rec["bytes_len"]:
            caps.failures.append(f"{_where(rec)}: the stored bytes are {_n(got[1])} long; the "
                                 f"receipt says {_n(rec['bytes_len'])}.")
        if rec.get("text_id"):
            texts.done += 1
            text = runread.stored_text(run_dir, rec)
            if text is None:
                texts.failures.append(f"{_where(rec)}: the stored text is missing.")
            elif isinstance(rec.get("text_chars"), int) and len(text) != rec["text_chars"]:
                texts.failures.append(f"{_where(rec)}: the stored text is {_n(len(text))} "
                                      f"characters; the receipt says {_n(rec['text_chars'])}.")
    return caps, texts


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
    current: tuple[str, str | None] = ("", None)
    for rid, _cid_full, n, item, c in items:
        sec.done += 1
        rec = receipts.get(rid)
        if rec is None:
            sec.failures.append(f"{_cid(c)}, support {n}: receipt {rid} is missing.")
            continue
        if current[0] != rid:
            current = (rid, runread.stored_text(run.dir, rec))
        text = current[1]
        if text is None:
            sec.failures.append(f"{_cid(c)}, support {n}: {_where(rec)} has no stored text.")
            continue
        try:
            found = find_quote(text, str(item.get("quote")), rec.get("flagged_ranges"))
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


def _plain_file(path: Path) -> bytes:
    """A regular file's bytes with one link, opened without following a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError("a link, not a plain file") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"{path.name} is not a plain file")
        if st.st_size > CODE_MAX_BYTES:
            raise OSError(f"{path.name} is larger than {CODE_MAX_BYTES} bytes")
        return os.read(fd, CODE_MAX_BYTES + 1)
    finally:
        os.close(fd)


def _imported_names(source: bytes) -> set[str]:
    """Top-level module names a script imports statically (read with `ast`, never run)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def code_bundle(run_dir: Path, script: str) -> Any:
    """The script and the sibling modules it imports, transitively, as the register's
    `Code`. The register's re-run copies every sibling `.py` file; a script runs the same
    with only the siblings it imports, and one that imports a sibling dynamically fails
    loudly on re-run (no result), never passes silently."""
    from stemma.register.derivation import Code, DerivationError

    rel = Path(script)
    if (rel.is_absolute() or len(rel.parts) < 2 or rel.parts[0] != "analysis"
            or any(p in ("", ".", "..") for p in rel.parts) or rel.suffix != ".py"):
        raise DerivationError(f"script {script} is not a .py file in analysis/")
    folder = run_dir / rel.parent
    if any(p.is_symlink() for p in [run_dir / "analysis", folder]):
        raise DerivationError(f"the folder of script {script} is not a plain folder")
    files: dict[str, bytes] = {}
    todo = [rel.name]
    while todo:
        name = todo.pop()
        if name in files:
            continue
        try:
            data = _plain_file(folder / name)
        except FileNotFoundError as exc:
            raise DerivationError(f"{rel.parent}/{name} does not exist") from exc
        except OSError as exc:
            raise DerivationError(f"{rel.parent}/{name}: {exc}") from exc
        files[name] = data
        if len(files) > CODE_MAX_FILES or sum(map(len, files.values())) > CODE_MAX_BYTES:
            raise DerivationError(f"{script} imports more than {CODE_MAX_FILES} files or "
                                  f"{CODE_MAX_BYTES} bytes of code (a resource limit)")
        try:
            imported = _imported_names(data)
        except (SyntaxError, ValueError) as exc:
            raise DerivationError(f"{rel.parent}/{name} is not valid Python") from exc
        todo.extend(f"{m}.py" for m in sorted(imported)
                    if (folder / f"{m}.py").is_file() and f"{m}.py" not in files)
    return Code(rel.name, files)


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


def _rerun_one(run: Run, c: dict[str, Any], rerun_fn: Rerunner,
               memo: Memo | None = None) -> tuple[str | None, str | None]:
    """(failure, not-run reason) for one estimated claim; both None when it reproduced.
    With a memo, a re-run whose code, inputs and documents are byte-identical to one
    already run in this process reuses that result (the tamper demo's second pass)."""
    from stemma.register.derivation import DerivationError

    deriv = c.get("derivation") or {}
    value = c.get("value") or {}
    script = deriv.get("script")
    if not isinstance(script, str):
        return f"{_cid(c)}: an estimated claim with no recorded script.", None
    try:
        code = code_bundle(run.dir, script)
    except DerivationError as exc:
        return f"{_cid(c)}: {script} cannot be re-run: {exc}.", None
    values: dict[str, float] = {}
    for given in deriv.get("inputs") or []:
        inp = run.claim(str(given))
        number = (inp or {}).get("value", {}) or {}
        num = number.get("number") if isinstance(number, dict) else None
        if inp is None or not isinstance(num, int | float) or isinstance(num, bool):
            return f"{_cid(c)}: input {given} is not a claim with a number.", None
        values[str(inp["claim_id"])] = float(num)
    documents: dict[str, str] = {}
    for rid in deriv.get("documents") or []:
        rec = runread.receipt(run.dir, str(rid))
        text = runread.stored_text(run.dir, rec) if rec else None
        if text is None:
            return f"{_cid(c)}: document {rid} named by {script} has no stored text.", None
        documents[str(rid)] = text
    key = experiment_key(code, values, documents)
    try:
        if memo is not None and key in memo:
            rr = memo[key]
        else:
            rr = rerun_fn(code, values, documents) if documents else rerun_fn(code, values)
            if memo is not None:
                memo[key] = rr
    except DerivationError as exc:
        if "sandbox-exec is not available" in str(exc):
            return None, f"{_cid(c)}: {exc}"
        return f"{_cid(c)}: {script} could not be re-run: {exc}.", None
    if rr.number is None:
        last = [ln.strip() for ln in str(rr.stderr_tail).splitlines() if ln.strip()]
        why = f" Its last error line: {last[-1][:200]}" if last else ""
        return ((f"{_cid(c)}: {script} reported no result on re-run (exit {rr.exit_code})."
                f"{why}"), None)
    want = value.get("number")
    if not isinstance(want, int | float) or rr.number != float(want):
        return ((f"{_cid(c)}: {script} now reports {rr.number!r}; the claim says "
                f"{value.get('number')!r}."), None)
    return None, None


def check_calculations(run: Run, rerun_fn: Rerunner | None = None,
                       workers: int = RERUN_WORKERS, memo: Memo | None = None) -> Section:
    from stemma.register.derivation import rerun

    fn = rerun_fn or rerun
    sec = Section("calculations", "Calculations")
    todo = [c for c in run.claims.values() if c.get("tier") == "estimated"]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda c: _rerun_one(run, c, fn, memo), todo))
    for failure, not_run in results:
        if not_run:
            sec.not_run.append(not_run)
            continue
        sec.done += 1
        if failure:
            sec.failures.append(failure)
    return sec


# ------------------------------------------------------------------ the log
def check_log(run_dir: Path) -> Section:
    from stemma.register.filing import claim_id_of
    from stemma.register.wm import WorldModel, WorldModelError

    sec = Section("log", "Register log")
    try:
        wm = WorldModel({"storage": "file", "path": str(run_dir / "register" / "log.jsonl"),
                         "namespace": "register"})
        txs = len(wm.audit())
    except WorldModelError as exc:
        sec.extra = "the log does not fold"
        sec.failures.append(f"register/log.jsonl does not fold: {exc}")
        return sec
    sec.extra = f"{_n(txs)} transactions fold with no gap or malformed line"
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
def _fingerprinted(run: Run) -> list[tuple[str, str, str]]:
    """(relative path, recorded SHA-256, what it is) for every file the register
    fingerprinted."""
    f = run.run_facts
    out: list[tuple[str, str, str]] = []
    for d in f.get("draft_submitted", []):
        out.append((f"register/drafts/answer.v{int(d['version'])}.md", str(d["sha256"]),
                    f"submitted draft v{d['version']}"))
    for fr in f.get("frame_recorded", []):
        out.append((f"register/frames/{fr['kind']}.r{int(fr['round'])}.md", str(fr["sha256"]),
                    f"blind framer's {fr['kind']} frame, round {fr['round']}"))
    for rv in f.get("review_recorded", []):
        name = FINAL_REVIEW_FILES.get(str(rv.get("role")))
        if name:
            out.append((f"reviews/v{int(rv['version'])}/{name}", str(rv["sha256"]),
                        f"{name.removesuffix('.md')} review of v{rv['version']}"))
    for rf in f.get("record_frozen", []):
        if isinstance(rf.get("snapshot"), str):
            out.append((str(rf["snapshot"]), str(rf["sha256"]),
                        f"branch {rf.get('branch_id')} record, round {rf.get('round')}"))
    finished = f.get("finished", [])
    if finished:
        out.append(("deliverable/answer.md", str(finished[-1]["sha256"]), "the deliverable"))
    return out


def check_fingerprints(run: Run) -> Section:
    sec = Section("fingerprints", "Fingerprints")
    for rel, want, what in _fingerprinted(run):
        sec.done += 1
        path = run.dir / rel
        try:
            got = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            sec.failures.append(f"{what} ({rel}) is missing.")
            continue
        if got != want:
            sec.failures.append(f"{what} ({rel}) changed after the register fingerprinted it.")
    return sec


# ------------------------------------------------------------------- driver
def _receipts(run_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((run_dir / "store" / "receipts").glob("*.json")):
        rec = runread.read_json(p)
        if isinstance(rec, dict):
            out.append(rec)
    return out


def release_state(run: Run) -> str:
    f = run.run_facts
    if not f.get("finished"):
        return "not released"
    return "withdrawn" if f.get("release_withdrawn") else "released"


def verify(run_dir: Path, rerun_fn: Rerunner | None = None,
           workers: int = RERUN_WORKERS, memo: Memo | None = None) -> Result:
    started = time.monotonic()
    run = runread.load(Path(run_dir))
    for cid in run.cited():
        run.claims[cid]["_cited"] = True  # only for naming failures
    receipts = _receipts(run.dir)
    caps, texts = check_captures(run.dir, receipts)
    sections = [caps, texts,
                check_quotations(run, {str(r.get("receipt_id")): r for r in receipts}),
                check_calculations(run, rerun_fn, workers, memo),
                check_log(run.dir),
                check_fingerprints(run)]
    return Result(run.run_id, run.run.get("created_at"), sections,
                  time.monotonic() - started, release_state(run))


NOT_CHECKED = ("Not re-checked here, because no code can: whether each figure is right "
               "for its sentence, whether a source is credible, and the reviewers' "
               "judgments. Those were the run's checking agents' work, shown in the report.")


def render(result: Result, max_listed: int = 20) -> str:
    created = (result.created_at or "")[:10]
    out = [(f"Verifying run {result.run_id} (RECORDED {created}) offline: no network, "
           f"no keys."), ""]
    width = max(len(s.label) for s in result.sections)
    for s in result.sections:
        mark = "FAIL" if s.failures else ("----" if s.not_run and not s.done else " ok ")
        out.append(f"  [{mark}] {s.label.ljust(width)}  {summary_line(s)}")
    out.append("")
    fails = result.failures
    if fails:
        out.append(f"Result: FAIL. {len(fails)} recorded fact{'s' if len(fails) != 1 else ''} "
                   f"did not reproduce:")
        for i, (label, msg) in enumerate(fails[:max_listed], start=1):
            out.append(f"  {i}. {label}: {msg}")
        if len(fails) > max_listed:
            out.append(f"  … and {len(fails) - max_listed} more (--json lists every one).")
        out.append("")
        out.append("Release: REFUSED. The run's answer rests on these records, and they no "
                   "longer match what was checked.")
    elif result.not_run:
        reasons = sorted({msg.split(": ", 1)[-1] for _l, msg in result.not_run})
        out.append(f"Result: INCOMPLETE. {len(result.not_run)} check(s) could not run here: "
                   + "; ".join(reasons))
    else:
        out.append(f"Result: PASS. Every recorded fact above reproduced "
                   f"({result.seconds:.0f} s). Release state: {result.release_state}.")
    out.append("")
    out.append(NOT_CHECKED)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stemma verify", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)
    try:
        result = verify(args.run_dir)
    except runread.RunReadError as exc:
        print(f"stemma verify: cannot read the run: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_json(), indent=1) if args.json else render(result))
    return result.exit_code


__all__ = ["RUN_ENTITY", "Result", "Section", "main", "render", "verify"]


if __name__ == "__main__":
    sys.exit(main())
