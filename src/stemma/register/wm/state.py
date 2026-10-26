"""State is a fold of the log.

Every event carries the full post-image of the one record it writes (or, for an
alias, the alias pointer), so folding needs no options and no clock: two
processes that read the same log hold the same state.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from .errors import LogCorruptError
from .records import (
    AuditEntry,
    Context,
    Entity,
    EventKind,
    Fact,
    MergeProposal,
    Relationship,
    WorldEvent,
)

EVENT_KINDS: frozenset[str] = frozenset(
    {"upsertEntity", "upsertContext", "relate", "assertFact", "alias", "merge-proposal"}
)
# Logical invariant: every event schema written by this engine is version 1.
SCHEMA_VERSION = 1
# Resource limit: alias chains longer than this mean a cycle in a corrupt log.
_MAX_ALIAS_HOPS = 10_000


class State:
    """The fold. Built only by `apply`, one committed transaction at a time."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.contexts: dict[str, Context] = {}
        self.relationships: dict[str, Relationship] = {}
        self.facts: dict[str, Fact] = {}
        self.aliases: dict[str, str] = {}
        self.proposals: dict[str, MergeProposal] = {}
        self.events: list[WorldEvent] = []
        self.audit: list[AuditEntry] = []
        self.tx = 0
        self.seq = 0

    def resolve(self, entity_id: str) -> str:
        return resolve_alias(entity_id, self.aliases.get)

    def apply(self, tx: dict[str, Any]) -> None:
        where = f"transaction {tx.get('tx')!r}"
        if tx.get("tx") != self.tx + 1:
            raise LogCorruptError(
                f"{where}: expected transaction {self.tx + 1} (a gap or a duplicate in the log)"
            )
        events = tx.get("events")
        if not isinstance(events, list) or not events:
            raise LogCorruptError(f"{where}: no events")
        for field in ("at", "op", "actor", "summary"):
            if not isinstance(tx.get(field), str):
                raise LogCorruptError(f"{where}: field `{field}` missing")
        seq = self.seq
        for ev in events:
            seq += 1
            if not isinstance(ev, dict) or ev.get("seq") != seq:
                raise LogCorruptError(f"{where}: expected event seq {seq}")
            if ev.get("kind") not in EVENT_KINDS or not isinstance(ev.get("payload"), dict):
                raise LogCorruptError(f"{where}: event {seq} has an unknown kind or no payload")
            keys = ("aliasId", "primaryId") if ev["kind"] == "alias" else ("id",)
            if not all(isinstance(ev["payload"].get(k), str) for k in keys):
                raise LogCorruptError(f"{where}: event {seq} payload lacks {'/'.join(keys)}")
            if not isinstance(ev.get("id"), str) or not isinstance(ev.get("at"), str):
                raise LogCorruptError(f"{where}: event {seq} lacks id or at")
        for ev in events:
            self._put(cast(EventKind, ev["kind"]), ev["payload"])
            self.events.append(cast(WorldEvent, ev))
        self.seq = seq
        self.tx = tx["tx"]
        entry: AuditEntry = {
            "seq": tx["tx"],
            "at": tx["at"],
            "kind": tx["op"],
            "actor": tx["actor"],
            "summary": tx["summary"],
            "eventIds": [ev["id"] for ev in events],
        }
        if isinstance(tx.get("runId"), str):
            entry["runId"] = tx["runId"]
        self.audit.append(entry)

    def _put(self, kind: EventKind, payload: dict[str, Any]) -> None:
        """Fold one validated event."""
        if kind == "alias":
            self.aliases[payload["aliasId"]] = payload["primaryId"]
            return
        record_id: str = payload["id"]
        if kind == "upsertEntity":
            self.entities[record_id] = cast(Entity, payload)
        elif kind == "upsertContext":
            self.contexts[record_id] = cast(Context, payload)
        elif kind == "relate":
            self.relationships[record_id] = cast(Relationship, payload)
        elif kind == "assertFact":
            self.facts[record_id] = cast(Fact, payload)
        else:
            self.proposals[record_id] = cast(MergeProposal, payload)


def resolve_alias(entity_id: str, lookup: Any) -> str:
    current = entity_id
    for _ in range(_MAX_ALIAS_HOPS):
        nxt = lookup(current)
        if nxt is None or nxt == current:
            return current
        current = nxt
    raise LogCorruptError(f"alias chain from {entity_id} does not end (a cycle)")


class Staging:
    """The events one mutation is about to append, layered over the current state.

    Reads see the staged post-images first, so a batch (an ingest) builds on its
    own earlier writes. Nothing reaches the state until the transaction is
    committed to the log and folded back.
    """

    def __init__(self, state: State) -> None:
        self.state = state
        self.events: list[tuple[EventKind, dict[str, Any]]] = []
        self._entities: dict[str, Entity] = {}
        self._contexts: dict[str, Context] = {}
        self._relationships: dict[str, Relationship] = {}
        self._facts: dict[str, Fact] = {}
        self._proposals: dict[str, MergeProposal] = {}
        self._aliases: dict[str, str] = {}

    # -------------------------------------------------------------- writes
    def put_entity(self, e: Entity) -> None:
        self._entities[e["id"]] = e
        self.events.append(("upsertEntity", dict(e)))

    def put_context(self, c: Context) -> None:
        self._contexts[c["id"]] = c
        self.events.append(("upsertContext", dict(c)))

    def put_relationship(self, r: Relationship) -> None:
        self._relationships[r["id"]] = r
        self.events.append(("relate", dict(r)))

    def put_fact(self, f: Fact) -> None:
        self._facts[f["id"]] = f
        self.events.append(("assertFact", dict(f)))

    def put_proposal(self, p: MergeProposal) -> None:
        self._proposals[p["id"]] = p
        self.events.append(("merge-proposal", dict(p)))

    def put_alias(self, alias_id: str, primary_id: str, payload: dict[str, Any]) -> None:
        self._aliases[alias_id] = primary_id
        self.events.append(("alias", payload))

    # --------------------------------------------------------------- reads
    def resolve(self, entity_id: str) -> str:
        return resolve_alias(
            entity_id, lambda k: self._aliases.get(k) or self.state.aliases.get(k)
        )

    def entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id) or self.state.entities.get(entity_id)

    def context(self, context_id: str) -> Context | None:
        return self._contexts.get(context_id) or self.state.contexts.get(context_id)

    def relationship(self, rel_id: str) -> Relationship | None:
        return self._relationships.get(rel_id) or self.state.relationships.get(rel_id)

    def fact(self, fact_id: str) -> Fact | None:
        return self._facts.get(fact_id) or self.state.facts.get(fact_id)

    def proposal(self, proposal_id: str) -> MergeProposal | None:
        return self._proposals.get(proposal_id) or self.state.proposals.get(proposal_id)

    def iter_entities(self) -> Iterator[Entity]:
        yield from _layered(self._entities, self.state.entities)

    def iter_relationships(self) -> Iterator[Relationship]:
        yield from _layered(self._relationships, self.state.relationships)

    def iter_facts(self) -> Iterator[Fact]:
        yield from _layered(self._facts, self.state.facts)


def _layered(top: dict[str, Any], base: dict[str, Any]) -> Iterator[Any]:
    for key, value in base.items():
        yield top.get(key, value)
    for key, value in top.items():
        if key not in base:
            yield value
