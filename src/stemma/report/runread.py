"""A run folder, read for the viewer and for verify. Reads only, and writes, creates
and locks nothing: files are read through `safefs` (confined, bounded, read-only) and
the register through `ledger` (its log's bytes folded in memory).

It tolerates both tree vocabularies: v0.3's `option`, `condition`, `rival` and
`open_lane` nodes, and v0.4's `question` and `open_lane` nodes with set-aside reasons
(V2 changes D2, D5). A missing optional file is an absence the viewer shows. A missing
required file (`run.json`, the register's log), an unsafe path, or a record in the wrong
shape is a `RunReadError` naming it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from stemma.register import tags
from stemma.report import ledger, safefs
from stemma.report.errors import RunReadError

__all__ = ["FINAL_REVIEW_FILES", "RUN_ENTITY", "Criterion", "Deliverable", "Release", "Run",
           "RunReadError", "jsonl_records", "load", "parse_criteria", "read_json",
           "read_text", "receipt", "stored_text"]

RUN_ENTITY: Final = "run:stemma:run"
FINAL_REVIEW_FILES: Final = {"stemma:deciders-eye-reviewer": "deciders-eye.md",
                             "stemma:evidence-auditor": "evidence-audit.md",
                             "stemma:mechanical-reader": "mechanical.md"}
# Logical invariants: the shapes of ids the store and register write.
RECEIPT_ID: Final = re.compile(r"r_[0-9]{8}T[0-9]{6}_[0-9a-f]{12}")
TEXT_ID: Final = re.compile(r"[0-9a-f]{64}(?:~[0-9a-f]{12})?")
SHA256: Final = re.compile(r"[0-9a-f]{64}")
CLAIM_ID: Final = re.compile(r"[0-9a-f]{64}")
_CRITERION_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(C\d{1,3})(?:\*\*)?\s*[:.)\-–—]\s*(?:\*\*)?\s*(.+)$")
_DRAFT = re.compile(r"answer\.v(\d{1,6})\.md")
# Resource limits: tree nodes and claims a run may hold for these readers.
MAX_NODES: Final = 20_000
MAX_CLAIMS: Final = 200_000


@dataclass(frozen=True)
class Criterion:
    cid: str  # C1, C2, ...
    text: str


@dataclass(frozen=True)
class Deliverable:
    """The text a reader is shown and what it is: the released deliverable (only when the
    release is valid), else the latest draft submitted for review, else the latest draft."""

    text: str
    kind: str  # "released" | "submitted_draft" | "draft" | "none"
    version: int | None
    path: str | None


@dataclass(frozen=True)
class Release:
    """`released` only when the register recorded a finish, the deliverable's bytes hash
    to the recorded SHA-256, and no release block or withdrawal stands."""

    state: str  # "released" | "withdrawn" | "mismatch" | "not_released"
    detail: str
    version: int | None = None


@dataclass
class Run:
    dir: Path
    run: dict[str, Any]
    task: dict[str, Any]
    criteria: list[Criterion]
    criteria_source: str | None
    nodes: list[dict[str, Any]]
    claims: dict[str, dict[str, Any]]  # full claim id -> the register's record
    deliverable: Deliverable
    release: Release
    run_facts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    snapshot: Any = None  # the ledger.Snapshot the claims were read from

    @property
    def run_id(self) -> str:
        return str(self.run.get("run_id") or self.dir.name)

    @property
    def question(self) -> str:
        return str(self.task.get("question") or "")

    def claim(self, short_or_full: str) -> dict[str, Any] | None:
        """The claim a tag or an input id names: an exact id, or the one id it begins."""
        key = str(short_or_full).strip().lower().removeprefix("c:")
        if not re.fullmatch(r"[0-9a-f]{12,64}", key):
            return None
        if key in self.claims:
            return self.claims[key]
        hits = [c for cid, c in self.claims.items() if cid.startswith(key)]
        return hits[0] if len(hits) == 1 else None

    def cited(self) -> list[str]:
        """The full ids of the claims the deliverable's tags name, in first-use order;
        a tag that names no single claim is left out (the viewer lists it as broken)."""
        out: list[str] = []
        for short in dict.fromkeys(tags.claim_tags(self.deliverable.text)):
            c = self.claim(short)
            if c is not None:
                out.append(str(c["claim_id"]))
        return out


# ------------------------------------------------------------------------ files
def read_json(run_dir: Path, rel: str) -> Any:
    """The parsed JSON at run_dir/rel, or None when it does not exist."""
    return safefs.maybe(safefs.read_json, run_dir, rel)


def read_text(run_dir: Path, rel: str) -> str | None:
    """The UTF-8 text at run_dir/rel, or None when it does not exist."""
    text: str | None = safefs.maybe(safefs.read_text, run_dir, rel)
    return text


def jsonl_records(run_dir: Path, rel: str) -> list[dict[str, Any]]:
    """Every complete JSON object line of a JSONL file; others are skipped."""
    import json

    data = safefs.maybe(safefs.read_bytes, run_dir, rel) or b""
    out: list[dict[str, Any]] = []
    for raw in data.splitlines():
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def receipt(run_dir: Path, receipt_id: object) -> dict[str, Any] | None:
    if not isinstance(receipt_id, str) or not RECEIPT_ID.fullmatch(receipt_id):
        return None
    rec = read_json(run_dir, f"store/receipts/{receipt_id}.json")
    return rec if isinstance(rec, dict) else None


def stored_text(run_dir: Path, rec: dict[str, Any]) -> str | None:
    """The stored text behind a receipt, or None when it has none."""
    tid = rec.get("text_id") or rec.get("sha256")
    if not isinstance(tid, str) or not TEXT_ID.fullmatch(tid):
        return None
    return read_text(run_dir, f"store/text/{tid}.txt")


def flagged_ranges(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """A receipt's flagged ranges, each {start, end, kind}; a malformed list is refused."""
    ranges = rec.get("flagged_ranges") or []
    if not isinstance(ranges, list) or not all(
            isinstance(r, dict) and isinstance(r.get("start"), int)
            and isinstance(r.get("end"), int) and isinstance(r.get("kind"), str)
            and not isinstance(r["start"], bool) and not isinstance(r["end"], bool)
            for r in ranges):
        raise RunReadError(f"receipt {rec.get('receipt_id')!r}: flagged_ranges is not a "
                           f"list of {{start, end, kind}}")
    return ranges


# ------------------------------------------------------------------- criteria
def parse_criteria(text: str) -> list[Criterion]:
    """Criteria written as lines `C1: ...` (or `- **C1.** ...`) in criteria.md."""
    out: list[Criterion] = []
    for line in text.splitlines():
        m = _CRITERION_LINE.match(line)
        if m:
            body = m.group(2).strip().removesuffix("**").strip()
            out.append(Criterion(m.group(1), body))
    return out


def _criteria(run_dir: Path, task: dict[str, Any]) -> tuple[list[Criterion], str | None]:
    text = read_text(run_dir, "criteria.md")
    if text:
        parsed = parse_criteria(text)
        if parsed:
            return parsed, "criteria.md"
    given = task.get("criteria")
    if isinstance(given, list) and given:
        return ([Criterion(f"C{i}", str(c)) for i, c in enumerate(given, start=1)],
                "task.json")
    return [], None


# ------------------------------------------------------------ release, drafts
def _int(v: object, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise RunReadError(f"{what} is not a whole number")
    return v


def submitted_versions(facts: dict[str, list[dict[str, Any]]]) -> list[int]:
    out = []
    for d in facts.get("draft_submitted", []):
        if not isinstance(d, dict):
            raise RunReadError("a draft_submitted record is not an object")
        out.append(_int(d.get("version"), "a draft_submitted version"))
    return out


def release_state(run_dir: Path, facts: dict[str, list[dict[str, Any]]]) -> Release:
    from stemma.register import checks_api

    try:
        blocks = checks_api.release_blocks(run_dir)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RunReadError(f"register/checks.jsonl cannot be read ({type(exc).__name__})") \
            from exc
    finished = facts.get("finished") or []
    exists = safefs.maybe(safefs.read_bytes, run_dir, "deliverable/answer.md")
    if not finished:
        detail = ("deliverable/answer.md exists, but the register recorded no release; "
                  "it is not shown" if exists else "the register recorded no release")
        return Release("not_released", detail)
    last = finished[-1]
    if not isinstance(last, dict) or not isinstance(last.get("sha256"), str):
        raise RunReadError("the register's finish record has no SHA-256")
    version = _int(last.get("version"), "the finish record's version")
    if exists is None or hashlib.sha256(exists).hexdigest() != last["sha256"]:
        return Release("mismatch", "deliverable/answer.md does not match the SHA-256 the "
                       "register recorded at release; it is not shown", version)
    withdrawn = facts.get("release_withdrawn") or []
    if withdrawn or blocks:
        why = "; ".join([str(w.get("reason")) for w in withdrawn if isinstance(w, dict)]
                        + [str(b) for b in blocks])
        return Release("withdrawn", f"the release was withdrawn: {why}", version)
    return Release("released", "the register recorded the release, and the deliverable's "
                   "bytes match it", version)


def _deliverable(run_dir: Path, release: Release, submitted: list[int]) -> Deliverable:
    if release.state == "released":
        text = read_text(run_dir, "deliverable/answer.md") or ""
        return Deliverable(text, "released", release.version, "deliverable/answer.md")
    if submitted:
        v = max(submitted)
        rel = f"register/drafts/answer.v{v}.md"
        frozen = read_text(run_dir, rel)
        if frozen:
            return Deliverable(frozen, "submitted_draft", v, rel)
    drafts = sorted(int(m.group(1)) for n in safefs.listdir(run_dir, "draft")
                    if (m := _DRAFT.fullmatch(n)))
    if drafts:
        rel = f"draft/answer.v{drafts[-1]}.md"
        return Deliverable(read_text(run_dir, rel) or "", "draft", drafts[-1], rel)
    return Deliverable("", "none", None, None)


# ----------------------------------------------------------------- validation
def _check_claim(c: Any) -> dict[str, Any]:
    if not isinstance(c, dict) or not isinstance(c.get("claim_id"), str) \
            or not CLAIM_ID.fullmatch(c["claim_id"]):
        raise RunReadError("a claim record has no valid claim id")
    where = f"claim {c['claim_id'][:12]}"
    if not isinstance(c.get("statement"), str) or not isinstance(c.get("tier"), str):
        raise RunReadError(f"{where}: statement or tier is not text")
    support = c.get("support") or []
    if not isinstance(support, list) or not all(
            isinstance(s, dict) and isinstance(s.get("receipt_id"), str)
            and isinstance(s.get("quote"), str) for s in support):
        raise RunReadError(f"{where}: support is not a list of {{receipt_id, quote}}")
    for key in ("value", "derivation", "verdict"):
        if c.get(key) is not None and not isinstance(c[key], dict):
            raise RunReadError(f"{where}: {key} is not an object")
    deriv = c.get("derivation") or {}
    if deriv and not (isinstance(deriv.get("script"), str)
                      and isinstance(deriv.get("inputs", []), list)
                      and isinstance(deriv.get("documents", []), list)):
        raise RunReadError(f"{where}: derivation is not {{script, inputs, documents}}")
    return c


def _check_nodes(tree: Any) -> list[dict[str, Any]]:
    if tree is None:
        return []
    nodes = tree.get("nodes") if isinstance(tree, dict) else None
    if not isinstance(nodes, list) or len(nodes) > MAX_NODES:
        raise RunReadError(f"tree.json is not {{nodes: [...]}} with at most {MAX_NODES:,} "
                           f"nodes")
    for i, n in enumerate(nodes, start=1):
        if not isinstance(n, dict) or not isinstance(n.get("id"), str | int) \
                or isinstance(n.get("id"), bool):
            raise RunReadError(f"tree.json node {i} has no id")
        if n.get("parent") is not None and not isinstance(n["parent"], str | int):
            raise RunReadError(f"tree.json node {n['id']}: parent is not an id")
    return nodes


# ----------------------------------------------------------------------- load
def run_facts(register: Any) -> dict[str, list[dict[str, Any]]]:
    """Facts about the run entity by predicate, oldest first, without those written by a
    session the driver revoked (the register's own reading)."""
    from stemma.register import checks_api

    revoked = checks_api.revoked_sessions(register.run_dir)
    out: dict[str, list[dict[str, Any]]] = {}
    if register.wm.view("graph").entity(RUN_ENTITY) is None:
        return out
    for f in register.wm.view("facts").about(RUN_ENTITY):
        if (f.get("source") or {}).get("runId") in revoked:
            continue
        out.setdefault(str(f["predicate"]), []).append(f["value"])
    return out


def _claims(reg: Any) -> list[dict[str, Any]]:
    ids = sorted(str((e.get("externalKey") or {}).get("id"))
                 for e in reg.wm.view("graph").query({"type": "claim"}))
    if len(ids) > MAX_CLAIMS:
        raise RunReadError(f"the register holds more than {MAX_CLAIMS:,} claims")
    return [_check_claim(reg._claim_record(cid)) for cid in ids]


def load(run_dir: Path) -> Run:
    run_dir = Path(run_dir)
    run = read_json(run_dir, "run.json")
    if not isinstance(run, dict):
        raise RunReadError(f"{run_dir} has no run.json; is it a run folder?")
    task = read_json(run_dir, "task/task.json")
    if not isinstance(task, dict):
        task = run.get("task") if isinstance(run.get("task"), dict) else {}
    if not safefs.is_regular(run_dir, ledger.LOG):
        raise RunReadError(f"{run_dir} has no register/log.jsonl as a plain file")
    snap = ledger.snapshot(run_dir)
    try:
        records = _claims(snap.register)
        facts = run_facts(snap.register)
    except RunReadError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError,
            RecursionError) as exc:
        raise RunReadError(f"a register record is not in the register's shape "
                           f"({type(exc).__name__}: {exc})") from exc
    release = release_state(run_dir, facts)
    nodes = _check_nodes(read_json(run_dir, "tree.json"))
    criteria, source = _criteria(run_dir, task)
    return Run(dir=run_dir, run=run, task=task, criteria=criteria, criteria_source=source,
               nodes=nodes, claims={str(c["claim_id"]): c for c in records},
               deliverable=_deliverable(run_dir, release, submitted_versions(facts)),
               release=release, run_facts=facts, snapshot=snap)
