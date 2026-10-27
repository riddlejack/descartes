"""The claim register and the tree, on Meterless World Model's data model.

How the register maps onto World Model (one file log, `register/log.jsonl`):
- a claim is an entity `claim:stemma:<claim_id>` (an external key, so its id is
  content-derived and independent writers agree on it); its attrs hold the fields the
  agent supplied plus what code wrote (author, created_at, check results, warnings);
- filing the same claim again changes nothing (idempotent re-ingest);
- each claim belongs to a lineage `lineage:stemma:<first claim_id>` through an open
  `current` edge; a correction relates the lineage to the corrected claim, which
  closes the old edge (`validTo`, `supersededBy`): superseded links are closed, never
  deleted. A `superseded_by` fact on the old claim says the same for quick reads;
- a verdict is a `verdict` fact on the claim; the newest wins and older ones stay;
- an entity name is a name-keyed `org` entity, so near-duplicates go to World Model's
  review queue; a parent mapping needs two sources;
- a tree node is an entity `node:stemma:<node_id>`; every change is a World Model
  transaction, so the log is the tree's history; `tree.json` is rendered from it;
- run-level events (drafts submitted, frame critiques asked, the run finished) are
  facts on `run:stemma:run`.
Check requests live in `register/checks.jsonl` (`checks_api`). `register/index.sqlite`
is a derived index rebuilt from the log.

Identity is a `Caller` the MCP server builds from the harness stamp; no method takes a
role from a tool argument.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from stemma.register import checks_api, filing, jsonl, roles, safeio, tags
from stemma.register import criteria as criteria_mod
from stemma.register.derivation import RerunResult, rerun
from stemma.register.evidence_port import Evidence, QuoteCheckFailed
from stemma.register.evidence_port import load as load_evidence
from stemma.register.filing import Refused
from stemma.register.wm import WorldModel, WorldModelError, entityIdFromName

SYSTEM = "stemma"
RUN_ENTITY = "run:stemma:run"

VERDICTS = ("holds", "holds_with_correction", "not_supported", "could_not_check")
RELEASABLE = frozenset({"holds", "holds_with_correction", "could_not_check"})
# Tree node kinds (CONTRACTS v0.4 section 5, decision D2): the tree is the decomposition
# of the question asked, so its nodes are sub-questions; an open lane holds what the
# frame has not placed yet. Options, rivals, gates and checks are never nodes.
NODE_KINDS = ("question", "open_lane")
NODE_FIELDS = frozenset({"id", "parent", "kind", "statement", "doubt", "settles_by",
                         "flip_point", "crux", "branch_id", "status", "set_aside"})
NODE_RECORD_FIELDS = NODE_FIELDS | {"reopened_from", "set_aside_from"}
STATUSES = ("open", "in_progress", "proposed_done", "answered", "gap_accepted",
            "waiting_user", "stopped_by_limit", "set_aside")
DONE = frozenset({"answered", "gap_accepted", "stopped_by_limit"})
SETTABLE_BY_AUTHORS = frozenset({"open", "in_progress"})
# Statuses a branch verdict set or is about to set; content changes reopen the node.
REVIEWED = frozenset({"proposed_done", "answered", "gap_accepted"})
# Resource limit: rounds of the three final reviews when run.json does not say (CONTRACTS
# section 6, `max_review_rounds`); never a quality bar.
DEFAULT_MAX_REVIEW_ROUNDS = 3
# Resource limit: characters of a set-aside reason (shown in the tree and the deliverable).
SET_ASIDE_MAX_CHARS = 2000
GAP_TYPES = ("user_held", "source_unavailable", "method_exhausted", "out_of_scope")
BRANCH_VERDICTS = {"answered": "answered", "accept_gap": "gap_accepted",
                   "find_more": "in_progress", "analyse": "in_progress",
                   "bound": "in_progress", "ask_user": "waiting_user"}
# Verdicts that send a branch back to its worker for a material point (D9: once).
SEND_BACK = frozenset({"find_more", "analyse", "bound"})
# Verdicts that close a branch; each accounts for every point sent back earlier.
CLOSING = frozenset({"answered", "accept_gap"})
# What became of a point sent back (D9): fixed; bounded or made conditional in the
# record (which then names the point's id); or disputed by the worker, the lead deciding
# in the branch's revision order (which names the point's id).
RESOLUTIONS = ("resolved", "bounded", "conditional", "disputed")
# The fields of a review point (D4): what it says, whether it is material (would change
# the answer or whether a criterion is met; the reviewer's judgment), and the tree node
# and criterion it concerns (at least one).
POINT_FIELDS = frozenset({"point", "material", "node", "criterion"})
# Headings of a revision order (D6), in order.
REVISION_HEADINGS = ("Finding", "Raised by", "Is it right?", "Fix")
RECORD_HEADINGS = ("Conclusion and confidence", "Claims (ids)", "Model", "Rival tested",
                   "Methods tried and what each returned", "Gaps (typed)",
                   "Implications for other branches", "What was not checked")
REVIEW_FILES = ("deciders-eye.md", "evidence-audit.md", "mechanical.md")
REVIEW_FILE_OF = dict(zip(roles.FINAL_REVIEWERS, REVIEW_FILES, strict=True))
# Short names in review point ids (`v<version>.<abbr>.<n>`), one per final reviewer.
REVIEW_ABBR = dict(zip(roles.FINAL_REVIEWERS, ("de", "ea", "mr"), strict=True))
FRAME_FILES = {"own": "framer/own-frame.md", "critique": "framer/critique.md"}

# Resource limits: how long await_checks may block in one call, and how often it looks.
AWAIT_MAX_S = 300
AWAIT_POLL_S = 2.0

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def closure(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """How each node closes and whether it has (CONTRACTS v0.4 section 6), by node id:
    `closes_by` is "branch" (a leaf: a question, or an open lane holding work), "children"
    (any node with children; its status is derived), "nothing" (an empty open lane),
    "set_aside" (the lead set it aside with a reason, D5: closed, and so is everything
    under it, marked "ancestor_set_aside" with `set_aside_by`), or "unreachable" (no path
    up to a root: its parents form a cycle or lead to no node). The walk starts at the
    roots and visits each reachable node once."""
    by_id = {str(n["id"]): n for n in nodes}
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for nid, n in by_id.items():
        parent = n.get("parent")
        if parent is None:
            roots.append(nid)
        else:
            children.setdefault(str(parent), []).append(nid)
    order: list[str] = []  # reachable nodes, each once, parents before children
    aside_by: dict[str, str] = {}  # node -> the set-aside ancestor (or itself)
    seen: set[str] = set()
    stack = sorted(roots, reverse=True)
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        order.append(nid)
        parent = by_id[nid].get("parent")
        if parent is not None and str(parent) in aside_by:
            aside_by[nid] = aside_by[str(parent)]
        elif by_id[nid].get("set_aside"):
            aside_by[nid] = nid
        stack.extend(sorted(children.get(nid, []), reverse=True))
    out: dict[str, dict[str, Any]] = {}
    for nid in reversed(order):  # children before parents
        n, kids = by_id[nid], children.get(nid, [])
        if nid in aside_by:
            by = aside_by[nid]
            res: dict[str, Any] = {"closes_by": "set_aside" if by == nid
                                   else "ancestor_set_aside",
                                   "status": "set_aside", "closed": True, "set_aside_by": by}
        elif kids:
            subs = [out[k] for k in kids]
            if all(c["closed"] for c in subs):
                live = [c for c in subs if c["status"] != "set_aside"]
                st = ("set_aside" if not live
                      else "stopped_by_limit" if any(c["status"] == "stopped_by_limit"
                                                     for c in live)
                      else "gap_accepted" if any(c["status"] == "gap_accepted" for c in live)
                      else "answered")
                closed = True
            else:
                st = "open" if all(c["status"] in ("open", "set_aside") for c in subs) \
                    else "in_progress"
                closed = False
            res = {"closes_by": "children", "status": st, "closed": closed}
        elif n["kind"] == "open_lane" and not n.get("branch_id"):
            res = {"closes_by": "nothing", "status": "not_needed", "closed": True}
        else:
            res = {"closes_by": "branch", "status": n["status"], "closed": n["status"] in DONE}
        out[nid] = res
    for nid in by_id:
        if nid not in out:
            out[nid] = {"closes_by": "unreachable", "status": "open", "closed": False}
    return out


def _concerns(pt: dict[str, Any]) -> str:
    """What a point concerns, for a gate line: `criterion C2, node n3`."""
    parts = []
    if pt.get("criterion"):
        parts.append(f"criterion {pt['criterion']}")
    if pt.get("node"):
        parts.append(f"node {pt['node']}")
    return ", ".join(parts) or "no criterion or node recorded"


def gate_refusal(failures: list[str]) -> str:
    """finish_run's refusal: every unmet condition, counted and one per line, so the
    lead can read the whole gate (CONTRACTS section 6, v0.3)."""
    lines = "\n".join(f"{i}. {f}" for i, f in enumerate(failures, start=1))
    return f"Refused: finish_run: {len(failures)} unmet conditions:\n{lines}"

@dataclass(frozen=True)
class Caller:
    agent_type: str
    agent_id: str | None
    session_id: str


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def claim_entity(cid: str) -> str:
    return f"claim:{SYSTEM}:{cid}"


def node_entity(node_id: str) -> str:
    return f"node:{SYSTEM}:{node_id}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Register:
    def __init__(self, run_dir: Path, evidence: Evidence | None = None,
                 rerun_fn: Callable[[Path, dict[str, float]], RerunResult] | None = None
                 ) -> None:
        self.run_dir = run_dir
        self.dir = run_dir / "register"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wm = WorldModel({"storage": "file", "path": str(self.dir / "log.jsonl"),
                              "namespace": "register"})
        self._evidence = evidence
        self._rerun = rerun_fn or rerun
        self._relied: list[str] = []
        self._lock = self.dir / "register"  # jsonl.exclusive locks `register.lock`

    @property
    def evidence(self) -> Evidence:
        if self._evidence is None:
            self._evidence = load_evidence()
        return self._evidence

    # ------------------------------------------------------------------ helpers
    def _src(self, caller: Caller, kind: str = "tool") -> dict[str, Any]:
        src: dict[str, Any] = {"kind": kind, "at": now(), "by": caller.agent_type}
        if caller.session_id:
            src["runId"] = caller.session_id
        return src

    def _ensure_entity(self, entity_id: str, etype: str, key: str, source: dict[str, Any],
                       attrs: dict[str, Any] | None = None) -> None:
        if self.wm.view("graph").entity(entity_id) is None:
            self.wm.upsertEntity({"type": etype, "externalKey": {"system": SYSTEM, "id": key},
                                  "attrs": attrs or {}, "source": source})

    def _require(self, caller: Caller, allowed: frozenset[str], what: str) -> None:
        if caller.agent_type not in allowed:
            raise Refused(f"Refused: {roles.short(caller.agent_type)} sessions do not {what}.")

    def _session_prompt_is(self, caller: Caller, role: str, prompt: str) -> None:
        """The caller is the main thread of the session the driver launched for `prompt`.

        A subagent shares its parent's session id, so a checker started inside any
        session (even a checking one) carries an `agent_id` and is refused here."""
        if caller.agent_id:
            raise Refused(f"Refused: this session was not started for {prompt}.")
        state = checks_api.state_for_session(self.run_dir, caller.session_id)
        if state is None or state.request.role != role or state.request.prompt != prompt:
            raise Refused(f"Refused: this session was not started for {prompt}.")
        rounds = checks_api.for_prompt(self.run_dir, role, prompt)
        if state.request.round != max(r.request.round for r in rounds):
            raise Refused(f"Refused: this session was started for an earlier round of {prompt}.")

    def _revoked(self) -> frozenset[str]:
        return checks_api.revoked_sessions(self.run_dir)

    def _live_facts(self, about: str, predicate: str) -> list[dict[str, Any]]:
        """Facts about an entity, oldest first, without those a revoked session wrote."""
        revoked = self._revoked()
        return [dict(f) for f in self.wm.view("facts").about(about, {"predicate": predicate})
                if f["source"].get("runId") not in revoked]

    def _verified(self) -> frozenset[str]:
        return checks_api.verified_sessions(self.run_dir)

    def _read(self, rel: str | PurePosixPath) -> bytes:
        """An agent-written file's bytes, refusing symlinks anywhere on the path."""
        try:
            return safeio.read_bytes(self.run_dir, rel)
        except FileNotFoundError as exc:
            raise Refused(f"Refused: {rel} does not exist.") from exc
        except (safeio.UnsafePath, NotADirectoryError) as exc:
            raise Refused(f"Refused: {rel} is not a plain file.") from exc

    def _write(self, rel: str | PurePosixPath, text: str) -> None:
        try:
            safeio.write_text(self.run_dir, rel, text)
        except (safeio.UnsafePath, NotADirectoryError) as exc:
            raise Refused(f"Refused: {rel} cannot be written safely.") from exc

    # ------------------------------------------------------------------- claims
    def resolve_claim_id(self, given: str) -> str:
        g = given.strip().lower()
        if g.startswith("claim:"):
            g = g.split(":", 1)[1]
        g = g.removeprefix("c:")
        if not re.fullmatch(r"[0-9a-f]{12,64}", g):
            raise Refused(f"Refused: {given!r} is not a claim id.")
        self.wm.refresh()
        hits = [e["externalKey"]["id"] for e in self.wm.view("graph").query({"type": "claim"})
                if e["externalKey"]["id"].startswith(g)]
        if len(hits) != 1:
            raise Refused(f"Refused: no single claim has id {given}.")
        return hits[0]

    def _claim_record(self, cid: str) -> dict[str, Any]:
        e = self.wm.view("graph").entity(claim_entity(cid))
        if e is None:
            raise Refused(f"Refused: no claim has id {cid}.")
        refused = self._sessions_with_refused_correction(cid)
        # A session whose correction of this claim was refused found it wrong: a holds it
        # recorded on the claim, before or after, does not stand.
        verdicts = [f for f in self._live_facts(claim_entity(cid), "verdict")
                    if not ((f["value"] or {}).get("verdict") == "holds"
                            and f["source"].get("runId") in refused)]
        superseded = self._live_facts(claim_entity(cid), "superseded_by")
        latest = verdicts[-1] if verdicts else None
        rec = dict(e["attrs"]["claim"])
        rec.update({
            "claim_id": cid,
            "tag": f"[[c:{cid[:12]}]]",
            "author": e["attrs"]["author"],
            "created_at": e["attrs"]["created_at"],
            "checks": e["attrs"]["checks"],
            "warnings": e["attrs"]["warnings"],
            "supersedes": e["attrs"].get("supersedes"),
            "superseded_by": superseded[-1]["value"] if superseded else None,
            "verdict": latest["value"] if latest else None,
            "verdict_session": latest["source"].get("runId") if latest else None,
            "verdict_history": [f["value"] for f in verdicts],
        })
        return rec

    def get_claim(self, given: str) -> dict[str, Any]:
        return self._claim_record(self.resolve_claim_id(given))

    def _lookup_input(self, given: str) -> tuple[str, float]:
        cid = self.resolve_claim_id(given)
        number = (self._claim_record(cid).get("value") or {}).get("number")
        if not isinstance(number, int | float):
            raise Refused(f"Refused: derivation input {given} has no number.")
        return cid, float(number)

    def claims_snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        """Every claim record at one log position, under the writers' lock (D, 5a)."""
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            log = self.dir / "log.jsonl"
            position = log.stat().st_size if log.exists() else 0
            ids = sorted(e["externalKey"]["id"] for e in self.wm.view("graph").query(
                {"type": "claim"}))
            return position, [self._claim_record(cid) for cid in ids]

    def _inputs(self) -> Callable[[str], tuple[str, float]]:
        """Derivation inputs; through the data layer when STEMMA_DATALAYER=zetaris (5a)."""
        if os.environ.get("STEMMA_DATALAYER") != "zetaris":
            return self._lookup_input
        from stemma.datalayer import InputMismatch, input_lookup

        def alert(record: dict[str, Any]) -> None:  # loud on the run, never a refusal
            jsonl.append(self.run_dir / "events.jsonl", {**record, "at": now(),
                                                          "source": "register"})

        through = input_lookup(self.run_dir, self._lookup_input, claims=self, alert=alert)

        def lookup(given: str) -> tuple[str, float]:
            try:
                return through(given)
            except InputMismatch as exc:
                raise Refused(f"Refused: input_mismatch: derivation input {given}.") from exc

        return lookup

    def _store_claim(self, result: filing.FilingResult, caller: Caller,
                     supersedes: str | None) -> tuple[str, bool]:
        cid = filing.claim_id_of(result.claim)
        source = self._src(caller)
        existed = self.wm.view("graph").entity(claim_entity(cid)) is not None
        if not existed:
            self.wm.upsertEntity({
                "type": "claim", "externalKey": {"system": SYSTEM, "id": cid},
                "attrs": {"claim": result.claim, "author": caller.agent_type,
                          "author_agent_id": caller.agent_id, "created_at": source["at"],
                          "checks": result.checks, "warnings": result.warnings,
                          "supersedes": supersedes},
                "source": source,
            })
        root = self._lineage_of(supersedes) if supersedes is not None else cid
        lineage = f"lineage:{SYSTEM}:{root}"
        self._ensure_entity(lineage, "lineage", root, source)
        current = self.wm.view("graph").neighborsOf(lineage, {"via": "current"})
        if not any(n["id"] == claim_entity(cid) for n in current):
            self.wm.relate({"from": lineage, "type": "current", "to": claim_entity(cid),
                            "validFrom": source["at"], "source": source})
        for name in result.claim["entities"]:
            if self.wm.view("graph").entity(entityIdFromName("org", name)) is None:
                self.wm.upsertEntity({"type": "org", "name": name, "source": source})
        return cid, existed

    def _lineage_of(self, cid: str) -> str:
        for lin in self.wm.view("graph").query({"type": "lineage"}):
            for edge in self.wm.view("graph").edges(lin["id"], {"via": "current", "direction": "out",
                                                               "includeClosed": True}):
                if edge["to"] == claim_entity(cid):
                    return str(lin["externalKey"]["id"])
        return cid

    def file_claim(self, caller: Caller, raw: dict[str, Any]) -> dict[str, Any]:
        self._require(caller, roles.AUTHORS, "file claims")
        result = filing.run_checks(raw, self.run_dir, self.evidence, self._rerun,
                                   self._inputs())
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            cid, existed = self._store_claim(result, caller, None)
            req = checks_api.request_check(self.run_dir, roles.CLAIM_CHECKER, f"claim:{cid}",
                                           "file_claim")
            self.rebuild_index()
        return {"claim_id": cid, "tag": f"[[c:{cid[:12]}]]", "already_filed": existed,
                "checks": result.checks, "warnings": result.warnings,
                "check_requested": req.request_id}

    def record_claim_verdict(self, caller: Caller, claim_id: str, verdict: str,
                             evidence: str, corrected_claim: dict[str, Any] | None = None
                             ) -> dict[str, Any]:
        self._require(caller, frozenset({roles.CLAIM_CHECKER}), "record claim verdicts")
        cid = self.resolve_claim_id(claim_id)
        self._session_prompt_is(caller, "claim-checker", f"claim:{cid}")  # early, cheap refusal
        if verdict not in VERDICTS:
            raise Refused(f"Refused: verdict must be one of {', '.join(VERDICTS)}.")
        if verdict == "holds" and self._correction_refused(caller.session_id, cid):
            # The session found the claim wrong in a detail: holds would contradict it.
            raise Refused("Refused: this session's correction of this claim was refused, so "
                          "it found an error; holds would contradict that. The verdicts "
                          "left are not_supported and could_not_check.")
        try:
            return self._record_claim_verdict(caller, cid, verdict, evidence, corrected_claim)
        except Refused as exc:
            if verdict == "holds_with_correction":
                jsonl.append(self._refused_corrections_log(),
                             {"session_id": caller.session_id, "claim_id": cid,
                              "reason": str(exc)[:500],
                              "at": datetime.now(UTC).isoformat(timespec="milliseconds")})
            raise

    def _refused_corrections_log(self) -> Path:
        return self.run_dir / "register" / "refused_corrections.jsonl"

    def _sessions_with_refused_correction(self, cid: str) -> frozenset[str]:
        return frozenset(r["session_id"] for r in jsonl.read_all(self._refused_corrections_log())
                         if r["claim_id"] == cid)

    def _correction_refused(self, session_id: str | None, cid: str) -> bool:
        return session_id in self._sessions_with_refused_correction(cid)

    def _record_claim_verdict(self, caller: Caller, cid: str, verdict: str, evidence: str,
                              corrected_claim: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(evidence, str) or not evidence.strip():
            raise Refused("Refused: a verdict carries its evidence.")
        if (verdict == "holds_with_correction") != (corrected_claim is not None):
            raise Refused("Refused: a corrected claim comes with holds_with_correction only.")
        corrected = None
        if corrected_claim is not None:
            corrected = filing.run_checks(corrected_claim, self.run_dir, self.evidence,
                                          self._rerun, self._inputs())
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            self._session_prompt_is(caller, "claim-checker", f"claim:{cid}")  # binding, in lock
            if self._claim_record(cid)["superseded_by"] is not None:
                raise Refused("Refused: the claim is already superseded.")
            source = self._src(caller, kind="claim-checker")
            out: dict[str, Any] = {"claim_id": cid, "verdict": verdict}
            if corrected is not None:
                new_id = filing.claim_id_of(corrected.claim)
                if self.wm.view("graph").entity(claim_entity(new_id)) is not None:
                    raise Refused("Refused: the corrected claim is already in the register.")
                new_cid, _ = self._store_claim(corrected, caller, cid)
                self.wm.assertFact({"about": claim_entity(cid), "predicate": "superseded_by",
                                    "value": new_cid, "source": source})
                self.wm.assertFact({"about": claim_entity(new_cid), "predicate": "verdict",
                                    "value": {"verdict": "holds", "evidence": evidence,
                                              "at_filing": True, "session": caller.session_id},
                                    "source": source})
                out["corrected_claim_id"] = new_cid
                out["dependents"] = self.dependents(cid)
            self.wm.assertFact({"about": claim_entity(cid), "predicate": "verdict",
                                "value": {"verdict": verdict, "evidence": evidence,
                                          "session": caller.session_id},
                                "source": source})
            self.rebuild_index()
        return out

    def find_claims(self, text: str | None = None, branch_id: str | None = None,
                    tier: str | None = None, entity: str | None = None,
                    include_superseded: bool = False) -> list[dict[str, Any]]:
        self.wm.refresh()
        out = []
        for e in self.wm.view("graph").query({"type": "claim"}):
            rec = self._claim_record(e["externalKey"]["id"])
            if not include_superseded and rec["superseded_by"]:
                continue
            if text and text.lower() not in rec["statement"].lower():
                continue
            if branch_id and rec.get("branch_id") != branch_id:
                continue
            if tier and rec["tier"] != tier:
                continue
            if entity and entity.lower() not in [n.lower() for n in rec["entities"]]:
                continue
            out.append({"claim_id": rec["claim_id"], "tag": rec["tag"],
                        "statement": rec["statement"], "tier": rec["tier"],
                        "branch_id": rec.get("branch_id"),
                        "verdict": (rec["verdict"] or {}).get("verdict"),
                        "superseded_by": rec["superseded_by"]})
        return sorted(out, key=lambda r: r["claim_id"])

    # ----------------------------------------------------- echoes and dependents
    def _document_of(self, receipt_id: str) -> str:
        path = self.run_dir / "store" / "receipts" / f"{receipt_id}.json"
        try:
            sha = json.loads(path.read_text(encoding="utf-8")).get("sha256")
        except (OSError, ValueError):
            sha = None
        return f"sha256:{sha}" if sha else f"receipt:{receipt_id}"

    def echo_candidates(self) -> list[dict[str, Any]]:
        """Live claims citing the same document. Code lists; the lead judges."""
        groups: dict[str, set[str]] = {}
        for c in self.find_claims():
            rec = self._claim_record(c["claim_id"])
            for s in rec["support"]:
                groups.setdefault(self._document_of(s["receipt_id"]), set()).add(rec["claim_id"])
        return [{"document": doc, "claim_ids": sorted(ids)}
                for doc, ids in sorted(groups.items()) if len(ids) > 1]

    def dependents(self, claim_id: str) -> list[dict[str, Any]]:
        """Every file in the run that uses the claim's tag, with who last wrote it."""
        short = claim_id[:12]
        writers: dict[str, str] = {}
        for ev in jsonl.read_all(self.run_dir / "events.jsonl"):
            if ev.get("event") == "file_write" and ev.get("path"):
                writers[str(ev["path"])] = str(ev.get("agent_type"))
        skip = {"store", "register", ".stemma", ".claude"}
        out = []
        files: list[PurePosixPath] = []
        for top, dirs, names in os.walk(self.run_dir, followlinks=False):
            base = PurePosixPath(Path(top).relative_to(self.run_dir).as_posix())
            if base.parts and base.parts[0] in skip:
                dirs[:] = []
                continue
            files += [base / n for n in names
                      if PurePosixPath(n).suffix in (".md", ".py", ".json", ".txt", ".csv")]
        for rel in sorted(files):
            try:  # regular files only: a planted symlink or FIFO is skipped, never followed
                text = safeio.read_text(self.run_dir, rel)
            except (OSError, UnicodeDecodeError, safeio.UnsafePath):
                continue
            if short in tags.claim_tags(text) or claim_id in text:
                key = str(rel).casefold()
                out.append({"path": str(rel), "owner": writers.get(key, writers.get(str(rel)))})
        return out

    # ----------------------------------------------------------------- entities
    def entity_review(self, caller: Caller, action: str = "list", proposal_id: str = "",
                      reason: str = "", name: str = "", parent: str = "",
                      support: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if action == "list":
            return {"pending": self.wm.mergeQueue("pending")}
        self._require(caller, frozenset({roles.LEAD}), "decide entity reviews")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            try:
                if action == "approve":
                    return {"proposal": self.wm.approveMerge(proposal_id, caller.agent_type,
                                                             reason or None)}
                if action == "reject":
                    return {"proposal": self.wm.rejectMerge(proposal_id, caller.agent_type,
                                                            reason or None)}
            except WorldModelError as exc:
                raise Refused(f"Refused: {exc}.") from exc
            if action == "parent":
                return self._map_parent(caller, name, parent, support or [])
        raise Refused("Refused: action must be list, approve, reject or parent.")

    def _map_parent(self, caller: Caller, name: str, parent: str,
                    support: list[dict[str, Any]]) -> dict[str, Any]:
        if not name.strip() or not parent.strip():
            raise Refused("Refused: a parent mapping names the entity and its parent.")
        receipts = {s.get("receipt_id") for s in support if isinstance(s, dict)}
        if len(support) < 2 or len(receipts) < 2:
            raise Refused("Refused: mapping a name to a parent needs two sources.")
        for n, s in enumerate(support, start=1):
            try:
                ok = self.evidence.check_quote(self.run_dir, str(s.get("receipt_id")),
                                               str(s.get("quote", "")))
            except QuoteCheckFailed as exc:
                raise Refused(f"Refused: support {n}: {exc}.") from exc
            if not ok:
                raise Refused(f"Refused: support {n}: the quotation is not in its receipt.")
        source = self._src(caller)
        child = self.wm.upsertEntity({"type": "org", "name": name, "source": source})
        par = self.wm.upsertEntity({"type": "org", "name": parent, "source": source})
        edge = self.wm.relate({"from": child["id"], "type": "parent", "to": par["id"],
                               "validFrom": source["at"], "source": source,
                               "attrs": {"support": support}})
        return {"relationship": edge}

    # --------------------------------------------------------------------- tree
    def tree(self) -> list[dict[str, Any]]:
        self.wm.refresh()
        nodes = [dict(e["attrs"]) for e in self.wm.view("graph").query({"type": "node"})]
        return sorted(nodes, key=lambda n: str(n["id"]))

    def tree_view(self) -> list[dict[str, Any]]:
        """The tree as the gate sees it: each node with how it closes and whether it has.
        A node that closes by its children shows the derived status, with the stored one
        beside it as `stored_status`."""
        nodes = self.tree()
        info = closure(nodes)
        out = []
        for n in nodes:
            c = info[str(n["id"])]
            view = {**n, "closes_by": c["closes_by"], "closed": c["closed"]}
            if c.get("set_aside_by"):
                view["set_aside_by"] = c["set_aside_by"]
            if c["closes_by"] != "branch":
                view["stored_status"], view["status"] = n["status"], c["status"]
            out.append(view)
        return out

    def _write_tree(self) -> None:
        nodes = self.tree_view()
        data = {"nodes": nodes, "log_tx": len(self.wm.audit())}
        self._write("tree.json", json.dumps(data, indent=2, ensure_ascii=False))
        lines = ["# Issue tree (rendered from tree.json)", ""]
        children: dict[str | None, list[dict[str, Any]]] = {}
        for n in nodes:
            children.setdefault(n.get("parent"), []).append(n)

        def walk(parent: str | None, depth: int) -> None:
            for n in children.get(parent, []):
                crux = " **crux**" if n.get("crux") else ""
                aside = f" (set aside: {n['set_aside']})" if n.get("set_aside") else ""
                lines.append(f"{'  ' * depth}- `{n['id']}` [{n['kind']}, {n['status']}]{crux} "
                             f"{n['statement']}{aside}")
                walk(str(n["id"]), depth + 1)

        walk(None, 0)
        self._write("tree.md", "\n".join(lines) + "\n")

    def _put_node(self, node: dict[str, Any], source: dict[str, Any]) -> None:
        self.wm.upsertEntity({"type": "node", "externalKey": {"system": SYSTEM,
                                                              "id": str(node["id"])},
                              "attrs": node, "source": source})

    def tree_upsert(self, caller: Caller, node: dict[str, Any]) -> dict[str, Any]:
        """Create or change one node. `set_aside` (D5, lead only) is the reason the node
        is set aside, or null to bring it back; a set-aside node and everything under it
        counts as closed, and the reason is shown in the tree and the run's record."""
        self._require(caller, roles.AUTHORS, "change the tree")
        bad = sorted(set(node) - NODE_FIELDS)
        if bad:
            raise Refused(f"Refused: unknown node field {bad[0]}.")
        node = dict(node)
        nid = node.get("id")
        if not isinstance(nid, str) or not _SAFE_ID.fullmatch(nid):
            raise Refused("Refused: a node needs a plain id.")
        if "set_aside" in node:
            self._require(caller, frozenset({roles.LEAD}), "set nodes aside")
            reason = node["set_aside"]
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise Refused("Refused: set_aside is the reason, as text, or null.")
            if isinstance(reason, str) and len(reason) > SET_ASIDE_MAX_CHARS:
                raise Refused(f"Refused: the set-aside reason is longer than "
                              f"{SET_ASIDE_MAX_CHARS} characters.")
            node["set_aside"] = reason.strip() if isinstance(reason, str) else None
        if node.get("status") == "set_aside":
            raise Refused("Refused: a node is set aside by giving set_aside its reason.")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            self._sync_records()
            existing = {n["id"]: n for n in self.tree()}
            merged: dict[str, Any] = dict(existing.get(nid) or {
                "id": nid, "parent": None, "doubt": None, "settles_by": None,
                "flip_point": None, "crux": False, "branch_id": None, "status": "open",
                "set_aside": None})
            merged.setdefault("set_aside", None)
            merged.update(node)
            if merged.get("kind") not in NODE_KINDS:
                raise Refused(f"Refused: kind must be one of {', '.join(NODE_KINDS)}.")
            if not isinstance(merged.get("statement"), str) or not merged["statement"].strip():
                raise Refused("Refused: a node needs a statement.")
            parent = merged.get("parent")
            if parent is not None and (parent not in existing or parent == nid):
                raise Refused(f"Refused: parent {parent} is not a node.")
            up, hops = parent, 0
            while up is not None and hops <= len(existing):
                if up == nid:
                    raise Refused(f"Refused: parent {parent} is under node {nid}; a node "
                                  f"cannot be placed under its own descendant.")
                up, hops = (existing.get(up) or {}).get("parent"), hops + 1
            if not isinstance(merged.get("crux"), bool):
                raise Refused("Refused: crux is true or false.")
            branch = merged.get("branch_id")
            if branch is not None and (not isinstance(branch, str)
                                       or not _SAFE_ID.fullmatch(branch)):
                raise Refused("Refused: branch_id is not a plain id.")
            old = existing.get(nid) or {}
            old_status = old.get("status")
            if ("status" in node and node["status"] != old_status
                    and node["status"] not in SETTABLE_BY_AUTHORS):
                raise Refused(f"Refused: status {node['status']} is not set by tree_upsert.")
            changed = [k for k in NODE_FIELDS - {"id", "status", "set_aside"}
                       if merged.get(k) != old.get(k)]
            if merged.get("set_aside"):
                if old_status != "set_aside":
                    merged["set_aside_from"] = old_status
                merged["status"] = "set_aside"
            elif old_status == "set_aside":
                # Brought back: it starts again, and only a branch verdict closes it.
                merged["status"] = "in_progress" if merged.get("branch_id") else "open"
                merged.pop("set_aside_from", None)
            elif old and old_status in REVIEWED and changed:
                # A reviewed (or under-review) node that changes is no longer what was
                # reviewed: it reopens, and only a new branch verdict can close it again.
                merged["status"] = "in_progress"
                merged["reopened_from"] = old_status
            self._put_node(merged, self._src(caller))
            if merged.get("set_aside") != old.get("set_aside"):
                self._run_fact(caller, "node_set_aside", {
                    "node": nid, "reason": merged.get("set_aside"), "at": now()})
            self._write_tree()
        return merged

    def _branch_dir(self, branch_id: str) -> PurePosixPath:
        """The branch's folder, relative to the run folder, reached without symlinks."""
        if not isinstance(branch_id, str) or not _SAFE_ID.fullmatch(branch_id):
            raise Refused("Refused: branch_id is not a plain id.")
        root = self.run_dir / "branches"
        hits = [root / branch_id] if (root / branch_id).is_dir() else \
            [p for p in root.glob(f"**/sub/{branch_id}") if p.is_dir()]
        if len(hits) != 1:
            raise Refused(f"Refused: branch {branch_id} has no folder.")
        rel = PurePosixPath(hits[0].relative_to(self.run_dir).as_posix())
        here = self.run_dir
        for part in rel.parts:
            here = here / part
            if here.is_symlink() or not here.is_dir():
                raise Refused(f"Refused: branch {branch_id}'s folder is not a plain folder.")
        return rel

    def _branch_nodes(self, branch_id: str) -> list[dict[str, Any]]:
        """The branch's nodes that are not set aside, nor under a node set aside (they
        need no review)."""
        tree = self.tree()
        info = closure(tree)
        nodes = [n for n in tree if n.get("branch_id") == branch_id
                 and info[str(n["id"])]["closes_by"] not in ("set_aside", "ancestor_set_aside")]
        if not nodes:
            raise Refused(f"Refused: no tree node that is not set aside has branch "
                          f"{branch_id}.")
        return nodes

    def propose_branch_done(self, caller: Caller, branch_id: str) -> dict[str, Any]:
        self._require(caller, roles.AUTHORS, "propose a branch done")
        record = self._branch_dir(branch_id) / "record.md"
        try:
            record_bytes = safeio.read_bytes(self.run_dir, record)
        except FileNotFoundError as exc:
            raise Refused("Refused: the branch has no record.md.") from exc
        except (safeio.UnsafePath, NotADirectoryError) as exc:
            raise Refused("Refused: record.md is not a plain file.") from exc
        headings = [h.strip() for h in re.findall(r"^#{1,6}\s+(.+?)\s*$",
                                                   record_bytes.decode("utf-8"), re.MULTILINE)]
        missing = [h for h in RECORD_HEADINGS if h not in headings]
        if missing:
            raise Refused(f"Refused: record.md lacks the heading {missing[0]!r}.")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            self._sync_records()
            nodes = self._branch_nodes(branch_id)
            orders = "\n".join(t for _n, t in self.revision_orders(branch_id))
            for pt in self.sent_back(branch_id):
                if pt["id"] not in orders:
                    raise Refused(f"Refused: point {pt['id']} was sent back; no revision "
                                  f"order of the branch (revision-<n>.md with the headings "
                                  f"{'; '.join(REVISION_HEADINGS)}) names it.")
            source = self._src(caller)
            prior = checks_api.for_prompt(self.run_dir, "branch-reviewer", f"branch:{branch_id}")
            rnd = len(prior) + 1
            # The reviewer is given this snapshot; its verdict binds to these bytes.
            snapshot = frozen_record(branch_id, rnd)
            try:
                safeio.write_bytes(self.run_dir, snapshot, record_bytes)
            except safeio.UnsafePath as exc:
                raise Refused("Refused: the record snapshot cannot be written safely.") from exc
            self._run_fact(caller, "record_frozen", {"branch_id": branch_id, "round": rnd,
                                                     "sha256": sha256_bytes(record_bytes),
                                                     "snapshot": snapshot})
            for n in nodes:
                self._put_node({**n, "status": "proposed_done"}, source)
            self._write_tree()
            req = checks_api.request_check(self.run_dir, roles.BRANCH_REVIEWER,
                                           f"branch:{branch_id}", "propose_branch_done",
                                           round_no=rnd)
        return {"branch_id": branch_id, "round": rnd, "record_digest": sha256_bytes(record_bytes),
                "record_snapshot": snapshot, "check_requested": req.request_id}

    def _frozen_records(self, branch_id: str) -> dict[int, dict[str, Any]]:
        return {int(r["round"]): r for r in self._run_facts("record_frozen")
                if r["branch_id"] == branch_id}

    def _sync_records(self) -> None:
        """A reviewed branch whose live record.md is no longer the reviewed snapshot
        reopens: the review no longer covers what the record says. Call under the lock."""
        source = {"kind": "register", "at": now(), "by": "stemma:register"}
        changed = False
        for n in self.tree():
            branch = n.get("branch_id")
            if n["status"] not in REVIEWED or not branch:
                continue
            frozen = self._frozen_records(branch)
            if not frozen:
                continue
            want = frozen[max(frozen)]["sha256"]
            try:
                live = sha256_bytes(safeio.read_bytes(self.run_dir,
                                                      self._branch_dir(branch) / "record.md"))
            except (FileNotFoundError, safeio.UnsafePath, NotADirectoryError, Refused):
                live = None
            if live != want:
                self._put_node({**n, "status": "in_progress", "reopened_from": n["status"]},
                               source)
                changed = True
        if changed:
            self._write_tree()

    def record_branch_verdict(self, caller: Caller, branch_id: str, verdict: str,
                              reason: str, method: str | None = None,
                              gap_type: str | None = None,
                              points: list[dict[str, Any]] | None = None,
                              resolutions: list[dict[str, Any]] | None = None
                              ) -> dict[str, Any]:
        """A branch reviewer's verdict on its round's record snapshot (D9): a send-back
        (find_more, analyse, bound) carries at least one material point and happens once
        per branch; a closing verdict (answered, accept_gap) gives a resolution for each
        point sent back earlier."""
        self._require(caller, frozenset({roles.BRANCH_REVIEWER}), "record branch verdicts")
        if verdict not in BRANCH_VERDICTS:
            raise Refused(f"Refused: verdict must be one of {', '.join(BRANCH_VERDICTS)}.")
        if not isinstance(reason, str) or not reason.strip():
            raise Refused("Refused: a branch verdict carries its reason.")
        if verdict == "find_more" and not (isinstance(method, str) and method.strip()):
            raise Refused("Refused: find_more names the method.")
        if (verdict == "accept_gap") != (gap_type is not None):
            raise Refused("Refused: accept_gap names its gap type, and only accept_gap does.")
        if gap_type is not None and gap_type not in GAP_TYPES:
            raise Refused(f"Refused: gap type must be one of {', '.join(GAP_TYPES)}.")
        if gap_type == "user_held" and not self.assumptions(branch_id):
            raise Refused("Refused: user_held needs an assumption recorded for the branch.")
        checked = self._check_points(points or [])
        if verdict in SEND_BACK and not any(pt["material"] for pt in checked):
            raise Refused(f"Refused: {verdict} sends the branch back, which needs at least "
                          f"one material point.")
        res_in = self._check_resolutions(resolutions or [])
        branch_dir = self._branch_dir(branch_id)
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            # Authorization and write under one lock: no new round can start in between.
            self._session_prompt_is(caller, "branch-reviewer", f"branch:{branch_id}")
            state = checks_api.state_for_session(self.run_dir, caller.session_id)
            rnd = state.request.round if state else 0
            nodes = self._branch_nodes(branch_id)
            if any(n["status"] != "proposed_done" for n in nodes):
                raise Refused("Refused: the branch is not proposed done.")
            frozen = self._frozen_records(branch_id).get(rnd)
            if frozen is None:
                raise Refused("Refused: no record snapshot exists for this round.")
            snap = safeio.read_bytes(self.run_dir, frozen["snapshot"])
            if sha256_bytes(snap) != frozen["sha256"]:
                raise Refused("Refused: the record snapshot does not match its digest.")
            sent = self.sent_back(branch_id)
            if verdict in SEND_BACK and sent:
                raise Refused(f"Refused: branch {branch_id} was sent back in round "
                              f"{sent[0]['round']}; a branch is sent back once (D9). The "
                              f"verdicts left are answered, accept_gap and ask_user, each "
                              f"with a resolution for every point sent back.")
            if verdict in CLOSING:
                self._require_resolutions(branch_id, sent, res_in, snap.decode("utf-8",
                                                                             "replace"))
            elif res_in:
                known = {pt["id"] for pt in sent}
                for r in res_in:
                    if r["point_id"] not in known:
                        raise Refused(f"Refused: no point {r['point_id']} was sent back on "
                                      f"branch {branch_id}.")
            pts = [{"id": f"{branch_id}.r{rnd}.{i}", **pt}
                   for i, pt in enumerate(checked, start=1)]
            body = {"branch_id": branch_id, "round": rnd, "verdict": verdict, "reason": reason,
                    "method": method, "gap_type": gap_type, "points": pts,
                    "resolutions": res_in, "record_digest": frozen["sha256"],
                    "record_snapshot": frozen["snapshot"], "by": caller.agent_type,
                    "session": caller.session_id, "at": now()}
            source = self._src(caller, kind="branch-reviewer")
            for n in nodes:
                self.wm.assertFact({"about": node_entity(n["id"]), "predicate": "branch_verdict",
                                    "value": body, "source": source})
                self._put_node({**n, "status": BRANCH_VERDICTS[verdict]}, source)
            self._run_fact(caller, "branch_verdict_recorded", body)
            vdir = branch_dir / "verdicts"
            number = len(self._verdict_files(branch_dir)) + 1
            self._write(vdir / f"{number}.json", json.dumps(body, indent=2))
            self._write_tree()
        return {**body, "verdict_file": str(vdir / f"{number}.json")}

    # ------------------------------------------------- review points (D4, D9)
    def criteria(self) -> dict[str, str]:
        """The run's criteria by id (criteria.md); refuses when the file is malformed."""
        try:
            return criteria_mod.read(self.run_dir)
        except criteria_mod.CriteriaInvalid as exc:
            raise Refused(f"Refused: {exc}.") from exc
        except (safeio.UnsafePath, NotADirectoryError) as exc:
            raise Refused("Refused: criteria.md is not a plain file.") from exc

    def _check_points(self, points: Any) -> list[dict[str, Any]]:
        """Each point is {point, material, node, criterion}: node a tree node id or null,
        criterion a criterion id or null, at least one of them set. The reviewer judges
        materiality; code checks only that the names exist."""
        if not isinstance(points, list):
            raise Refused("Refused: points is a list of {point, material, node, criterion}.")
        if not points:
            return []
        nodes = {str(n["id"]) for n in self.tree()}
        crit = self.criteria()
        out: list[dict[str, Any]] = []
        for i, pt in enumerate(points, start=1):
            if (not isinstance(pt, dict) or not {"point", "material"} <= set(pt)
                    or not set(pt) <= POINT_FIELDS
                    or not isinstance(pt["point"], str) or not pt["point"].strip()
                    or not isinstance(pt["material"], bool)):
                raise Refused(f"Refused: point {i} is not {{point: text, material: true or "
                              f"false, node: node id or null, criterion: C-id or null}}.")
            node, cid = pt.get("node"), pt.get("criterion")
            if node is None and cid is None:
                raise Refused(f"Refused: point {i} names neither a tree node nor a criterion.")
            if node is not None and (not isinstance(node, str) or node not in nodes):
                raise Refused(f"Refused: point {i} names node {node!r}, which is not in the tree.")
            if cid is not None and (not isinstance(cid, str) or cid not in crit):
                raise Refused(f"Refused: point {i} names criterion {cid!r}, which is not in "
                              f"criteria.md.")
            out.append({"point": pt["point"], "material": pt["material"], "node": node,
                        "criterion": cid})
        return out

    @staticmethod
    def _check_resolutions(resolutions: Any) -> list[dict[str, Any]]:
        if not isinstance(resolutions, list):
            raise Refused("Refused: resolutions is a list of {point_id, status, note}.")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, r in enumerate(resolutions, start=1):
            if (not isinstance(r, dict) or not {"point_id", "status"} <= set(r)
                    or not set(r) <= {"point_id", "status", "note"}
                    or not isinstance(r["point_id"], str)
                    or r["status"] not in RESOLUTIONS
                    or not isinstance(r.get("note", ""), str)):
                raise Refused(f"Refused: resolution {i} is not {{point_id, status: "
                              f"{' | '.join(RESOLUTIONS)}, note}}.")
            if r["point_id"] in seen:
                raise Refused(f"Refused: point {r['point_id']} is resolved twice.")
            seen.add(r["point_id"])
            out.append({"point_id": r["point_id"], "status": r["status"],
                        "note": r.get("note", "")})
        return out

    def sent_back(self, branch_id: str) -> list[dict[str, Any]]:
        """The material points sent back on a branch (by a session not revoked), each with
        its id and round; empty when the branch was never sent back."""
        out: list[dict[str, Any]] = []
        for v in self._run_facts("branch_verdict_recorded"):
            if v["branch_id"] == branch_id and v["verdict"] in SEND_BACK:
                out += [{**pt, "round": v["round"]} for pt in v["points"] if pt["material"]]
        return out

    def _require_resolutions(self, branch_id: str, sent: list[dict[str, Any]],
                             given: list[dict[str, Any]], record: str) -> None:
        """A closing verdict accounts for every point sent back: `bounded` or
        `conditional` needs the record snapshot to name the point's id; `disputed` needs
        a revision order of the branch that names it (exact facts; the judgment of
        whether the record really bounds it is the reviewer's)."""
        by_id = {r["point_id"]: r for r in given}
        want = [pt["id"] for pt in sent]
        for pid in by_id:
            if pid not in want:
                raise Refused(f"Refused: no point {pid} was sent back on branch {branch_id}.")
        for pid in want:
            r = by_id.get(pid)
            if r is None:
                raise Refused(f"Refused: point {pid} was sent back on this branch; a closing "
                              f"verdict gives its resolution ({', '.join(RESOLUTIONS)}).")
            if r["status"] in ("bounded", "conditional") and pid not in record:
                raise Refused(f"Refused: point {pid} is {r['status']}, but the record does "
                              f"not name it.")
            if r["status"] == "disputed" and not any(
                    pid in text for _n, text in self.revision_orders(branch_id)):
                raise Refused(f"Refused: point {pid} is disputed, but no revision order of "
                              f"branch {branch_id} names it.")

    def revision_orders(self, branch_id: str) -> list[tuple[int, str]]:
        """The branch's revision orders (`revision-<n>.md`, D6) with all four headings."""
        bdir = self.run_dir / self._branch_dir(branch_id)
        out: list[tuple[int, str]] = []
        for p in sorted(bdir.glob("revision-*.md")):
            m = re.fullmatch(r"revision-(\d+)\.md", p.name)
            if not m:
                continue
            try:
                text = safeio.read_text(self.run_dir,
                                        self._branch_dir(branch_id) / p.name)
            except (OSError, UnicodeDecodeError, safeio.UnsafePath):
                continue
            heads = [h.strip() for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)]
            if all(h in heads for h in REVISION_HEADINGS):
                out.append((int(m.group(1)), text))
        return sorted(out)

    def _verdict_files(self, branch_dir: PurePosixPath) -> list[PurePosixPath]:
        vdir = self.run_dir / branch_dir / "verdicts"
        if vdir.is_symlink() or not vdir.is_dir():
            return []
        names = sorted((p for p in vdir.glob("*.json") if p.stem.isdigit()),
                       key=lambda p: int(p.stem))
        return [branch_dir / "verdicts" / p.name for p in names]

    def revoke_session(self, session_id: str, reason: str) -> None:
        """Host only (the driver): a checking session ran on the wrong model or effort.
        Its verdicts, frames and reviews stop counting, it can record nothing more, and a
        branch it answered goes back to proposed_done for the relaunched check."""
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            checks_api.revoke(self.run_dir, session_id, reason)
            source = {"kind": "register", "at": now(), "by": "stemma:register"}
            for fin in self._run_facts("finished"):
                if session_id in fin.get("sessions", []):
                    # The release rested on this session: it no longer stands.
                    self._ensure_entity(RUN_ENTITY, "run", "run", source)
                    self.wm.assertFact({"about": RUN_ENTITY, "predicate": "release_withdrawn",
                                        "value": {"session": session_id, "reason": reason,
                                                  "at": source["at"]}, "source": source})
            for n in self.tree():
                facts = self.wm.view("facts").about(node_entity(n["id"]),
                                                    {"predicate": "branch_verdict"})
                if (facts and facts[-1]["source"].get("runId") == session_id
                        and n["status"] in set(BRANCH_VERDICTS.values())):
                    self._put_node({**n, "status": "proposed_done"}, source)
            self._write_tree()
            self.rebuild_index()

    def release_state(self) -> str | None:
        """`released`, `withdrawn` (a release rested on a session since revoked, or a
        release block was recorded), or None when nothing was released."""
        self.wm.refresh()
        if not self._run_facts("finished"):
            return None
        if self._run_facts("release_withdrawn") or checks_api.release_blocks(self.run_dir):
            return "withdrawn"
        return "released"

    def assumptions(self, branch_id: str | None = None) -> list[dict[str, Any]]:
        """Assumptions taken in place of a user's answer, oldest first."""
        self.wm.refresh()
        return [a for a in self._run_facts("assumption")
                if branch_id is None or a["branch_id"] == branch_id]

    def proceed_on_assumption(self, caller: Caller, branch_id: str,
                              assumption: str) -> dict[str, Any]:
        """A branch waiting on the user goes back to work on a stated assumption."""
        self._require(caller, frozenset({roles.LEAD}), "proceed on an assumption")
        if not isinstance(assumption, str) or not assumption.strip():
            raise Refused("Refused: state the assumption.")
        branch_dir = self._branch_dir(branch_id)
        with jsonl.exclusive(self._lock):
            nodes = self._branch_nodes(branch_id)
            if any(n["status"] != "waiting_user" for n in nodes):
                raise Refused("Refused: the branch is not waiting on the user.")
            question = None
            files = self._verdict_files(branch_dir)
            if files:
                question = json.loads(self._read(files[-1])).get("reason")
            source = self._src(caller)
            record = {"branch_id": branch_id, "question": question,
                      "assumption": assumption.strip(), "at": source["at"]}
            self._run_fact(caller, "assumption", record)
            for n in nodes:
                self._put_node({**n, "status": "in_progress"}, source)
            self._write_tree()
        return record

    # ---------------------------------------------------- frame, drafts, finish
    def _run_facts(self, predicate: str) -> list[dict[str, Any]]:
        if self.wm.view("graph").entity(RUN_ENTITY) is None:
            return []
        return [f["value"] for f in self._live_facts(RUN_ENTITY, predicate)]

    def _run_fact(self, caller: Caller, predicate: str, value: Any) -> None:
        source = self._src(caller)
        self._ensure_entity(RUN_ENTITY, "run", "run", source)
        self.wm.assertFact({"about": RUN_ENTITY, "predicate": predicate, "value": value,
                            "source": source})

    def request_frame_critique(self, caller: Caller) -> dict[str, Any]:
        self._require(caller, frozenset({roles.LEAD}), "ask for the frame critique")
        frame = self._read("frame.md")
        if not self._recorded_frames("own"):
            raise Refused("Refused: the blind framer's own frame has not landed.")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            prior = checks_api.for_prompt(self.run_dir, "blind-framer", "frame:critique")
            req = checks_api.request_check(self.run_dir, roles.BLIND_FRAMER, "frame:critique",
                                           "request_frame_critique", round_no=len(prior) + 1)
            self._run_fact(caller, "frame_critique_requested",
                           {"frame_digest": sha256_bytes(frame),
                            "round": req.round})
        return {"check_requested": req.request_id, "round": req.round}

    def max_review_rounds(self) -> int:
        """`limits.max_review_rounds` from run.json; the default when run.json or the key
        is absent. A value that is not a positive whole number is a loud refusal."""
        try:
            run = json.loads(safeio.read_text(self.run_dir, "run.json"))
        except FileNotFoundError:
            return DEFAULT_MAX_REVIEW_ROUNDS
        except (OSError, ValueError, safeio.UnsafePath) as exc:
            raise Refused(f"Refused: run.json cannot be read ({type(exc).__name__}).") from exc
        limits = run.get("limits") if isinstance(run, dict) else None
        if not isinstance(limits, dict) or "max_review_rounds" not in limits:
            return DEFAULT_MAX_REVIEW_ROUNDS
        n = limits["max_review_rounds"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise Refused("Refused: run.json max_review_rounds is not a positive whole number.")
        return n

    def review_rounds_exhausted(self) -> bool:
        return len(self.submitted_versions()) >= self.max_review_rounds()

    def _completion_statement(self, version: int) -> str:
        """When the review rounds ran out: the last permitted round's criticisms, which no
        further round could resolve, as the reviewers recorded them, material points first
        (code selects nothing; the reviewers judged materiality)."""
        limit = self.max_review_rounds()
        have = self._recorded_reviews(version)
        parts = ["# Completion statement", "",
                 (f"Review rounds stopped by limit (stopped_by_limit): {limit} of {limit} "
                  f"rounds (max_review_rounds in run.json, a resource limit, not a quality "
                  f"verdict). Released: version {version}, the last round."), "",
                 "## Material points the last round raised, unresolved", ""]
        material = [(roles.short(role), pt["point"]) for role in roles.FINAL_REVIEWERS
                    for pt in have[role].get("points", []) if pt["material"]]
        parts += [f"- {who}: {point}" for who, point in material] or ["- none"]
        parts += ["", f"## The reviews of version {version}", ""]
        for role in roles.FINAL_REVIEWERS:
            body = safeio.read_text(self.run_dir,
                                    f"reviews/v{version}/{REVIEW_FILE_OF[role]}").strip()
            parts += [f"### {roles.short(role)}", "", body, ""]
        return "\n".join(parts)

    def submitted_versions(self) -> list[int]:
        self.wm.refresh()
        return [int(v["version"]) for v in self._run_facts("draft_submitted")]

    def submit_draft(self, caller: Caller, version: int) -> dict[str, Any]:
        self._require(caller, frozenset({roles.LEAD}), "submit drafts")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise Refused("Refused: version is a positive whole number.")
        data = self._read(f"draft/answer.v{version}.md")
        with jsonl.exclusive(self._lock):
            done = self.submitted_versions()
            if done and version <= max(done):
                raise Refused(f"Refused: version {version} is not after version {max(done)}.")
            limit = self.max_review_rounds()
            if len(done) >= limit:
                raise Refused(f"Refused: the review-round limit is reached ({limit} rounds, "
                              f"max_review_rounds in run.json, a resource limit). finish_run "
                              f"releases a reviewed version, listing the last round's "
                              f"criticisms.")
            try:
                safeio.write_bytes(self.run_dir, frozen_draft(version), data)
            except safeio.UnsafePath as exc:
                raise Refused("Refused: the frozen draft cannot be written safely.") from exc
            self._run_fact(caller, "draft_submitted",
                           {"version": version, "sha256": sha256_bytes(data)})
            reqs = [checks_api.request_check(self.run_dir, role, f"draft:{version}",
                                             "submit_draft").request_id
                    for role in roles.FINAL_REVIEWERS]
        return {"version": version, "checks_requested": reqs}

    # ------------------------------------------- frames and reviews (tool-written)
    def _recorded_frames(self, kind: str) -> list[dict[str, Any]]:
        self.wm.refresh()
        return [f for f in self._run_facts("frame_recorded") if f["kind"] == kind]

    def _recorded_reviews(self, version: int) -> dict[str, dict[str, Any]]:
        self.wm.refresh()
        return {r["role"]: r for r in self._run_facts("review_recorded")
                if r["version"] == version}

    def record_frame(self, caller: Caller, kind: str, text: str) -> dict[str, Any]:
        """Blind framer only: its own frame (`own`) or its critique (`critique`), from the
        session the driver launched for frame:<kind>, latest round."""
        self._require(caller, frozenset({roles.BLIND_FRAMER}), "record frames")
        if kind not in FRAME_FILES:
            raise Refused("Refused: kind is own or critique.")
        if not isinstance(text, str) or not text.strip():
            raise Refused("Refused: the frame text is empty.")
        with jsonl.exclusive(self._lock):
            self._session_prompt_is(caller, "blind-framer", f"frame:{kind}")
            if kind == "critique" and not self._recorded_frames("own"):
                raise Refused("Refused: the own frame comes before the critique.")
            rnd = len(self._recorded_frames(kind)) + 1
            self._write(FRAME_FILES[kind], text)
            self._write(f"register/frames/{kind}.r{rnd}.md", text)
            record = {"kind": kind, "round": rnd, "sha256": sha256_bytes(text.encode("utf-8")),
                      "session": caller.session_id}
            self._run_fact(caller, "frame_recorded", record)
        return {**record, "file": FRAME_FILES[kind]}

    def record_review(self, caller: Caller, version: int, text: str,
                      points: list[dict[str, Any]]) -> dict[str, Any]:
        """A final reviewer only: its review of draft <version>, from the session the
        driver launched for draft:<version>, with its points, each {point, material, node,
        criterion} (D4): the reviewer judges materiality, code only reads it and checks
        that the node and criterion it names exist (CONTRACTS v0.4 section 6)."""
        self._require(caller, frozenset(roles.FINAL_REVIEWERS), "record reviews")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise Refused("Refused: version is a positive whole number.")
        if not isinstance(text, str) or not text.strip():
            raise Refused("Refused: the review text is empty.")
        checked = self._check_points(points)
        rel = f"reviews/v{version}/{REVIEW_FILE_OF[caller.agent_type]}"
        with jsonl.exclusive(self._lock):
            self._session_prompt_is(caller, roles.short(caller.agent_type), f"draft:{version}")
            if caller.agent_type in self._recorded_reviews(version):
                raise Refused("Refused: this review is already recorded.")
            self._write(rel, text)
            abbr = REVIEW_ABBR[caller.agent_type]
            record = {"version": version, "role": caller.agent_type,
                      "sha256": sha256_bytes(text.encode("utf-8")), "session": caller.session_id,
                      "points": [{"id": f"v{version}.{abbr}.{i}", **pt}
                                 for i, pt in enumerate(checked, start=1)]}
            self._run_fact(caller, "review_recorded", record)
        return {**record, "file": rel}

    # ------------------------------------------------------------- release gate
    def finish_failures(self, version: int | None = None,
                        text: str | None = None) -> list[str]:
        """Every unmet condition of the release gate (CONTRACTS section 6) for releasing
        the frozen draft `version` (default: the latest submitted). `text` is that
        draft's text when the caller has already read it."""
        failures: list[str] = []
        verified = self._verified()
        relied: set[str] = set()  # the checking sessions this release would rest on

        def rests_on(session: str | None, what: str) -> None:
            relied.add(str(session))
            if session not in verified:
                failures.append(f"{what} comes from a session whose model and effort "
                                f"are not verified yet")

        for reason in checks_api.release_blocks(self.run_dir):
            failures.append(f"the release is blocked: {reason}")
        try:
            if not self.criteria():
                failures.append("criteria.md names no criterion (- C1: ...)")
        except Refused as exc:
            failures.append(str(exc).removeprefix("Refused: ").rstrip("."))
        nodes = self.tree_view()
        if not nodes:
            failures.append("the tree has no nodes")
        for n in nodes:
            if n["closes_by"] == "unreachable":
                failures.append(f"node {n['id']} is not reachable from a root of the tree "
                                f"(its parents form a cycle or lead to no node)")
                continue
            if n["closes_by"] != "branch":
                continue  # set aside, an empty open lane, or a node its children close
            if n["status"] not in DONE:
                where = (f"branch {n['branch_id']}" if n.get("branch_id")
                         else "in no branch; only a branch verdict closes it")
                failures.append(f"node {n['id']} is {n['status']} ({n['kind']}, {where})")
            elif n["status"] in ("answered", "gap_accepted"):
                facts = self._live_facts(node_entity(n["id"]), "branch_verdict")
                if not facts:
                    failures.append(f"node {n['id']} has no branch verdict")
                else:
                    rests_on(facts[-1]["source"].get("runId"),
                             f"the branch verdict for node {n['id']}")
                    resolved = {r["point_id"] for r in facts[-1]["value"].get("resolutions")
                                or []}
                    for pt in self.sent_back(str(n.get("branch_id"))):
                        if pt["id"] not in resolved:
                            failures.append(f"node {n['id']}: point {pt['id']} sent back on "
                                            f"branch {n['branch_id']} has no resolution")
        critiques = self._recorded_frames("critique")
        if not critiques:
            failures.append("the blind framer's critique is not recorded")
        else:
            rests_on(critiques[-1]["session"], "the frame critique")
        if not safeio.is_file(self.run_dir, "framer/lead-response.md"):
            failures.append("framer/lead-response.md does not exist")
        versions = self.submitted_versions()
        if version is None:
            version = max(versions) if versions else None
        if version is None:
            failures.append("no draft was submitted")
            return failures
        if version not in versions:
            failures.append(f"version {version} was not submitted")
            return failures
        at_limit = len(versions) >= self.max_review_rounds()
        if at_limit and version != max(versions):
            failures.append(f"the review-round limit is reached: only the latest submitted "
                            f"version, {max(versions)}, may be released")
        have = self._recorded_reviews(version)
        for role in roles.FINAL_REVIEWERS:
            if role not in have:
                failures.append(f"the {roles.short(role)} review of version {version} "
                                f"is not recorded")
                continue
            rests_on(have[role]["session"], f"the {roles.short(role)} review")
            if not at_limit:
                for pt in have[role].get("points", []):
                    if pt["material"]:
                        failures.append(f"the {roles.short(role)} review of version {version} "
                                        f"raises a material point {pt.get('id', '')} "
                                        f"({_concerns(pt)}): {pt['point'][:300]}")
        if text is None:
            text = safeio.read_text(self.run_dir, frozen_draft(version))
        for a in self.assumptions():
            if a["assumption"] not in text:
                failures.append(f"the deliverable does not list the assumption taken for "
                                f"branch {a['branch_id']}")
        for line, raw in tags.malformed_tags(text):
            failures.append(f"deliverable line {line}: malformed tag {raw!r}")
        for seg in tags.untagged_number_segments(text):
            failures.append(f"deliverable line {seg.line}: untagged number in {seg.text[:60]!r}")
        for short in dict.fromkeys(tags.claim_tags(text)):
            try:
                rec = self.get_claim(short)
            except Refused:
                failures.append(f"tag c:{short} names no claim")
                continue
            if rec["superseded_by"]:
                failures.append(f"claim {short} is superseded")
            verdict = (rec["verdict"] or {}).get("verdict")
            if verdict not in RELEASABLE:
                failures.append(f"claim {short} has verdict {verdict or 'none'}")
            else:
                rests_on(rec["verdict_session"], f"the verdict on claim {short}")
        self._relied = sorted(relied)
        return failures

    def gate_status(self, caller: Caller, version: int) -> dict[str, Any]:
        """Read the release gate for `version` without releasing: every unmet condition,
        the same list finish_run refuses with (CONTRACTS section 6, v0.3.1)."""
        self._require(caller, frozenset({roles.LEAD}), "read the release gate")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise Refused("Refused: version is a positive whole number.")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            self._sync_records()
            unmet = self.finish_failures(version)
            points = self.open_material_points(version)
            aside = self.set_aside_nodes()
        return {"version": version, "releasable": not unmet, "unmet_count": len(unmet),
                "unmet": "\n".join(f"{i}. {f}" for i, f in enumerate(unmet, start=1)),
                "open_material_points": points, "set_aside": aside}

    def open_material_points(self, version: int) -> dict[str, Any]:
        """The material points the final reviews of `version` raise, grouped by the
        criterion and by the tree node each concerns (a point naming both is in both)."""
        by_criterion: dict[str, list[dict[str, Any]]] = {}
        by_node: dict[str, list[dict[str, Any]]] = {}
        count = 0
        for role, rec in sorted(self._recorded_reviews(version).items()):
            for pt in rec.get("points", []):
                if not pt["material"]:
                    continue
                count += 1
                item = {"id": pt.get("id"), "by": roles.short(role), "point": pt["point"],
                        "criterion": pt.get("criterion"), "node": pt.get("node")}
                if pt.get("criterion"):
                    by_criterion.setdefault(str(pt["criterion"]), []).append(item)
                if pt.get("node"):
                    by_node.setdefault(str(pt["node"]), []).append(item)
        return {"count": count, "by_criterion": by_criterion, "by_node": by_node}

    def set_aside_nodes(self) -> list[dict[str, Any]]:
        """Every node the lead set aside (D5), with its reason; nodes under one are listed
        with `under` naming it."""
        out = []
        for n in self.tree_view():
            if n.get("closes_by") == "set_aside":
                out.append({"id": n["id"], "statement": n["statement"],
                            "reason": n.get("set_aside")})
            elif n.get("closes_by") == "ancestor_set_aside":
                out.append({"id": n["id"], "statement": n["statement"],
                            "under": n.get("set_aside_by")})
        return out

    def frame_failures(self) -> list[str]:
        """What a frame-only run lacks before it ends (CONTRACTS v0.4 section 9): intake.md,
        criteria.md with a criterion, frame.md, a tree, the blind framer's critique, and
        framer/lead-response.md written after it. Exact facts only."""
        self.wm.refresh()
        missing = [f"{rel} does not exist" for rel in ("intake.md", "frame.md")
                   if not safeio.is_file(self.run_dir, rel)]
        try:
            if not self.criteria():
                missing.append("criteria.md names no criterion")
        except Refused as exc:
            missing.append(str(exc).removeprefix("Refused: ").rstrip("."))
        if not self.tree():
            missing.append("the tree has no nodes")
        if not self._recorded_frames("critique"):
            missing.append("the blind framer's critique is not recorded")
        elif not safeio.is_file(self.run_dir, "framer/lead-response.md"):
            missing.append("framer/lead-response.md does not exist")
        else:
            crit = os.stat(self.run_dir / "framer" / "critique.md", follow_symlinks=False)
            resp = os.stat(self.run_dir / "framer" / "lead-response.md", follow_symlinks=False)
            if resp.st_mtime_ns < crit.st_mtime_ns:
                missing.append("framer/lead-response.md predates the latest critique")
        return missing

    def finish_run(self, caller: Caller, version: int) -> dict[str, Any]:
        """Release the frozen, reviewed draft `version` as the deliverable. The bytes read
        once here are the bytes the gate checks and the bytes released and rendered."""
        self._require(caller, frozenset({roles.LEAD}), "finish the run")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise Refused("Refused: version is a positive whole number.")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            self._sync_records()
            if version not in self.submitted_versions():
                raise Refused(gate_refusal(self.finish_failures(version)))
            try:
                data = safeio.read_bytes(self.run_dir, frozen_draft(version))
            except (FileNotFoundError, safeio.UnsafePath, NotADirectoryError) as exc:
                raise Refused(f"Refused: finish_run: the frozen draft of version {version} is "
                              f"missing or not a plain file.") from exc
            submitted = next(d for d in self._run_facts("draft_submitted")
                             if int(d["version"]) == version)
            if not data.strip() or sha256_bytes(data) != submitted["sha256"]:
                raise Refused(f"Refused: finish_run: the frozen draft of version {version} is "
                              f"empty or not what was submitted.")
            text = data.decode("utf-8")
            failures = self.finish_failures(version, text)
            if failures:
                raise Refused(gate_refusal(failures))
            self._write("deliverable/answer.md", text)
            self._write("deliverable/answer.reader.md", self._render_reader(text))
            extra: dict[str, Any] = {}
            if self.review_rounds_exhausted():
                self._write("deliverable/completion.md", self._completion_statement(version))
                self._run_fact(caller, "stopped_by_limit",
                               {"what": "review_rounds", "limit": self.max_review_rounds(),
                                "version": version})
                extra = {"completion": "deliverable/completion.md",
                         "stopped_by_limit": ["review_rounds"]}
            self._run_fact(caller, "finished", {"at": now(), "version": version,
                                                "sha256": sha256_bytes(data),
                                                "sessions": self._relied, **extra})
            req = checks_api.request_check(self.run_dir, roles.RETRO_WRITER, "retro:run",
                                           "finish_run")
        return {"finished": True, "version": version, "deliverable": "deliverable/answer.md",
                "reader": "deliverable/answer.reader.md", "check_requested": req.request_id,
                **extra}

    def _render_reader(self, text: str) -> str:
        order: dict[str, int] = {}
        refs: list[str] = []
        unchecked: set[str] = set()

        def ref(m: re.Match[str]) -> str:
            short = m.group(1)
            if short not in order:
                order[short] = len(order) + 1
                rec = self.get_claim(short)
                if (rec["verdict"] or {}).get("verdict") == "could_not_check":
                    unchecked.add(short)
                mark = " (not checked)" if short in unchecked else ""
                receipts = ", ".join(s["receipt_id"] for s in rec["support"]) or rec["tier"]
                refs.append(f"{order[short]}. {rec['statement']} [{rec['tier']}; {receipts}]{mark}")
            return f"[{order[short]}{', not checked' if short in unchecked else ''}]"

        body = tags.C_TAG.sub(ref, text)
        body = re.sub(r"[ \t]?" + tags.NC_TAG.pattern, "", body)
        return body.rstrip() + "\n\n## References\n\n" + "\n".join(refs) + "\n"

    # ------------------------------------------------------------------- await
    def _landed(self, target: str) -> dict[str, Any] | None:
        kind, _, ident = target.partition(":")
        verified = self._verified()
        if kind == "claim":
            rec = self._claim_record(self.resolve_claim_id(ident))
            # A verdict lands once its session's model and effort are verified.
            verdict: dict[str, Any] | None = rec["verdict"] \
                if rec["verdict_session"] in verified else None
            return verdict
        if kind == "branch":
            rounds = checks_api.for_prompt(self.run_dir, "branch-reviewer", target)
            nodes = [n for n in self.tree() if n.get("branch_id") == ident]
            if not rounds or not nodes:
                return None
            latest = max(r.request.round for r in rounds)
            for f in reversed(self._live_facts(node_entity(nodes[0]["id"]), "branch_verdict")):
                if f["value"].get("round") == latest and f["source"].get("runId") in verified:
                    result: dict[str, Any] = f["value"]
                    return result
            return None
        if kind == "draft":
            if not ident.isdigit():
                raise Refused(f"Refused: {target} is not a check id.")
            have = {r: v for r, v in self._recorded_reviews(int(ident)).items()
                    if v["session"] in verified}
            return {"reviews": sorted(have)} if len(have) == len(REVIEW_FILES) else None
        if kind == "frame":
            if ident not in FRAME_FILES:
                raise Refused(f"Refused: {target} is not a check id.")
            got = [f for f in self._recorded_frames(ident) if f["session"] in verified]
            return {"file": FRAME_FILES[ident], "round": got[-1]["round"]} if got else None
        raise Refused(f"Refused: {target} is not a check id.")

    def _normalise_target(self, given: str | int) -> str:
        """`claim:<id>`, `branch:<id>`, `draft:<n>`, `frame:own|critique`; or a bare claim
        id, branch id or draft version."""
        if isinstance(given, int) and not isinstance(given, bool):
            return f"draft:{given}"
        g = str(given).strip()
        if g.split(":", 1)[0] in ("claim", "branch", "draft", "frame"):
            if g.startswith("claim:"):
                return "claim:" + self.resolve_claim_id(g)
            return g
        if g.isdigit() and len(g) < 12:
            return f"draft:{int(g)}"
        if g in {n.get("branch_id") for n in self.tree()}:
            return f"branch:{g}"
        return "claim:" + self.resolve_claim_id(g)

    def await_checks(self, ids: list[str | int], max_wait_s: float) -> dict[str, Any]:
        """Verdicts that have landed and checks still running. Never invents one."""
        if not isinstance(ids, list) or not ids:
            raise Refused("Refused: name at least one check id.")
        targets = [self._normalise_target(i) for i in ids]
        deadline = time.monotonic() + max(0.0, min(float(max_wait_s), AWAIT_MAX_S))
        while True:
            self.wm.refresh()
            landed: dict[str, Any] = {}
            waiting: list[dict[str, Any]] = []
            all_states = list(checks_api.states(self.run_dir).values())
            for t in targets:
                got = self._landed(t)
                if got is not None:
                    landed[t] = got
                    continue
                mine = [s for s in all_states if s.request.prompt == t]
                if not mine:
                    waiting.append({"id": t, "status": "not_requested"})
                for s in mine:
                    entry: dict[str, Any] = {"id": t, "role": s.request.role,
                                             "status": s.status}
                    if s.status == "ended":
                        entry["outcome"] = f"ended without a verdict ({s.outcome})"
                    waiting.append(entry)
            # Not yet requested counts as open: the caller asked to wait for it.
            open_ = [w for w in waiting if w["status"] in ("pending", "running", "not_requested")]
            if not open_ or time.monotonic() >= deadline:
                return {"landed": landed, "not_landed": waiting}
            time.sleep(AWAIT_POLL_S)

    # -------------------------------------------------------------------- index
    def rebuild_index(self) -> None:
        """Rebuild `register/index.sqlite` from the log (a derived view, never a source)."""
        path = self.dir / "index.sqlite"
        tmp = self.dir / "index.sqlite.tmp"
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        try:
            con.execute("CREATE TABLE claims (claim_id TEXT PRIMARY KEY, tag TEXT, statement "
                        "TEXT, tier TEXT, branch_id TEXT, author TEXT, verdict TEXT, "
                        "superseded_by TEXT, number REAL, created_at TEXT)")
            for e in self.wm.view("graph").query({"type": "claim"}):
                r = self._claim_record(e["externalKey"]["id"])
                con.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    r["claim_id"], r["tag"], r["statement"], r["tier"], r.get("branch_id"),
                    r["author"], (r["verdict"] or {}).get("verdict"), r["superseded_by"],
                    (r.get("value") or {}).get("number"), r["created_at"]))
            con.commit()
        finally:
            con.close()
        tmp.replace(path)


def frozen_record(branch_id: str, round_no: int) -> str:
    """The record snapshot a branch reviewer is given for a round (tool-only)."""
    return f"register/branches/{branch_id}/record.r{round_no}.md"


def frozen_draft(version: int) -> str:
    """Where submit_draft freezes a draft (tool-only; what reviewers read and release uses)."""
    return f"register/drafts/answer.v{version}.md"


def open_register(run_dir: Path) -> Register:
    return Register(run_dir)
