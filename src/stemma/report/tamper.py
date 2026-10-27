"""`stemma tamper <run>`: the tamper demo. Copy a recorded run to a temporary folder,
change one byte of the stored source behind a figure the answer cites (or that figure
in the register's log), run `stemma verify` on the copy, and show exactly what fails and
that the release is refused. The original run is never touched.

The demo verifies the untouched copy first and refuses to go on if anything already
fails there: a demo on a run that does not verify shows nothing. It refuses a copy that
holds a link or a special file, takes every path it writes from a checked identifier, and
writes only inside the temporary copy, through the register's confined writer (no link
followed in any part of the path). It exits 0 when verify caught the change, 1 when it
did not, and 2 when it refused to run.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stemma.register import safeio
from stemma.report import runread, safefs, verify

_SHA = re.compile(r"[0-9a-f]{64}")

# What a verify of the copy needs; everything else (the driver's state, the assembled
# plugin, derived indexes, transcripts of tool calls) is left out (logical invariant).
COPY = ("run.json", "task", "criteria.md", "tree.json", "register", "analysis", "draft",
        "reviews", "framer", "branches", "deliverable", "store/bytes", "store/text",
        "store/receipts")
SKIP_IN_COPY = ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm", "calls")


class TamperError(Exception):
    """The run offers nothing the demo can change (no cited figure from a source)."""


@dataclass(frozen=True)
class Change:
    what: str  # one line saying what was changed
    detail: list[str]


def _clone(src: Path, dst: Path) -> None:
    """Copy a file tree; a copy-on-write clone where the file system offers one."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin" and src.is_dir():
        done = subprocess.run(["/bin/cp", "-c", "-R", str(src), str(dst)],
                              capture_output=True, check=False)
        if done.returncode == 0:
            for name in SKIP_IN_COPY:
                for p in dst.rglob(name):
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
            return
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_IN_COPY))
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def copy_run(run_dir: Path, dest: Path) -> None:
    for rel in COPY:
        src = run_dir / rel
        if src.exists() and not src.is_symlink():
            _clone(src, dest / rel)
    for p in dest.rglob("*"):  # stored files are written read-only; the copy is ours
        if p.is_file() and not p.is_symlink():
            p.chmod(0o644)


def _first_sourced_cited(run: runread.Run) -> tuple[int, dict[str, Any]] | None:
    for n, cid in enumerate(run.cited(), start=1):
        c = run.claims[cid]
        if c.get("support") and (c.get("value") or {}).get("as_written"):
            return n, c
    return None


def _bump_digit(data: bytes, at: int) -> bytes:
    old = data[at:at + 1]
    new = b"0" if old == b"9" else bytes([old[0] + 1]) if old.isdigit() else b"#"
    return data[:at] + new + data[at + 1:]


def tamper_source(run: runread.Run) -> Change:
    """Change one byte of the stored source behind the first sourced figure the answer
    cites: a digit of that figure where the raw bytes hold it, else the middle byte."""
    found = _first_sourced_cited(run)
    if found is None:
        raise TamperError("the answer cites no figure from a stored source")
    n, c = found
    item = c["support"][0]
    rec = runread.receipt(run.dir, item["receipt_id"]) or {}
    sha = rec.get("sha256")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise TamperError(f"receipt {item['receipt_id']} does not name stored bytes by a "
                          f"SHA-256")
    rel = f"store/bytes/{sha}"
    data = safefs.read_bytes(run.dir, rel)
    written = str(c["value"]["as_written"])
    digits = re.sub(r"\D", "", written)
    at = -1
    for form in (written, written.replace(",", ""), digits):
        if form and (i := data.find(form.encode("utf-8"))) >= 0:
            at = i + next(k for k, ch in enumerate(form) if ch.isdigit())
            break
    if at < 0:
        at = len(data) // 2
    changed = _bump_digit(data, at)
    _write(run.dir, rel, changed)
    url = rec.get("final_url") or rec.get("requested_url") or ""
    on_it = [str(k) for k, cid in enumerate(run.cited(), start=1)
             if any((runread.receipt(run.dir, str(s.get("receipt_id"))) or {}).get("sha256")
                    == sha for s in run.claims[cid].get("support") or [])]

    def show(b: bytes) -> str:
        return b[max(0, at - 30):at + 30].decode("utf-8", "replace").replace("\n", " ")

    return Change(
        f"one byte of the stored source behind figure {n} of the answer",
        [f"figure {n}: {c['statement'][:110]}",
         f"source: {url}",
         (f"receipt {item['receipt_id']}, byte {at:,} of {len(data):,}: "
         f"{data[at:at + 1].decode('latin-1')!r} became {changed[at:at + 1].decode('latin-1')!r}"),
         f"before: …{show(data)}…",
         f"after:  …{show(changed)}…",
         f"figures of the answer that rest on this source: {', '.join(on_it)}"])


def tamper_figure(run: runread.Run) -> Change:
    """Change the first cited figure's number in the register's log, as if someone had
    edited the record after the checks ran."""
    for n, cid in enumerate(run.cited(), start=1):
        number = (run.claims[cid].get("value") or {}).get("number")
        if isinstance(number, int | float) and not isinstance(number, bool):
            break
    else:
        raise TamperError("the answer cites no figure with a number")
    lines = safefs.read_bytes(run.dir, "register/log.jsonl").splitlines(keepends=True)
    needle = f'"number":{json.dumps(number)}'.encode()
    for i, line in enumerate(lines):
        if cid.encode() in line and b'"upsertEntity"' in line and needle in line:
            new_number = number + 1
            lines[i] = line.replace(needle, f'"number":{json.dumps(new_number)}'.encode(), 1)
            _write(run.dir, "register/log.jsonl", b"".join(lines))
            return Change(f"figure {n} of the answer, in the register's log",
                          [f"figure {n}: {run.claims[cid]['statement'][:110]}",
                           (f"claim c:{cid[:12]}: number {number!r} became {new_number!r} "
                           f"(log line {i + 1})")])
    raise TamperError(f"claim {cid[:12]} was not found in the log as filed")


def _write(copy: Path, rel: str, data: bytes) -> None:
    """Replace copy/rel through the register's confined writer: no link followed in any
    part of the path, the new bytes renamed into place inside the copy."""
    safefs.parts(rel)
    try:
        safeio.write_bytes(copy, rel, data)
    except (safeio.UnsafePath, OSError) as exc:
        raise TamperError(f"cannot write {rel} inside the copy: {exc}") from exc


def _lines(result: verify.Result) -> list[str]:
    return [f"     [{'FAIL' if s.failures else ' ok '}] {s.label.ljust(13)} "
            f"{verify.summary_line(s)}" for s in result.sections]


def demo(run_dir: Path, what: str = "source", keep: bool = False, out: Any = None) -> int:
    def say(line: str = "") -> None:
        print(line, file=out, flush=True)

    run_dir = Path(run_dir).resolve()
    original = runread.read_json(run_dir, "run.json")
    if not isinstance(original, dict):
        raise runread.RunReadError(f"{run_dir} has no run.json; is it a run folder?")
    work = Path(tempfile.mkdtemp(prefix="stemma-tamper-"))
    # The second pass re-runs only calculations whose code, inputs or documents differ
    # from the first pass's; byte-identical re-runs reuse the first pass's result.
    memo: verify.Memo = {}
    copy = work / "run"
    try:
        say(f"Tamper demo on a copy of run {original.get('run_id', run_dir.name)}. "
            f"The original is not touched.")
        say()
        t = time.monotonic()
        copy_run(run_dir, copy)
        say(f"  1. Copied the recorded run to {copy} ({time.monotonic() - t:.0f} s).")
        _files, problems = safefs.walk(copy)
        if problems:
            raise TamperError("the run holds what a demo must not copy: "
                              + "; ".join(problems[:5]))
        say("  2. Verifying the untouched copy:")
        base = verify.verify(copy, memo=memo)
        for line in _lines(base):
            say(line)
        if base.failures:
            raise TamperError(f"the untouched copy already fails verify ({len(base.failures)} "
                              f"failure(s), the first: {base.failures[0][0]}: "
                              f"{base.failures[0][1][:200]}); a demo on it would show nothing")
        run = runread.load(copy)
        change = tamper_source(run) if what == "source" else tamper_figure(run)
        say(f"  3. Tampering: {change.what}.")
        for d in change.detail:
            say(f"       {d}")
        say("  4. Verifying the tampered copy:")
        after = verify.verify(copy, memo=memo)
        for line in _lines(after):
            say(line)
        new = [f"{label}: {msg}" for label, msg in after.failures]
        say()
        if not new:
            say("NOT CAUGHT: verify found nothing new after the change. This is a failure "
                "of the check, and the demo exits 1.")
            return 1
        say(f"Caught. {len(new)} new failure(s), each named:")
        for i, msg in enumerate(new, start=1):
            say(f"  {i}. {msg}")
        say()
        say("Release: REFUSED. The answer rests on records that no longer match what was "
            "checked.")
        return 0
    finally:
        if keep:
            say(f"(The tampered copy is kept at {copy}.)")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stemma tamper", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--what", choices=("source", "figure"), default="source",
                    help="change one byte of a stored source (default) or one figure in "
                         "the register's log")
    ap.add_argument("--keep", action="store_true", help="keep the tampered copy")
    args = ap.parse_args(argv)
    try:
        return demo(args.run_dir, args.what, args.keep)
    except (runread.RunReadError, TamperError) as exc:
        print(f"stemma tamper: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
