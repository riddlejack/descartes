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
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.register import checks_api, filing, jsonl, roles, tags
from stemma.register.derivation import RerunResult, rerun
from stemma.register.evidence_port import Evidence, QuoteCheckFailed
from stemma.register.evidence_port import load as load_evidence
from stemma.register.filing import Refused
from stemma.register.wm import WorldModel, WorldModelError, entityIdFromName

SYSTEM = "stemma"
RUN_ENTITY = "run:stemma:run"

VERDICTS = ("holds", "holds_with_correction", "not_supported", "could_not_check")
RELEASABLE = frozenset({"holds", "holds_with_correction", "could_not_check"})
NODE_KINDS = ("option", "condition", "rival", "open_lane")
NODE_FIELDS = frozenset({"id", "parent", "kind", "statement", "doubt", "settles_by",
                         "flip_point", "crux", "branch_id", "status"})
STATUSES = ("open", "in_progress", "proposed_done", "answered", "gap_accepted",
            "waiting_user", "stopped_by_limit")
DONE = frozenset({"answered", "gap_accepted", "stopped_by_limit"})
SETTABLE_BY_AUTHORS = frozenset({"open", "in_progress"})
BRANCH_VERDICTS = {"answered": "answered", "accept_gap": "gap_accepted",
                   "find_more": "in_progress", "analyse": "in_progress",
                   "bound": "in_progress", "ask_user": "waiting_user"}
RECORD_HEADINGS = ("Conclusion and confidence", "Claims (ids)", "Model", "Rival tested",
                   "Methods tried and what each returned", "Gaps (typed)",
                   "Implications for other branches", "What was not checked")
REVIEW_FILES = ("deciders-eye.md", "evidence-audit.md", "mechanical.md")

# Resource limits: how long await_checks may block in one call, and how often it looks.
AWAIT_MAX_S = 300
AWAIT_POLL_S = 2.0

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        state = checks_api.state_for_session(self.run_dir, caller.session_id)
        if state is None or state.request.role != role or state.request.prompt != prompt:
            raise Refused(f"Refused: this session was not started for {prompt}.")

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
        facts = self.wm.view("facts")
        verdicts = facts.about(claim_entity(cid), {"predicate": "verdict"})
        superseded = facts.about(claim_entity(cid), {"predicate": "superseded_by"})
        latest = next((f for f in verdicts if "supersededBy" not in f), None)
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
                                   self._lookup_input)
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
        self._session_prompt_is(caller, "claim-checker", f"claim:{cid}")
        if verdict not in VERDICTS:
            raise Refused(f"Refused: verdict must be one of {', '.join(VERDICTS)}.")
        if not isinstance(evidence, str) or not evidence.strip():
            raise Refused("Refused: a verdict carries its evidence.")
        if (verdict == "holds_with_correction") != (corrected_claim is not None):
            raise Refused("Refused: a corrected claim comes with holds_with_correction only.")
        corrected = None
        if corrected_claim is not None:
            corrected = filing.run_checks(corrected_claim, self.run_dir, self.evidence,
                                          self._rerun, self._lookup_input)
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            if self._claim_record(cid)["superseded_by"] is not None:
                raise Refused("Refused: the claim is already superseded.")
            source = self._src(caller, kind="claim-checker")
            out: dict[str, Any] = {"claim_id": cid, "verdict": verdict}
            if corrected is not None:
                new_cid, _ = self._store_claim(corrected, caller, cid)
                if new_cid == cid:
                    raise Refused("Refused: the corrected claim is identical to the claim.")
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
        for path in sorted(self.run_dir.rglob("*")):
            rel = path.relative_to(self.run_dir)
            if not path.is_file() or (rel.parts and rel.parts[0] in skip):
                continue
            if path.suffix not in (".md", ".py", ".json", ".txt", ".csv"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
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

    def _write_tree(self) -> None:
        nodes = self.tree()
        data = {"nodes": nodes, "log_tx": len(self.wm.audit())}
        tmp = self.run_dir / "tree.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.run_dir / "tree.json")
        lines = ["# Issue tree (rendered from tree.json)", ""]
        children: dict[str | None, list[dict[str, Any]]] = {}
        for n in nodes:
            children.setdefault(n.get("parent"), []).append(n)

        def walk(parent: str | None, depth: int) -> None:
            for n in children.get(parent, []):
                crux = " **crux**" if n.get("crux") else ""
                lines.append(f"{'  ' * depth}- `{n['id']}` [{n['kind']}, {n['status']}]{crux} "
                             f"{n['statement']}")
                walk(str(n["id"]), depth + 1)

        walk(None, 0)
        (self.run_dir / "tree.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _put_node(self, node: dict[str, Any], source: dict[str, Any]) -> None:
        self.wm.upsertEntity({"type": "node", "externalKey": {"system": SYSTEM,
                                                              "id": str(node["id"])},
                              "attrs": node, "source": source})

    def tree_upsert(self, caller: Caller, node: dict[str, Any]) -> dict[str, Any]:
        self._require(caller, roles.AUTHORS, "change the tree")
        bad = sorted(set(node) - NODE_FIELDS)
        if bad:
            raise Refused(f"Refused: unknown node field {bad[0]}.")
        nid = node.get("id")
        if not isinstance(nid, str) or not _SAFE_ID.fullmatch(nid):
            raise Refused("Refused: a node needs a plain id.")
        with jsonl.exclusive(self._lock):
            existing = {n["id"]: n for n in self.tree()}
            merged: dict[str, Any] = dict(existing.get(nid) or {
                "id": nid, "parent": None, "doubt": None, "settles_by": None,
                "flip_point": None, "crux": False, "branch_id": None, "status": "open"})
            merged.update(node)
            if merged.get("kind") not in NODE_KINDS:
                raise Refused(f"Refused: kind must be one of {', '.join(NODE_KINDS)}.")
            if not isinstance(merged.get("statement"), str) or not merged["statement"].strip():
                raise Refused("Refused: a node needs a statement.")
            parent = merged.get("parent")
            if parent is not None and (parent not in existing or parent == nid):
                raise Refused(f"Refused: parent {parent} is not a node.")
            if not isinstance(merged.get("crux"), bool):
                raise Refused("Refused: crux is true or false.")
            branch = merged.get("branch_id")
            if branch is not None and (not isinstance(branch, str)
                                       or not _SAFE_ID.fullmatch(branch)):
                raise Refused("Refused: branch_id is not a plain id.")
            old_status = (existing.get(nid) or {}).get("status")
            if ("status" in node and node["status"] != old_status
                    and node["status"] not in SETTABLE_BY_AUTHORS):
                raise Refused(f"Refused: status {node['status']} is not set by tree_upsert.")
            self._put_node(merged, self._src(caller))
            self._write_tree()
        return merged

    def _branch_dir(self, branch_id: str) -> Path:
        if not _SAFE_ID.fullmatch(branch_id):
            raise Refused("Refused: branch_id is not a plain id.")
        direct = self.run_dir / "branches" / branch_id
        if direct.is_dir():
            return direct
        hits = [p for p in (self.run_dir / "branches").glob(f"**/sub/{branch_id}") if p.is_dir()]
        if len(hits) != 1:
            raise Refused(f"Refused: branch {branch_id} has no folder.")
        return hits[0]

    def _branch_nodes(self, branch_id: str) -> list[dict[str, Any]]:
        nodes = [n for n in self.tree() if n.get("branch_id") == branch_id]
        if not nodes:
            raise Refused(f"Refused: no tree node has branch {branch_id}.")
        return nodes

    def propose_branch_done(self, caller: Caller, branch_id: str) -> dict[str, Any]:
        self._require(caller, roles.AUTHORS, "propose a branch done")
        record = self._branch_dir(branch_id) / "record.md"
        if not record.is_file():
            raise Refused("Refused: the branch has no record.md.")
        headings = [h.strip() for h in re.findall(r"^#{1,6}\s+(.+?)\s*$",
                                                   record.read_text(encoding="utf-8"), re.MULTILINE)]
        missing = [h for h in RECORD_HEADINGS if h not in headings]
        if missing:
            raise Refused(f"Refused: record.md lacks the heading {missing[0]!r}.")
        with jsonl.exclusive(self._lock):
            nodes = self._branch_nodes(branch_id)
            source = self._src(caller)
            for n in nodes:
                self._put_node({**n, "status": "proposed_done"}, source)
            self._write_tree()
            prior = checks_api.for_prompt(self.run_dir, "branch-reviewer", f"branch:{branch_id}")
            req = checks_api.request_check(self.run_dir, roles.BRANCH_REVIEWER,
                                           f"branch:{branch_id}", "propose_branch_done",
                                           round_no=len(prior) + 1)
        return {"branch_id": branch_id, "record_digest": sha256_file(record),
                "check_requested": req.request_id}

    def record_branch_verdict(self, caller: Caller, branch_id: str, verdict: str,
                              reason: str, method: str | None = None) -> dict[str, Any]:
        self._require(caller, frozenset({roles.BRANCH_REVIEWER}), "record branch verdicts")
        self._session_prompt_is(caller, "branch-reviewer", f"branch:{branch_id}")
        if verdict not in BRANCH_VERDICTS:
            raise Refused(f"Refused: verdict must be one of {', '.join(BRANCH_VERDICTS)}.")
        if not isinstance(reason, str) or not reason.strip():
            raise Refused("Refused: a branch verdict carries its reason.")
        if verdict == "find_more" and not (isinstance(method, str) and method.strip()):
            raise Refused("Refused: find_more names the method.")
        branch_dir = self._branch_dir(branch_id)
        with jsonl.exclusive(self._lock):
            nodes = self._branch_nodes(branch_id)
            if any(n["status"] != "proposed_done" for n in nodes):
                raise Refused("Refused: the branch is not proposed done.")
            digest = sha256_file(branch_dir / "record.md")
            body = {"branch_id": branch_id, "verdict": verdict, "reason": reason,
                    "method": method, "record_digest": digest, "by": caller.agent_type,
                    "session": caller.session_id, "at": now()}
            source = self._src(caller, kind="branch-reviewer")
            for n in nodes:
                self.wm.assertFact({"about": node_entity(n["id"]), "predicate": "branch_verdict",
                                    "value": body, "source": source})
                self._put_node({**n, "status": BRANCH_VERDICTS[verdict]}, source)
            vdir = branch_dir / "verdicts"
            vdir.mkdir(exist_ok=True)
            number = len(list(vdir.glob("*.json"))) + 1
            (vdir / f"{number}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
            self._write_tree()
        return {**body, "verdict_file": str((vdir / f"{number}.json").relative_to(self.run_dir))}

    # ---------------------------------------------------- frame, drafts, finish
    def _run_facts(self, predicate: str) -> list[dict[str, Any]]:
        if self.wm.view("graph").entity(RUN_ENTITY) is None:
            return []
        return [f["value"] for f in self.wm.view("facts").about(RUN_ENTITY,
                                                                 {"predicate": predicate})]

    def _run_fact(self, caller: Caller, predicate: str, value: Any) -> None:
        source = self._src(caller)
        self._ensure_entity(RUN_ENTITY, "run", "run", source)
        self.wm.assertFact({"about": RUN_ENTITY, "predicate": predicate, "value": value,
                            "source": source})

    def request_frame_critique(self, caller: Caller) -> dict[str, Any]:
        self._require(caller, frozenset({roles.LEAD}), "ask for the frame critique")
        if not (self.run_dir / "frame.md").is_file():
            raise Refused("Refused: frame.md does not exist.")
        if not (self.run_dir / "framer" / "own-frame.md").is_file():
            raise Refused("Refused: the blind framer's own frame has not landed.")
        with jsonl.exclusive(self._lock):
            self.wm.refresh()
            prior = checks_api.for_prompt(self.run_dir, "blind-framer", "frame:critique")
            req = checks_api.request_check(self.run_dir, roles.BLIND_FRAMER, "frame:critique",
                                           "request_frame_critique", round_no=len(prior) + 1)
            self._run_fact(caller, "frame_critique_requested",
                           {"frame_digest": sha256_file(self.run_dir / "frame.md"),
                            "round": req.round})
        return {"check_requested": req.request_id, "round": req.round}

    def submitted_versions(self) -> list[int]:
        self.wm.refresh()
        return [int(v["version"]) for v in self._run_facts("draft_submitted")]

    def submit_draft(self, caller: Caller, version: int) -> dict[str, Any]:
        self._require(caller, frozenset({roles.LEAD}), "submit drafts")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise Refused("Refused: version is a positive whole number.")
        path = self.run_dir / "draft" / f"answer.v{version}.md"
        if not path.is_file():
            raise Refused(f"Refused: draft/answer.v{version}.md does not exist.")
        with jsonl.exclusive(self._lock):
            done = self.submitted_versions()
            if done and version <= max(done):
                raise Refused(f"Refused: version {version} is not after version {max(done)}.")
            frozen = self.dir / "drafts" / f"answer.v{version}.md"
            frozen.parent.mkdir(exist_ok=True)
            frozen.write_bytes(path.read_bytes())
            self._run_fact(caller, "draft_submitted",
                           {"version": version, "sha256": sha256_file(path)})
            reqs = [checks_api.request_check(self.run_dir, role, f"draft:{version}",
                                             "submit_draft").request_id
                    for role in roles.FINAL_REVIEWERS]
        return {"version": version, "checks_requested": reqs}

    def finish_failures(self) -> list[str]:
        """Every unmet condition of the release gate (CONTRACTS section 6)."""
        failures: list[str] = []
        nodes = self.tree()
        if not nodes:
            failures.append("the tree has no nodes")
        for n in nodes:
            if n["status"] not in DONE:
                failures.append(f"node {n['id']} is {n['status']}")
        for f in ("framer/critique.md", "framer/lead-response.md"):
            if not (self.run_dir / f).is_file():
                failures.append(f"{f} does not exist")
        versions = self.submitted_versions()
        if not versions:
            failures.append("no draft was submitted")
        else:
            v = max(versions)
            for r in REVIEW_FILES:
                if not (self.run_dir / "reviews" / f"v{v}" / r).is_file():
                    failures.append(f"reviews/v{v}/{r} does not exist")
        answer = self.run_dir / "deliverable" / "answer.md"
        if not answer.is_file():
            failures.append("deliverable/answer.md does not exist")
            return failures
        text = answer.read_text(encoding="utf-8")
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
        return failures

    def finish_run(self, caller: Caller) -> dict[str, Any]:
        self._require(caller, frozenset({roles.LEAD}), "finish the run")
        with jsonl.exclusive(self._lock):
            failures = self.finish_failures()
            if failures:
                raise Refused("Refused: finish_run: " + "; ".join(failures) + ".")
            self._render_reader()
            self._run_fact(caller, "finished", {"at": now()})
            req = checks_api.request_check(self.run_dir, roles.RETRO_WRITER, "retro:run",
                                           "finish_run")
        return {"finished": True, "reader": "deliverable/answer.reader.md",
                "check_requested": req.request_id}

    def _render_reader(self) -> None:
        text = (self.run_dir / "deliverable" / "answer.md").read_text(encoding="utf-8")
        order: dict[str, int] = {}
        refs: list[str] = []

        def ref(m: re.Match[str]) -> str:
            short = m.group(1)
            if short not in order:
                order[short] = len(order) + 1
                rec = self.get_claim(short)
                unchecked = (rec["verdict"] or {}).get("verdict") == "could_not_check"
                mark = " (not checked)" if unchecked else ""
                receipts = ", ".join(s["receipt_id"] for s in rec["support"]) or rec["tier"]
                refs.append(f"{order[short]}. {rec['statement']} [{rec['tier']}; {receipts}]{mark}")
            rec_unchecked = refs[order[short] - 1].endswith("(not checked)")
            return f" [{order[short]}{', not checked' if rec_unchecked else ''}]"

        body = tags.C_TAG.sub(ref, text)
        body = tags.NC_TAG.sub("", body)
        out = body.rstrip() + "\n\n## References\n\n" + "\n".join(refs) + "\n"
        (self.run_dir / "deliverable" / "answer.reader.md").write_text(out, encoding="utf-8")

    # ------------------------------------------------------------------- await
    def _landed(self, target: str) -> dict[str, Any] | None:
        kind, _, ident = target.partition(":")
        if kind == "claim":
            verdict: dict[str, Any] | None = self._claim_record(
                self.resolve_claim_id(ident))["verdict"]
            return verdict
        if kind == "branch":
            vdir = self._branch_dir(ident) / "verdicts"
            rounds = checks_api.for_prompt(self.run_dir, "branch-reviewer", target)
            files = sorted(vdir.glob("*.json"), key=lambda p: int(p.stem)) if vdir.is_dir() else []
            if rounds and len(files) >= len(rounds):
                result: dict[str, Any] = json.loads(files[-1].read_text(encoding="utf-8"))
                return result
            return None
        if kind == "draft":
            have = [r for r in REVIEW_FILES if (self.run_dir / "reviews" / f"v{ident}" / r).is_file()]
            return {"reviews": have} if len(have) == len(REVIEW_FILES) else None
        if kind == "frame":
            f = {"own": "framer/own-frame.md", "critique": "framer/critique.md"}.get(ident)
            if f is None:
                raise Refused(f"Refused: {target} is not a check id.")
            return {"file": f} if (self.run_dir / f).is_file() else None
        raise Refused(f"Refused: {target} is not a check id.")

    def _normalise_target(self, given: str) -> str:
        g = given.strip()
        if g.split(":", 1)[0] in ("claim", "branch", "draft", "frame"):
            if g.startswith("claim:"):
                return "claim:" + self.resolve_claim_id(g)
            return g
        return "claim:" + self.resolve_claim_id(g)

    def await_checks(self, ids: list[str], max_wait_s: float) -> dict[str, Any]:
        """Verdicts that have landed and checks still running. Never invents one."""
        if not isinstance(ids, list) or not ids:
            raise Refused("Refused: name at least one check id.")
        targets = [self._normalise_target(str(i)) for i in ids]
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
            open_ = [w for w in waiting if w["status"] in ("pending", "running")]
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


def open_register(run_dir: Path) -> Register:
    return Register(run_dir)
