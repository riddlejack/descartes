"""The WorldModel facade: writes, views and the operator surface.

Every mutation is one transaction: under the log's exclusive lock it folds what
other processes appended, validates, computes the post-images of the records it
changes, appends them as one line, and folds that line back. A mutation that
would change nothing appends nothing, so re-running the same input leaves the log
byte-identical.

Record timestamps come from the write's provenance (`source.at`). The clock is
read only for operator actions that genuinely happen now (approving or rejecting
a merge).
"""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar, cast, overload

from . import validate as v
from .errors import (
    ProposalStateError,
    UnknownRecordError,
    UnknownViewError,
    ValidationError,
)
from .ids import (
    canonicalJson,
    contextIdFromAttrs,
    entityIdFromName,
    externalId,
    factId,
    normalizeName,
    proposalId,
    relationshipId,
    sha256Hex,
)
from .log import FileLog, Log, MemoryLog
from .records import (
    AssertFactInput,
    AuditEntry,
    ConflictPolicy,
    Context,
    Entity,
    EntityState,
    ExternalKey,
    Fact,
    IngestObservation,
    IngestReport,
    MergeProposal,
    Provenance,
    RelateInput,
    Relationship,
    UpsertContextInput,
    UpsertEntityInput,
    WorldEvent,
)
from .resolve import AUTO_MERGE_AT, REVIEW_LOWER, decide, nameSimilarity
from .state import SCHEMA_VERSION, Staging, State

T = TypeVar("T")

_POLICIES: tuple[ConflictPolicy, ...] = (
    "most-recent-wins",
    "highest-confidence-wins",
    "source-priority",
    "manual",
)
_STATES: tuple[EntityState, ...] = ("emerging", "active", "archived")
# Resource limit: default depth of a graph traversal.
_TRAVERSE_DEPTH = 5
# Logical invariant: the external-key system under which ingest keys observations.
_INGEST_SYSTEM = "ingest"


def _copy[V](value: V) -> V:
    return copy.deepcopy(value)


def _wall_clock() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Plan[R]:
    """What a mutation's build step hands back: its audit fields and how to read
    its result once the transaction is folded."""

    op: str
    actor: str
    at: str
    summary: str
    result: Callable[[], R]
    run_id: str | None = None
    notes: list[str] = field(default_factory=list)


# Conformance only (see WorldModel.__init__): a folder that turns memory worlds into file logs.
MEMORY_AS_FILE_ENV = "STEMMA_WM_MEMORY_AS_FILE"

class WorldModel:
    """A World Model over an append-only event log.

    Options (a dict, camelCase as the conformance runner sends them):
      storage         "memory" (default) or "file"
      path            the log file (required for "file"); the lock is `<path>.lock`
      namespace       a label, kept for callers; storage is chosen by `path`
      clock           callable returning a datetime or ISO string; operator actions only
      resolver        {"autoMergeAt": float | None, "reviewLower": float}; None turns
                      auto-merge off so every near-duplicate goes to the review queue
      conflictPolicy  most-recent-wins (default), highest-confidence-wins,
                      source-priority or manual
      sourcePriority  provenance kinds, highest priority first (source-priority policy)
    """

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        opts = v.check_keys(
            options or {},
            "WorldModel",
            set(),
            {
                "storage",
                "path",
                "namespace",
                "clock",
                "resolver",
                "conflictPolicy",
                "sourcePriority",
                "schemaVersion",
            },
        )
        storage = opts.get("storage", "memory")
        # Conformance only: World Model's runner always asks for memory storage, so this
        # variable makes every memory world a fresh file log in the named folder, letting
        # the unmodified runner exercise the file store the register uses. Unset in runs.
        file_dir = os.environ.get(MEMORY_AS_FILE_ENV)
        if storage == "memory" and file_dir and opts.get("path") is None:
            storage = "file"
            opts = {**opts, "storage": "file",
                    "path": os.path.join(file_dir, f"world-{os.getpid()}-{id(self)}.jsonl")}
        if storage == "memory":
            if opts.get("path") is not None:
                raise ValidationError("WorldModel: `path` is only for storage 'file'")
            self._log: Log = MemoryLog()
        elif storage == "file":
            path = opts.get("path")
            if not isinstance(path, (str, os.PathLike)) or not str(path):
                raise ValidationError("WorldModel: storage 'file' needs `path` (the log file)")
            self._log = FileLog(path)
        else:
            raise ValidationError(f"WorldModel: storage must be 'memory' or 'file', not {storage!r}")
        namespace = opts.get("namespace")
        if namespace is not None and not isinstance(namespace, str):
            raise ValidationError("WorldModel: `namespace` must be a string")
        self.namespace: str | None = namespace
        if opts.get("schemaVersion", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise ValidationError(f"WorldModel: only schemaVersion {SCHEMA_VERSION} exists")
        clock = opts.get("clock")
        if clock is not None and not callable(clock):
            raise ValidationError("WorldModel: `clock` must be callable")
        self._clock: Callable[[], Any] = clock or _wall_clock

        resolver = v.check_keys(
            opts.get("resolver") or {}, "WorldModel.resolver", set(), {"autoMergeAt", "reviewLower"}
        )
        self._auto_merge_at: float | None = resolver.get("autoMergeAt", AUTO_MERGE_AT)
        self._review_lower: float = resolver.get("reviewLower", REVIEW_LOWER)
        for name, value in (("autoMergeAt", self._auto_merge_at), ("reviewLower", self._review_lower)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValidationError(f"WorldModel.resolver: `{name}` must be a number")
        if self._auto_merge_at is not None and self._auto_merge_at < self._review_lower:
            raise ValidationError("WorldModel.resolver: autoMergeAt is below reviewLower")

        policy = opts.get("conflictPolicy", "most-recent-wins")
        if policy not in _POLICIES:
            raise ValidationError(f"WorldModel: unknown conflictPolicy {policy!r}")
        self._policy: ConflictPolicy = policy
        priority = opts.get("sourcePriority") or []
        if not isinstance(priority, list) or not all(isinstance(k, str) for k in priority):
            raise ValidationError("WorldModel: `sourcePriority` must be a list of strings")
        self._priority: list[str] = list(priority)

        self._lock = threading.RLock()
        self._state = State()
        self._cursor = 0
        self.refresh()

    # ============================================================ log plumbing
    def refresh(self) -> int:
        """Fold transactions other processes appended since the last read.
        Returns how many were folded. Reads call this themselves."""
        with self._lock:
            txs, cursor = self._log.read_from(self._cursor)
            for tx in txs:
                self._state.apply(tx)
            self._cursor = cursor
            return len(txs)

    def _mutate(self, build: Callable[[Staging], _Plan[T]]) -> T:
        with self._lock, self._log.exclusive():
            self.refresh()
            staging = Staging(self._state)
            plan = build(staging)
            if staging.events:
                notes = list(plan.notes)
                torn = self._log.torn_tail(self._cursor)
                if torn:
                    sidecar = self._log.discard_tail(self._cursor, torn)
                    notes.append(
                        f"discarded {len(torn)} bytes of an uncommitted torn append (kept in {sidecar})"
                    )
                tx_no = self._state.tx + 1
                events: list[WorldEvent] = []
                for i, (kind, payload) in enumerate(staging.events, start=1):
                    seq = self._state.seq + i
                    events.append(
                        {
                            "id": f"evt_{seq}",
                            "seq": seq,
                            "kind": kind,
                            "payload": payload,
                            "schemaVersion": SCHEMA_VERSION,
                            "at": plan.at,
                            "tx": tx_no,
                        }
                    )
                summary = plan.summary + "".join(f"; {n}" for n in notes)
                tx: dict[str, Any] = {
                    "tx": tx_no,
                    "at": plan.at,
                    "op": plan.op,
                    "actor": plan.actor,
                    "summary": summary,
                    "events": events,
                }
                if plan.run_id is not None:
                    tx["runId"] = plan.run_id
                self._log.append(tx)
                self.refresh()
                if self._state.tx != tx_no:
                    raise AssertionError("committed transaction did not fold back")
            return plan.result()

    def _now(self) -> str:
        return v.iso(self._clock(), "clock()")

    # ================================================================ writes
    def upsertEntity(self, input: UpsertEntityInput | Mapping[str, Any]) -> Entity:
        """Create or update an entity. Externally keyed: id `type:system:id`.
        Name keyed: id `ent_<hash>`, and a near-duplicate name of the same type
        auto-merges (alias) or is queued for an operator by the merge bands."""
        op = "upsertEntity"
        data = _mapping(input, op)
        source = v.provenance(data.get("source"), op)
        v.check_keys(data, op, {"type"}, {"source", "name", "externalKey", "attrs", "state"})
        etype = v.text(data, "type", op, colon_free=True)
        name = v.optional_text(data, "name", op)
        key = _external_key(data, op)
        if key is None and name is None:
            raise ValidationError(f"{op}: a name-keyed entity needs `name` (or give `externalKey`)")
        attrs = v.attrs(data, op)
        state = _entity_state(data, op)

        def build(st: Staging) -> _Plan[Entity]:
            notes: list[str] = []
            target = self._stage_entity(st, etype, name, key, attrs, state, source, notes)
            return _Plan(
                op, source.get("by", "system"), source["at"], f"{target} ({etype})",
                lambda: _copy(self._state.entities[target]), notes=notes,
            )

        return self._mutate(build)

    def upsertContext(self, input: UpsertContextInput | Mapping[str, Any]) -> Context:
        """Create or update a context. Externally keyed: `type:system:id`; otherwise
        a hash of type, attrs and parent."""
        op = "upsertContext"
        data = _mapping(input, op)
        source = v.provenance(data.get("source"), op)
        v.check_keys(
            data, op, {"type"}, {"source", "externalKey", "attrs", "parent", "startedAt", "endedAt"}
        )
        ctype = v.text(data, "type", op, colon_free=True)
        key = _external_key(data, op)
        attrs = v.attrs(data, op)
        parent = v.optional_text(data, "parent", op)
        started = v.iso(data["startedAt"], f"{op}: startedAt") if data.get("startedAt") else None
        ended = v.iso(data["endedAt"], f"{op}: endedAt") if data.get("endedAt") else None
        if started and ended and ended < started:
            raise ValidationError(f"{op}: endedAt is before startedAt")

        def build(st: Staging) -> _Plan[Context]:
            if parent is not None and st.context(parent) is None:
                raise UnknownRecordError(f"{op}: parent context {parent} does not exist")
            cid = (
                externalId(ctype, key["system"], key["id"])
                if key
                else contextIdFromAttrs(ctype, attrs, parent)
            )
            existing = st.context(cid)
            post = _context_post(existing, ctype, cid, attrs, parent, started, ended, key, source)
            if existing is None or post != existing:
                st.put_context(post)
            return _Plan(
                op, source.get("by", "system"), source["at"], f"{cid} ({ctype})",
                lambda: _copy(self._state.contexts[cid]),
            )

        return self._mutate(build)

    def relate(self, input: RelateInput | Mapping[str, Any]) -> Relationship:
        """Add a bitemporal edge. Unless `multiValued`, a new open edge with the same
        (from, type, context) and a different target closes the open one
        (`validTo` = the new edge's validFrom, `supersededBy` = its id)."""
        op = "relate"
        data = _mapping(input, op)
        source = v.provenance(data.get("source"), op)
        v.check_keys(
            data,
            op,
            {"from", "type", "to"},
            {"source", "context", "attrs", "validFrom", "validTo", "multiValued"},
        )
        frm, to = v.text(data, "from", op), v.text(data, "to", op)
        rtype = v.text(data, "type", op)
        context = v.optional_text(data, "context", op)
        rel_attrs = v.attrs(data, op) if data.get("attrs") is not None else None
        valid_from = v.iso(data["validFrom"], f"{op}: validFrom") if data.get("validFrom") else None
        valid_to = v.iso(data["validTo"], f"{op}: validTo") if data.get("validTo") else None
        multi = data.get("multiValued", False)
        if not isinstance(multi, bool):
            raise ValidationError(f"{op}: `multiValued` must be true or false")

        def build(st: Staging) -> _Plan[Relationship]:
            rid = self._stage_relate(
                st, frm, rtype, to, context, rel_attrs, valid_from, valid_to, multi, source
            )
            return _Plan(
                op, source.get("by", "system"), source["at"], f"{frm} -[{rtype}]-> {to}",
                lambda: _copy(self._state.relationships[rid]),
            )

        return self._mutate(build)

    def assertFact(self, input: AssertFactInput | Mapping[str, Any]) -> Fact:
        """Record a fact about an existing entity. The conflict policy picks which
        current fact on (entity, predicate) wins; the others get `supersededBy`, the
        winner's value is materialized into the entity's attrs, nothing is deleted."""
        op = "assertFact"
        data = _mapping(input, op)
        source = v.provenance(data.get("source"), op)
        v.check_keys(data, op, {"about", "predicate"}, {"source", "value", "confidence", "context"})
        if "value" not in data:
            raise ValidationError(f"{op}: missing field value")
        about_in = v.text(data, "about", op)
        predicate = v.text(data, "predicate", op)
        value = v.json_data(data["value"], f"{op}: value")
        confidence = data.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValidationError(f"{op}: `confidence` must be a number")
            if not 0.0 <= float(confidence) <= 1.0:
                raise ValidationError(f"{op}: `confidence` must be within [0, 1]")
            confidence = float(confidence)
        context = v.optional_text(data, "context", op)

        def build(st: Staging) -> _Plan[Fact]:
            about = st.resolve(about_in)
            entity = st.entity(about)
            if entity is None:
                raise UnknownRecordError(f"{op}: entity {about_in} does not exist")
            if context is not None and st.context(context) is None:
                raise UnknownRecordError(f"{op}: context {context} does not exist")
            fid = factId(about, predicate, value, source["at"], source["kind"], source.get("url"))
            summary = f"{about}.{predicate}"
            if st.fact(fid) is None:
                fact: Fact = {
                    "id": fid,
                    "about": about,
                    "predicate": predicate,
                    "value": value,
                    "confidence": confidence,
                    "source": source,
                    "assertedAt": source["at"],
                }
                if context is not None:
                    fact["context"] = context
                summary += self._stage_fact(st, fact, entity)
            return _Plan(
                op, source.get("by", "system"), source["at"], summary,
                lambda: _copy(self._state.facts[fid]),
            )

        return self._mutate(build)

    def ingest(self, observations: list[IngestObservation] | list[Mapping[str, Any]]) -> IngestReport:
        """Run a batch of observations through the pipeline in one transaction.

        Each observation becomes an entity `<type>:ingest:<externalId>` (type default
        "item") carrying a checksum of its content; each mentioned entity is upserted
        by name (through the merge bands) and linked by a `mentions` edge. An
        observation whose checksum is unchanged is skipped; a changed one is
        reprocessed, and its mentions that are gone are closed. Re-ingesting the
        same batch appends nothing and reports changed=False.
        """
        op = "ingest"
        if not isinstance(observations, list):
            raise ValidationError(f"{op}: observations must be a list")
        parsed = [_observation(o, f"{op}[{i}]") for i, o in enumerate(observations)]
        run_id = "run_" + sha256Hex(canonicalJson([[p.external_id, p.checksum] for p in parsed]))[:12]

        def build(st: Staging) -> _Plan[IngestReport]:
            before = self._shape_canonical()
            events_before = self._state.seq
            processed = skipped = 0
            notes: list[str] = []
            for p in parsed:
                oid = externalId(p.type, _INGEST_SYSTEM, p.external_id)
                existing = st.entity(st.resolve(oid))
                if existing is not None and existing["attrs"].get("checksum") == p.checksum:
                    skipped += 1
                    continue
                processed += 1
                attrs: dict[str, Any] = {"title": p.title, "checksum": p.checksum}
                if p.text is not None:
                    attrs["text"] = p.text
                key: ExternalKey = {"system": _INGEST_SYSTEM, "id": p.external_id}
                oid = self._stage_entity(st, p.type, p.title, key, attrs, None, p.source, [])
                mentioned: list[str] = []
                for mtype, mname in p.mentions:
                    mid = self._stage_entity(st, mtype, mname, None, {}, None, p.source, notes)
                    if mid not in mentioned:
                        mentioned.append(mid)
                # Step 1, reconcile: close this observation's mentions that are gone.
                for rel in list(st.iter_relationships()):
                    if (
                        rel["type"] == "mentions"
                        and st.resolve(rel["from"]) == oid
                        and "validTo" not in rel
                        and st.resolve(rel["to"]) not in mentioned
                    ):
                        closed = _copy(rel)
                        closed["validTo"] = max(p.source["at"], rel["validFrom"])
                        st.put_relationship(closed)
                for mid in mentioned:
                    self._stage_relate(
                        st, oid, "mentions", mid, None, None, None, None, True, p.source
                    )
            at = max((p.source["at"] for p in parsed), default=_EPOCH)
            summary = f"run {run_id}: {processed} processed, {skipped} unchanged"

            def result() -> IngestReport:
                return {
                    "runId": run_id,
                    "processed": processed,
                    "skippedUnchanged": skipped,
                    "eventsAppended": self._state.seq - events_before,
                    "changed": self._shape_canonical() != before,
                }

            return _Plan(op, "pipeline", at, summary, result, run_id=run_id, notes=notes)

        return self._mutate(build)

    # ============================================================== operator
    def mergeQueue(self, status: str | None = None) -> list[MergeProposal]:
        """Merge proposals in the order they were raised; `status` filters
        ("pending", "approved", "rejected")."""
        self.refresh()
        return [
            _copy(p)
            for p in self._state.proposals.values()
            if status is None or p["status"] == status
        ]

    def approveMerge(
        self,
        proposalId: str,
        by: str,
        reason: str | None = None,
        primaryId: str | None = None,
    ) -> MergeProposal:
        """An operator approves a pending proposal: one alias event points the other
        entity at the primary (default: the proposal's rightId, the entity that was
        there first), the primary keeps both names as aliases, and the audit records
        who did it."""
        op = "merge-approved"
        actor = _operator(by, op)

        def build(st: Staging) -> _Plan[MergeProposal]:
            p = self._pending(st, proposalId, op)
            primary = primaryId or p["rightId"]
            if primary not in (p["leftId"], p["rightId"]):
                raise ValidationError(f"{op}: primaryId must be {p['leftId']} or {p['rightId']}")
            other = p["leftId"] if primary == p["rightId"] else p["rightId"]
            at = self._now()
            done = _copy(p)
            done["status"], done["resolvedAt"], done["resolvedBy"] = "approved", at, actor
            if reason:
                done["resolution"] = reason
            st.put_proposal(done)
            alias_id, primary_id = st.resolve(other), st.resolve(primary)
            if alias_id != primary_id:
                self._stage_alias(st, alias_id, primary_id, at, actor, reason or p["reason"])
            return _Plan(
                op, actor, at, f"{p['leftName']} -> {p['rightName']}" if primary == p["rightId"]
                else f"{p['rightName']} -> {p['leftName']}",
                lambda: _copy(self._state.proposals[p["id"]]),
            )

        return self._mutate(build)

    def rejectMerge(self, proposalId: str, by: str, reason: str | None = None) -> MergeProposal:
        """An operator rejects a pending proposal; the pair is never proposed or
        auto-merged again."""
        op = "merge-rejected"
        actor = _operator(by, op)

        def build(st: Staging) -> _Plan[MergeProposal]:
            p = self._pending(st, proposalId, op)
            at = self._now()
            done = _copy(p)
            done["status"], done["resolvedAt"], done["resolvedBy"] = "rejected", at, actor
            if reason:
                done["resolution"] = reason
            st.put_proposal(done)
            return _Plan(
                op, actor, at, f"{p['leftName']} stays separate from {p['rightName']}",
                lambda: _copy(self._state.proposals[p["id"]]),
            )

        return self._mutate(build)

    # ================================================================= reads
    @overload
    def view(self, name: Literal["graph"]) -> GraphView: ...
    @overload
    def view(self, name: Literal["facts"]) -> FactsView: ...
    @overload
    def view(self, name: Literal["snapshot"]) -> SnapshotView: ...
    @overload
    def view(self, name: Literal["timeline"]) -> TimelineView: ...
    @overload
    def view(self, name: str) -> GraphView | FactsView | SnapshotView | TimelineView: ...
    def view(self, name: str) -> GraphView | FactsView | SnapshotView | TimelineView:
        """Read surfaces: "graph", "facts", "snapshot", "timeline"."""
        if name == "graph":
            return GraphView(self)
        if name == "facts":
            return FactsView(self)
        if name == "snapshot":
            return SnapshotView(self)
        if name == "timeline":
            return TimelineView(self)
        raise UnknownViewError(f"World Model has no view {name!r}")

    def resolveAlias(self, entityId: str) -> str:
        self.refresh()
        return self._state.resolve(entityId)

    def audit(self) -> list[AuditEntry]:
        """One entry per committed mutation, `seq` strictly increasing."""
        self.refresh()
        return _copy(self._state.audit)

    def events(self) -> list[WorldEvent]:
        """The event log, in append order."""
        self.refresh()
        return _copy(self._state.events)

    def stateShape(self) -> dict[str, Any]:
        """Everything derived from the writes, sorted; excludes log positions
        (event ids, sequence numbers) and the audit."""
        self.refresh()
        return self._shape()

    def stateShapeCanonical(self) -> str:
        self.refresh()
        return self._shape_canonical()

    def _shape(self) -> dict[str, Any]:
        s = self._state
        return _copy(
            {
                "entities": [s.entities[k] for k in sorted(s.entities)],
                "contexts": [s.contexts[k] for k in sorted(s.contexts)],
                "relationships": [s.relationships[k] for k in sorted(s.relationships)],
                "facts": [s.facts[k] for k in sorted(s.facts)],
                "aliases": [
                    {"aliasId": k, "primaryId": s.aliases[k]} for k in sorted(s.aliases)
                ],
                "mergeProposals": [s.proposals[k] for k in sorted(s.proposals)],
            }
        )

    def _shape_canonical(self) -> str:
        return canonicalJson(self._shape())

    # ============================================================== staging
    def _stage_entity(
        self,
        st: Staging,
        etype: str,
        name: str | None,
        key: ExternalKey | None,
        attrs: dict[str, Any],
        state: EntityState | None,
        source: Provenance,
        notes: list[str],
    ) -> str:
        """Stage one entity upsert (with name resolution) and return the id the
        write landed on."""
        if key is not None:
            eid = externalId(etype, key["system"], key["id"])
        else:
            assert name is not None
            eid = entityIdFromName(etype, name)
        target = st.resolve(eid)
        existing = st.entity(target)
        keep_name = False
        if existing is None and key is None and name is not None:
            merged_into = self._resolve_name(st, etype, eid, name, source, notes)
            if merged_into is not None:
                target, existing, keep_name = merged_into, st.entity(merged_into), True
        if existing is not None and existing["type"] != etype:
            raise ValidationError(
                f"upsertEntity: {target} is a {existing['type']!r}, not a {etype!r}"
            )
        post = _entity_post(existing, etype, target, name, attrs, state, key, source, keep_name)
        if existing is None or post != existing:
            st.put_entity(post)
        return target

    def _resolve_name(
        self,
        st: Staging,
        etype: str,
        eid: str,
        name: str,
        source: Provenance,
        notes: list[str],
    ) -> str | None:
        """Merge bands against every primary named entity of the same type. Returns
        the entity to auto-merge into, or None (after queueing any review-band
        proposals)."""
        auto: list[tuple[float, str]] = []
        review: list[tuple[float, Entity]] = []
        for other in st.iter_entities():
            if other["type"] != etype or "name" not in other or other["id"] == eid:
                continue
            if st.resolve(other["id"]) != other["id"]:
                continue  # already an alias of something else
            prior = st.proposal(proposalId(eid, other["id"]))
            if prior is not None and prior["status"] == "rejected":
                continue  # an operator said these are different
            similarity = max(nameSimilarity(name, n) for n in [other["name"], *other["aliases"]])
            band = decide(similarity, self._auto_merge_at, self._review_lower)
            if band == "auto-merge":
                auto.append((similarity, other["id"]))
            elif band == "review" and prior is None:
                review.append((similarity, other))
        if auto:
            similarity, primary = min(auto, key=lambda t: (-t[0], t[1]))
            self._stage_alias(
                st, eid, primary, source["at"], "resolver", f"name similarity {similarity:.3f}"
            )
            notes.append(f"auto-merged {name!r} into {primary} (similarity {similarity:.3f})")
            return primary
        for similarity, other in sorted(review, key=lambda t: t[1]["id"]):
            pid = proposalId(eid, other["id"])
            st.put_proposal(
                {
                    "id": pid,
                    "leftId": eid,
                    "rightId": other["id"],
                    "leftName": name,
                    "rightName": other.get("name", other["id"]),
                    "similarity": similarity,
                    "reason": (
                        f"name similarity {similarity:.3f} in the review band "
                        f"[{self._review_lower}, {self._auto_merge_at})"
                    ),
                    "status": "pending",
                    "proposedAt": source["at"],
                }
            )
            notes.append(f"queued {pid}: {name!r} ~ {other.get('name')!r} ({similarity:.3f})")
        return None

    def _stage_alias(
        self, st: Staging, alias_id: str, primary_id: str, at: str, by: str, reason: str
    ) -> None:
        st.put_alias(
            alias_id,
            primary_id,
            {
                "aliasId": alias_id,
                "primaryId": primary_id,
                "mergedAt": at,
                "mergedBy": by,
                "reason": reason,
            },
        )
        primary = st.entity(primary_id)
        merged = st.entity(alias_id)
        if primary is not None and merged is not None:
            post = _copy(primary)
            for n in merged["aliases"]:
                if n not in post["aliases"]:
                    post["aliases"].append(n)
            if post != primary:
                st.put_entity(post)

    def _stage_relate(
        self,
        st: Staging,
        frm_in: str,
        rtype: str,
        to_in: str,
        context: str | None,
        attrs: dict[str, Any] | None,
        valid_from_in: str | None,
        valid_to: str | None,
        multi: bool,
        source: Provenance,
    ) -> str:
        frm, to = st.resolve(frm_in), st.resolve(to_in)
        for end, label in ((frm, frm_in), (to, to_in)):
            if st.entity(end) is None:
                raise UnknownRecordError(f"relate: entity {label} does not exist")
        if context is not None and st.context(context) is None:
            raise UnknownRecordError(f"relate: context {context} does not exist")
        valid_from = valid_from_in or source["at"]
        if valid_to is not None and valid_to <= valid_from:
            raise ValidationError("relate: validTo must be after validFrom")
        rid = relationshipId(frm, rtype, to, context, valid_from_in)
        if st.relationship(rid) is not None:
            return rid  # the same edge again: a no-op
        rel: Relationship = {
            "id": rid,
            "from": frm,
            "type": rtype,
            "to": to,
            "validFrom": valid_from,
            "schemaVersion": SCHEMA_VERSION,
            "source": source,
            "createdAt": source["at"],
            "multiValued": multi,
        }
        if context is not None:
            rel["context"] = context
        if attrs is not None:
            rel["attrs"] = attrs
        if valid_to is not None:
            rel["validTo"] = valid_to
        closes: list[Relationship] = []
        later: list[Relationship] = []
        if not multi and valid_to is None:
            for other in st.iter_relationships():
                if (
                    "validTo" in other
                    or other["type"] != rtype
                    or other.get("context") != context
                    or st.resolve(other["from"]) != frm
                    or st.resolve(other["to"]) == to
                ):
                    continue
                (closes if other["validFrom"] <= valid_from else later).append(other)
        if later:
            # A back-dated edge: it is closed by the earliest edge that starts after it.
            successor = min(later, key=lambda r: (r["validFrom"], r["id"]))
            rel["validTo"] = successor["validFrom"]
            rel["supersededBy"] = successor["id"]
        st.put_relationship(rel)
        for old in sorted(closes, key=lambda r: r["id"]):
            closed = _copy(old)
            closed["validTo"] = valid_from
            closed["supersededBy"] = rid
            st.put_relationship(closed)
        return rid

    def _stage_fact(self, st: Staging, fact: Fact, entity: Entity) -> str:
        about, predicate = fact["about"], fact["predicate"]
        if self._policy == "manual":
            st.put_fact(fact)
            return " (manual: not materialized)"
        rivals = sorted(
            (
                f
                for f in st.iter_facts()
                if f["predicate"] == predicate
                and "supersededBy" not in f
                and st.resolve(f["about"]) == about
            ),
            key=lambda f: (f["assertedAt"], f["id"]),
        )
        winner = fact
        for rival in rivals:
            if self._beats(rival, winner):
                winner = rival
        if winner["id"] != fact["id"]:
            fact = _copy(fact)
            fact["supersededBy"] = winner["id"]
        st.put_fact(fact)
        for rival in rivals:
            if rival["id"] != winner["id"]:
                loser = _copy(rival)
                loser["supersededBy"] = winner["id"]
                st.put_fact(loser)
        current = st.entity(entity["id"]) or entity
        if predicate not in current["attrs"] or current["attrs"][predicate] != winner["value"]:
            post = _copy(current)
            post["attrs"][predicate] = _copy(winner["value"])
            st.put_entity(post)
        return f" = {canonicalJson(winner['value'])} ({self._policy})"

    def _beats(self, a: Fact, b: Fact) -> bool:
        """Does `a` win over the current winner `b` under the conflict policy?"""
        if self._policy == "highest-confidence-wins":
            if a["confidence"] is None or b["confidence"] is None:
                raise ValidationError(
                    "assertFact: highest-confidence-wins needs `confidence` on every fact"
                )
            return a["confidence"] > b["confidence"]
        if self._policy == "source-priority":
            return _rank(a["source"]["kind"], self._priority) < _rank(
                b["source"]["kind"], self._priority
            )
        return a["assertedAt"] > b["assertedAt"]

    def _pending(self, st: Staging, pid: str, op: str) -> MergeProposal:
        p = st.proposal(pid)
        if p is None:
            raise UnknownRecordError(f"{op}: no merge proposal {pid}")
        if p["status"] != "pending":
            raise ProposalStateError(f"{op}: proposal {pid} is already {p['status']}")
        return p


# ==================================================================== views
class _View:
    def __init__(self, model: WorldModel) -> None:
        self._m = model

    def _state(self) -> State:
        self._m.refresh()
        return self._m._state


class GraphView(_View):
    """Relational and traversal reads over the current fold."""

    def entity(self, id: str) -> Entity | None:
        s = self._state()
        e = s.entities.get(s.resolve(id))
        return _copy(e) if e is not None else None

    def relationship(self, id: str) -> Relationship | None:
        r = self._state().relationships.get(id)
        return _copy(r) if r is not None else None

    def edges(self, id: str, opts: Mapping[str, Any] | None = None) -> list[Relationship]:
        """Edges touching `id`. opts: via, inContext, at (point in time), direction
        ("out" | "in" | "both", default both), includeClosed (default False; ignored
        when `at` is given)."""
        o = v.check_keys(
            opts or {}, "edges", set(), {"via", "inContext", "at", "direction", "includeClosed"}
        )
        s = self._state()
        return [_copy(r) for r, _ in self._matching(s, id, o, include_closed=True)]

    def neighborsOf(self, id: str, opts: Mapping[str, Any] | None = None) -> list[Entity]:
        """Entities across matching edges. opts: via, inContext, at, direction
        (default both). Without `at`, only open edges count."""
        o = v.check_keys(opts or {}, "neighborsOf", set(), {"via", "inContext", "at", "direction"})
        s = self._state()
        found: dict[str, Entity] = {}
        for _, other in self._matching(s, id, o, include_closed=False):
            e = s.entities.get(other)
            if e is not None:
                found[e["id"]] = e
        return [_copy(found[k]) for k in sorted(found)]

    def incoming(self, id: str, opts: Mapping[str, Any] | None = None) -> list[Entity]:
        o = dict(v.check_keys(opts or {}, "incoming", set(), {"via", "inContext", "at"}))
        o["direction"] = "in"
        return self.neighborsOf(id, o)

    def outgoing(self, id: str, opts: Mapping[str, Any] | None = None) -> list[Entity]:
        o = dict(v.check_keys(opts or {}, "outgoing", set(), {"via", "inContext", "at"}))
        o["direction"] = "out"
        return self.neighborsOf(id, o)

    def _matching(
        self, s: State, id: str, o: Mapping[str, Any], include_closed: bool
    ) -> list[tuple[Relationship, str]]:
        rid = s.resolve(id)
        direction = o.get("direction", "both")
        if direction not in ("out", "in", "both"):
            raise ValidationError(f"direction must be out, in or both, not {direction!r}")
        at = v.iso(o["at"], "at") if o.get("at") else None
        show_closed = include_closed and bool(o.get("includeClosed", False))
        out: list[tuple[Relationship, str]] = []
        for r in s.relationships.values():
            if o.get("via") is not None and r["type"] != o["via"]:
                continue
            if o.get("inContext") is not None and r.get("context") != o["inContext"]:
                continue
            if at is not None:
                if not _valid_at(r, at):
                    continue
            elif "validTo" in r and not show_closed:
                continue
            frm, to = s.resolve(r["from"]), s.resolve(r["to"])
            if direction in ("out", "both") and frm == rid:
                out.append((r, to))
            elif direction in ("in", "both") and to == rid:
                out.append((r, frm))
        out.sort(key=lambda t: (t[0]["validFrom"], t[0]["id"]))
        return out

    def entitiesInContext(self, contextId: str, opts: Mapping[str, Any] | None = None) -> list[Entity]:
        o = v.check_keys(opts or {}, "entitiesInContext", set(), {"type"})
        s = self._state()
        found: dict[str, Entity] = {}
        for r in s.relationships.values():
            if r.get("context") != contextId:
                continue
            for end in (s.resolve(r["from"]), s.resolve(r["to"])):
                e = s.entities.get(end)
                if e is not None and (o.get("type") is None or e["type"] == o["type"]):
                    found[e["id"]] = e
        return [_copy(found[k]) for k in sorted(found)]

    def traverse(self, startId: str, opts: Mapping[str, Any]) -> list[Entity]:
        """Follow open `via` edges: "ancestors" goes from -> to, "descendants" to -> from."""
        o = v.check_keys(opts, "traverse", {"via"}, {"direction", "maxDepth"})
        direction = o.get("direction", "ancestors")
        if direction not in ("ancestors", "descendants"):
            raise ValidationError("traverse: direction must be ancestors or descendants")
        depth = o.get("maxDepth", _TRAVERSE_DEPTH)
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValidationError("traverse: maxDepth must be a non-negative integer")
        s = self._state()
        start = s.resolve(startId)
        seen = {start}
        frontier = [start]
        out: list[Entity] = []
        for _ in range(depth):
            nxt: list[str] = []
            for node in frontier:
                for r in sorted(s.relationships.values(), key=lambda r: r["id"]):
                    if r["type"] != o["via"] or "validTo" in r:
                        continue
                    frm, to = s.resolve(r["from"]), s.resolve(r["to"])
                    target = to if (direction == "ancestors" and frm == node) else (
                        frm if (direction == "descendants" and to == node) else None
                    )
                    if target is not None and target not in seen:
                        seen.add(target)
                        nxt.append(target)
                        e = s.entities.get(target)
                        if e is not None:
                            out.append(_copy(e))
            frontier = nxt
        return out

    def query(self, opts: Mapping[str, Any] | None = None) -> list[Entity]:
        """Primary entities (aliases left out), sorted by id. opts: type, attrs
        (every given attr equal), where (a predicate on the entity)."""
        o = v.check_keys(opts or {}, "query", set(), {"type", "attrs", "where"})
        s = self._state()
        want = o.get("attrs") or {}
        where = o.get("where")
        out = []
        for k in sorted(s.entities):
            e = s.entities[k]
            if s.resolve(k) != k:
                continue
            if o.get("type") is not None and e["type"] != o["type"]:
                continue
            if any(e["attrs"].get(a) != val for a, val in want.items()):
                continue
            copy_e = _copy(e)
            if where is not None and not where(copy_e):
                continue
            out.append(copy_e)
        return out


class FactsView(_View):
    def about(self, id: str, opts: Mapping[str, Any] | None = None) -> list[Fact]:
        """Facts about an entity (through aliases), oldest first. opts: predicate,
        current (True: only facts not superseded)."""
        o = v.check_keys(opts or {}, "about", set(), {"predicate", "current"})
        s = self._state()
        rid = s.resolve(id)
        out = [
            f
            for f in s.facts.values()
            if s.resolve(f["about"]) == rid
            and (o.get("predicate") is None or f["predicate"] == o["predicate"])
            and (not o.get("current") or "supersededBy" not in f)
        ]
        return [_copy(f) for f in sorted(out, key=lambda f: (f["assertedAt"], f["id"]))]

    def fact(self, id: str) -> Fact | None:
        f = self._state().facts.get(id)
        return _copy(f) if f is not None else None


class SnapshotView(_View):
    def entityAt(self, id: str, at: str) -> Entity | None:
        """The entity as the log described it at `at`: the last post-image written
        at or before `at`, after following aliases made by then."""
        when = v.iso(at, "entityAt")
        s = self._state()
        aliases: dict[str, str] = {}
        images: dict[str, Entity] = {}
        for ev in s.events:
            if ev["at"] > when:
                continue
            if ev["kind"] == "alias":
                aliases[ev["payload"]["aliasId"]] = ev["payload"]["primaryId"]
            elif ev["kind"] == "upsertEntity":
                images[ev["payload"]["id"]] = cast(Entity, ev["payload"])
        current = id
        seen = {id}
        while current in aliases and aliases[current] not in seen:
            current = aliases[current]
            seen.add(current)
        e = images.get(current)
        return _copy(e) if e is not None else None


class TimelineView(_View):
    def entitiesOfType(self, type: str, opts: Mapping[str, Any] | None = None) -> list[Entity]:
        """Primary entities of a type first seen within [from, to], by createdAt."""
        o = v.check_keys(opts or {}, "entitiesOfType", set(), {"from", "to"})
        lo = v.iso(o["from"], "from") if o.get("from") else None
        hi = v.iso(o["to"], "to") if o.get("to") else None
        s = self._state()
        out = [
            e
            for k, e in s.entities.items()
            if s.resolve(k) == k
            and e["type"] == type
            and (lo is None or e["createdAt"] >= lo)
            and (hi is None or e["createdAt"] <= hi)
        ]
        return [_copy(e) for e in sorted(out, key=lambda e: (e["createdAt"], e["id"]))]

    def get(self) -> list[WorldEvent]:
        return _copy(self._state().events)


# ================================================================== helpers
_EPOCH = "1970-01-01T00:00:00.000Z"


@dataclass
class _Obs:
    external_id: str
    type: str
    title: str
    text: str | None
    source: Provenance
    mentions: list[tuple[str, str]]
    checksum: str


def _observation(raw: Any, op: str) -> _Obs:
    data = _mapping(raw, op)
    at = data.get("at")
    src = data.get("source")
    if not isinstance(src, Mapping):
        v.provenance(src, op)  # raises: no provenance, no write
        raise TypeError("unreachable: provenance() refuses a missing source")
    # Ruling (as the spec's reference pins it): the observation's own `at` is the
    # record time, so it overrides `source.at`.
    source = v.provenance({**src, "at": at if at is not None else src.get("at")}, op)
    v.check_keys(data, op, {"externalId", "title", "at"}, {"source", "type", "text", "entities"})
    external_id = v.text(data, "externalId", op)
    otype = v.text(data, "type", op, colon_free=True) if data.get("type") else "item"
    title = v.text(data, "title", op)
    text = data.get("text")
    if text is not None and not isinstance(text, str):
        raise ValidationError(f"{op}: `text` must be a string")
    mentions: list[tuple[str, str]] = []
    raw_mentions = data.get("entities") or []
    if not isinstance(raw_mentions, list):
        raise ValidationError(f"{op}: `entities` must be a list")
    for i, m in enumerate(raw_mentions):
        mm = v.check_keys(m, f"{op}.entities[{i}]", {"type", "name"}, set())
        mentions.append(
            (
                v.text(mm, "type", f"{op}.entities[{i}]", colon_free=True),
                v.text(mm, "name", f"{op}.entities[{i}]"),
            )
        )
    checksum = "sha256:" + sha256Hex(
        canonicalJson(
            {
                "externalId": external_id,
                "type": otype,
                "title": title,
                "text": text,
                "at": source["at"],
                "entities": [[t, n] for t, n in mentions],
            }
        )
    )
    return _Obs(external_id, otype, title, text, source, mentions, checksum)


def _mapping(data: Any, op: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValidationError(f"{op}: input must be an object, got {type(data).__name__}")
    return data


def _external_key(data: Mapping[str, Any], op: str) -> ExternalKey | None:
    raw = data.get("externalKey")
    if raw is None:
        return None
    k = v.check_keys(raw, f"{op}.externalKey", {"system", "id"}, set())
    return {
        "system": v.text(k, "system", f"{op}.externalKey", colon_free=True),
        "id": v.text(k, "id", f"{op}.externalKey"),
    }


def _entity_state(data: Mapping[str, Any], op: str) -> EntityState | None:
    state = data.get("state")
    if state is None:
        return None
    if state not in _STATES:
        raise ValidationError(f"{op}: state must be one of {', '.join(_STATES)}")
    return cast(EntityState, state)


def _operator(by: Any, op: str) -> str:
    if not isinstance(by, str) or not by.strip():
        raise ValidationError(f"{op}: `by` (the operator) must be a non-empty string")
    return by


def _entity_post(
    existing: Entity | None,
    etype: str,
    eid: str,
    name: str | None,
    attrs: dict[str, Any],
    state: EntityState | None,
    key: ExternalKey | None,
    source: Provenance,
    keep_name: bool,
) -> Entity:
    """The entity after this upsert. Latest wins by provenance time: an observation
    older than the entity's last update fills only attrs the entity lacks."""
    at = source["at"]
    if existing is None:
        e: Entity = {
            "type": etype,
            "id": eid,
            "attrs": _copy(attrs),
            "schemaVersion": SCHEMA_VERSION,
            "source": source,
            "createdAt": at,
            "updatedAt": at,
            "aliases": [name] if name else [],
            "state": state or "active",
        }
        if name:
            e["name"] = name
        if key:
            e["externalKey"] = key
        return e
    e = _copy(existing)
    if at >= existing["updatedAt"]:
        e["attrs"] = {**existing["attrs"], **_copy(attrs)}
        e["source"] = source
        e["updatedAt"] = at
        if state:
            e["state"] = state
        if name and not keep_name:
            e["name"] = name
    else:
        e["attrs"] = {**_copy(attrs), **existing["attrs"]}
    if name and "name" not in e:
        e["name"] = name
    e["createdAt"] = min(existing["createdAt"], at)
    if name and name not in e["aliases"]:
        e["aliases"].append(name)
    if key and "externalKey" not in e:
        e["externalKey"] = key
    return e


def _context_post(
    existing: Context | None,
    ctype: str,
    cid: str,
    attrs: dict[str, Any],
    parent: str | None,
    started: str | None,
    ended: str | None,
    key: ExternalKey | None,
    source: Provenance,
) -> Context:
    at = source["at"]
    if existing is None:
        c: Context = {
            "type": ctype,
            "id": cid,
            "attrs": _copy(attrs),
            "schemaVersion": SCHEMA_VERSION,
            "source": source,
            "createdAt": at,
            "updatedAt": at,
        }
    else:
        if existing["type"] != ctype:
            raise ValidationError(f"upsertContext: {cid} is a {existing['type']!r}, not {ctype!r}")
        c = _copy(existing)
        newer = at >= existing["updatedAt"]
        c["attrs"] = (
            {**existing["attrs"], **_copy(attrs)} if newer else {**_copy(attrs), **existing["attrs"]}
        )
        if newer:
            c["source"] = source
            c["updatedAt"] = at
        c["createdAt"] = min(existing["createdAt"], at)
    for k, val in (("parent", parent), ("startedAt", started), ("endedAt", ended)):
        if val is not None and (existing is None or at >= existing["updatedAt"] or k not in c):
            c[k] = val  # type: ignore[literal-required]
    if key and "externalKey" not in c:
        c["externalKey"] = key
    return c


def _valid_at(r: Relationship, at: str) -> bool:
    if at < r["validFrom"]:
        return False
    return "validTo" not in r or at < r["validTo"]


def _rank(kind: str, ranking: list[str]) -> int:
    return ranking.index(kind) if kind in ranking else len(ranking)


__all__ = [
    "FactsView",
    "GraphView",
    "SnapshotView",
    "TimelineView",
    "WorldModel",
    "normalizeName",
]
