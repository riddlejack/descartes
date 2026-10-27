"""A run folder, read for the viewer and for verify. Reads only; writes nothing into the
run except the register's own lock file, which the register takes for a consistent
snapshot of the claims.

It tolerates both tree vocabularies: v0.3's `option`, `condition`, `rival` and
`open_lane` nodes, and v0.4's `question` and `open_lane` nodes with set-aside reasons
(V2 changes D2, D5). A missing optional file is an absence the viewer shows, never an
error; a missing or unreadable required file (`run.json`, the register's log) is a
`RunReadError` naming it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stemma.register import tags

RUN_ENTITY = "run:stemma:run"
FINAL_REVIEW_FILES = {"stemma:deciders-eye-reviewer": "deciders-eye.md",
                      "stemma:evidence-auditor": "evidence-audit.md",
                      "stemma:mechanical-reader": "mechanical.md"}
_CRITERION_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(C\d+)(?:\*\*)?\s*[:.)\-–—]\s*(?:\*\*)?\s*(.+)$")
_RECEIPT_ID = re.compile(r"r_[0-9]{8}T[0-9]{6}_[0-9a-f]{12}")
_TEXT_ID = re.compile(r"[0-9a-f]{64}(?:~[0-9a-f]{12})?")


class RunReadError(Exception):
    """A run folder lacks something every run has, or holds it in an unreadable form."""


@dataclass(frozen=True)
class Criterion:
    cid: str  # C1, C2, ...
    text: str


@dataclass(frozen=True)
class Deliverable:
    """The text a reader is shown, and what it is: the released deliverable, or (for a
    run that never released) the latest draft submitted for review, or the latest draft."""

    text: str
    kind: str  # "released" | "submitted_draft" | "draft" | "none"
    version: int | None
    path: str | None


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
    run_facts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return str(self.run.get("run_id") or self.dir.name)

    @property
    def question(self) -> str:
        return str(self.task.get("question") or "")

    def claim(self, short_or_full: str) -> dict[str, Any] | None:
        """The claim a tag or an input id names: an exact id, or the one id it begins."""
        key = short_or_full.strip().lower().removeprefix("c:")
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
def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RunReadError(f"{path.name} cannot be read as JSON ({type(exc).__name__})") from exc


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return None


def jsonl_records(path: Path) -> list[dict[str, Any]]:
    """Every complete JSON object line of a JSONL file; others are skipped."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for raw in data.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def receipt(run_dir: Path, receipt_id: str) -> dict[str, Any] | None:
    if not _RECEIPT_ID.fullmatch(receipt_id):
        return None
    rec = read_json(run_dir / "store" / "receipts" / f"{receipt_id}.json")
    return rec if isinstance(rec, dict) else None


def stored_text(run_dir: Path, rec: dict[str, Any]) -> str | None:
    """The stored text behind a receipt, or None when it has none."""
    tid = rec.get("text_id") or rec.get("sha256")
    if not isinstance(tid, str) or not _TEXT_ID.fullmatch(tid):
        return None
    return read_text(run_dir / "store" / "text" / f"{tid}.txt")


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
    text = read_text(run_dir / "criteria.md")
    if text:
        parsed = parse_criteria(text)
        if parsed:
            return parsed, "criteria.md"
    given = task.get("criteria")
    if isinstance(given, list) and given:
        return ([Criterion(f"C{i}", str(c)) for i, c in enumerate(given, start=1)],
                "task.json")
    return [], None


# ---------------------------------------------------------------- deliverable
def _deliverable(run_dir: Path, submitted: list[int]) -> Deliverable:
    released = read_text(run_dir / "deliverable" / "answer.md")
    if released and released.strip():
        version = None
        return Deliverable(released, "released", version, "deliverable/answer.md")
    if submitted:
        v = max(submitted)
        rel = f"register/drafts/answer.v{v}.md"
        text = read_text(run_dir / rel)
        if text:
            return Deliverable(text, "submitted_draft", v, rel)
    drafts = sorted((int(m.group(1)), p) for p in (run_dir / "draft").glob("answer.v*.md")
                    if (m := re.fullmatch(r"answer\.v(\d+)\.md", p.name)))
    if drafts:
        v, p = drafts[-1]
        return Deliverable(p.read_text(encoding="utf-8"), "draft", v, f"draft/{p.name}")
    return Deliverable("", "none", None, None)


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
        if f["source"].get("runId") in revoked:
            continue
        out.setdefault(str(f["predicate"]), []).append(f["value"])
    return out


def load(run_dir: Path) -> Run:
    from stemma.register.store import Register
    from stemma.register.wm import WorldModelError

    run_dir = Path(run_dir)
    run = read_json(run_dir / "run.json")
    if not isinstance(run, dict):
        raise RunReadError(f"{run_dir} has no run.json; is it a run folder?")
    task = read_json(run_dir / "task" / "task.json")
    if not isinstance(task, dict):
        task = run.get("task") if isinstance(run.get("task"), dict) else {}
    if not (run_dir / "register" / "log.jsonl").is_file():
        raise RunReadError(f"{run_dir} has no register/log.jsonl")
    try:
        reg = Register(run_dir)
        _pos, records = reg.claims_snapshot()
    except WorldModelError as exc:
        raise RunReadError(f"the register's log does not fold: {exc}") from exc
    facts = run_facts(reg)
    submitted = [int(d["version"]) for d in facts.get("draft_submitted", [])]
    tree = read_json(run_dir / "tree.json")
    nodes = list(tree.get("nodes") or []) if isinstance(tree, dict) else []
    criteria, source = _criteria(run_dir, task)
    return Run(dir=run_dir, run=run, task=task, criteria=criteria, criteria_source=source,
               nodes=nodes, claims={str(c["claim_id"]): c for c in records},
               deliverable=_deliverable(run_dir, submitted), run_facts=facts)
