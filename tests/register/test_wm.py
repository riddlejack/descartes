"""Unit tests of the World Model engine's semantics (memory storage)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from stemma.register.wm import (
    ProposalStateError,
    ProvenanceError,
    UnknownRecordError,
    UnknownViewError,
    ValidationError,
    WorldModel,
    decide,
    entityIdFromName,
    nameSimilarity,
    normalizeName,
    sha256Hex,
)

CLOCK = datetime(2026, 5, 18, tzinfo=UTC)


def src(at: str = "2026-01-01T00:00:00.000Z", **extra: str) -> dict[str, Any]:
    return {"kind": "manual", "at": at, **extra}


def world(**opts: Any) -> WorldModel:
    return WorldModel({"storage": "memory", "namespace": "t", "clock": lambda: CLOCK, **opts})


OBSERVATIONS: list[dict[str, Any]] = [
    {
        "externalId": "a1",
        "title": "Rates held",
        "at": "2026-05-17T14:00:00.000Z",
        "source": {"kind": "rss", "at": "2026-05-17T14:00:00.000Z"},
        "entities": [{"type": "person", "name": "Ada Example"}, {"type": "org", "name": "Bank Q"}],
    },
    {
        "externalId": "a2",
        "title": "Patience signalled",
        "at": "2026-05-17T16:30:00.000Z",
        "source": {"kind": "rss", "at": "2026-05-17T16:30:00.000Z"},
        "entities": [{"type": "person", "name": "Ada Example"}],
    },
]


# ------------------------------------------------------------------ stable ids
def test_normalize_name_folds_accents_nbsp_and_zero_width() -> None:
    assert normalizeName("José\u00a0\u00a0García") == "jose garcia"
    assert normalizeName("  Zo\u200be  ") == "zoe"
    assert normalizeName("Acme Corp") != normalizeName("Acme Corporation")


def test_entity_id_from_name_is_the_spec_formula() -> None:
    expected = "ent_" + sha256Hex("person:jose garcia")[:16]
    assert entityIdFromName("person", "José García") == expected
    assert entityIdFromName("person", "x") != entityIdFromName("org", "x")


def test_external_key_gives_literal_id() -> None:
    w = world()
    e = w.upsertEntity(
        {"type": "account", "externalKey": {"system": "crm", "id": "A:1"}, "source": src()}
    )
    assert e["id"] == "account:crm:A:1"


# ------------------------------------------------------------------ merge bands
@pytest.mark.parametrize(
    ("similarity", "band"),
    [(0.85, "auto-merge"), (0.8499, "review"), (0.82, "review"), (0.8199, "new"), (1.0, "auto-merge")],
)
def test_decide_boundaries(similarity: float, band: str) -> None:
    assert decide(similarity) == band


def test_decide_refuses_nan_and_non_numbers() -> None:
    with pytest.raises(ValueError):
        decide(float("nan"))
    with pytest.raises(TypeError):
        decide("0.9")  # type: ignore[arg-type]


def test_near_duplicate_auto_merges_into_existing_entity() -> None:
    w = world()
    first = w.upsertEntity({"type": "person", "name": "Jerome Example", "source": src()})
    assert nameSimilarity("Jerome Example", "Jerome Exampl") >= 0.85
    merged = w.upsertEntity({"type": "person", "name": "Jerome Exampl", "source": src()})
    assert merged["id"] == first["id"]
    assert merged["name"] == "Jerome Example"  # the primary keeps its display name
    assert "Jerome Exampl" in merged["aliases"]
    alias = entityIdFromName("person", "Jerome Exampl")
    assert w.resolveAlias(alias) == first["id"]
    graph = w.view("graph").entity(alias)
    assert graph is not None and graph["id"] == first["id"]
    assert "auto-merged" in w.audit()[-1]["summary"]


def test_review_band_queues_and_operator_approves() -> None:
    w = world()
    a, b = "Jerome Example abc", "Jerome Example x"
    s = nameSimilarity(a, b)
    assert 0.82 <= s < 0.85
    left = w.upsertEntity({"type": "person", "name": a, "source": src()})
    right = w.upsertEntity({"type": "person", "name": b, "source": src()})
    assert right["id"] != left["id"]  # review band never auto-merges
    queue = w.mergeQueue("pending")
    assert len(queue) == 1 and queue[0]["leftId"] == right["id"]
    done = w.approveMerge(queue[0]["id"], "operator-1", "same person")
    assert done["status"] == "approved" and done["resolvedBy"] == "operator-1"
    assert done["resolvedAt"] == "2026-05-18T00:00:00.000Z"  # the injected clock
    assert w.resolveAlias(right["id"]) == left["id"]
    primary = w.view("graph").entity(left["id"])
    assert primary is not None and b in primary["aliases"]
    assert w.audit()[-1]["kind"] == "merge-approved" and w.audit()[-1]["actor"] == "operator-1"
    with pytest.raises(ProposalStateError):
        w.approveMerge(queue[0]["id"], "operator-1")


def test_rejected_pair_is_never_proposed_or_merged_again() -> None:
    w = world()
    w.upsertEntity({"type": "org", "name": "Jerome Example abc", "source": src()})
    w.upsertEntity({"type": "org", "name": "Jerome Example x", "source": src()})
    (p,) = w.mergeQueue()
    rejected = w.rejectMerge(p["id"], "operator-2", "different firms")
    assert rejected["status"] == "rejected"
    w.upsertEntity({"type": "org", "name": "Jerome Example x", "source": src("2026-02-01")})
    assert len(w.mergeQueue()) == 1
    with pytest.raises(UnknownRecordError):
        w.rejectMerge("merge_missing", "operator-2")
    with pytest.raises(ValidationError):
        w.approveMerge(p["id"], "")


def test_auto_merge_can_be_switched_off() -> None:
    w = world(resolver={"autoMergeAt": None})
    w.upsertEntity({"type": "person", "name": "Jerome Example", "source": src()})
    second = w.upsertEntity({"type": "person", "name": "Jerome Exampl", "source": src()})
    assert second["id"] == entityIdFromName("person", "Jerome Exampl")
    assert [p["status"] for p in w.mergeQueue()] == ["pending"]


def test_other_types_never_merge() -> None:
    w = world()
    a = w.upsertEntity({"type": "person", "name": "Same Name", "source": src()})
    b = w.upsertEntity({"type": "org", "name": "Same Name", "source": src()})
    assert a["id"] != b["id"] and w.mergeQueue() == []


# ------------------------------------------------------------------ provenance
@pytest.mark.parametrize(
    "source",
    [None, {}, {"kind": "manual"}, {"at": "2026-01-01T00:00:00Z"}, {"kind": "", "at": "2026-01-01"}],
)
def test_writes_without_provenance_are_rejected(source: Any) -> None:
    w = world()
    data: dict[str, Any] = {"type": "person", "name": "No Provenance"}
    if source is not None:
        data["source"] = source
    with pytest.raises(ProvenanceError):
        w.upsertEntity(data)
    assert w.events() == [] and w.audit() == []


def test_every_write_kind_needs_provenance() -> None:
    w = world()
    w.upsertEntity({"type": "x", "externalKey": {"system": "s", "id": "1"}, "source": src()})
    with pytest.raises(ProvenanceError):
        w.relate({"from": "x:s:1", "type": "self", "to": "x:s:1"})
    with pytest.raises(ProvenanceError):
        w.assertFact({"about": "x:s:1", "predicate": "p", "value": 1})
    with pytest.raises(ProvenanceError):
        w.upsertContext({"type": "season"})
    with pytest.raises(ProvenanceError):
        w.ingest([{"externalId": "o", "title": "t", "at": "2026-01-01"}])


def test_unknown_fields_and_naive_times_are_refused() -> None:
    w = world()
    with pytest.raises(ValidationError, match="unknown field"):
        w.upsertEntity({"type": "x", "name": "n", "nmae": "typo", "source": src()})
    with pytest.raises(ValidationError, match="unknown provenance"):
        w.upsertEntity({"type": "x", "name": "n", "source": {**src(), "who": "me"}})
    with pytest.raises(ValidationError, match="no time zone"):
        w.upsertEntity({"type": "x", "name": "n", "source": src("2026-01-01T10:00:00")})
    with pytest.raises(ValidationError, match="contain ':'"):
        w.upsertEntity({"type": "a:b", "name": "n", "source": src()})


def test_record_timestamps_come_from_provenance_not_the_clock() -> None:
    w = WorldModel({"storage": "memory"})  # wall clock: must not appear in records
    e = w.upsertEntity({"type": "x", "name": "n", "source": src("2001-02-03T04:05:06.789+02:00")})
    assert e["createdAt"] == e["updatedAt"] == "2001-02-03T02:05:06.789Z"
    assert w.audit()[0]["at"] == "2001-02-03T02:05:06.789Z"
    assert w.events()[0]["at"] == "2001-02-03T02:05:06.789Z"


# ------------------------------------------------------------------ log and audit
def test_every_mutation_audits_with_increasing_seq_and_noops_write_nothing() -> None:
    w = world()
    w.upsertEntity({"type": "x", "name": "Audit One", "source": src()})
    w.upsertEntity({"type": "x", "name": "Audit Two", "source": src("2026-01-02")})
    w.upsertEntity({"type": "x", "name": "Audit Two", "source": src("2026-01-02")})  # no-op
    audit = w.audit()
    assert [a["seq"] for a in audit] == [1, 2]
    assert [e["seq"] for e in w.events()] == [1, 2]
    assert all(a["eventIds"] for a in audit)


def test_state_is_a_fold_of_the_log() -> None:
    w = world()
    w.ingest(OBSERVATIONS)
    w.assertFact({"about": "item:ingest:a1", "predicate": "tone", "value": "calm", "source": src()})
    replay = world()
    for ev in w.events():
        assert ev["payload"]["id" if ev["kind"] != "alias" else "aliasId"]
    # a second engine reading the same committed lines holds the same state
    replay._log._lines = list(w._log._lines)  # type: ignore[attr-defined]
    assert replay.stateShapeCanonical() == w.stateShapeCanonical()
    assert replay.audit() == w.audit()


def test_returned_records_are_copies() -> None:
    w = world()
    e = w.upsertEntity({"type": "x", "name": "n", "attrs": {"k": [1]}, "source": src()})
    e["attrs"]["k"].append(2)
    got = w.view("graph").entity(e["id"])
    assert got is not None and got["attrs"] == {"k": [1]}


# ------------------------------------------------------------------ upsert merge rules
def test_latest_wins_by_provenance_time() -> None:
    w = world()
    w.upsertEntity({"type": "x", "name": "n", "attrs": {"a": 1}, "source": src("2026-01-02")})
    w.upsertEntity(
        {"type": "x", "name": "n", "attrs": {"a": 0, "b": 2}, "source": src("2026-01-01")}
    )
    e = w.view("graph").entity(entityIdFromName("x", "n"))
    assert e is not None
    assert e["attrs"] == {"a": 1, "b": 2}  # older write only fills gaps
    assert e["createdAt"] == "2026-01-01T00:00:00.000Z"
    assert e["updatedAt"] == "2026-01-02T00:00:00.000Z"


# ------------------------------------------------------------------ ingest
def test_ingest_twice_appends_nothing_and_reports_unchanged() -> None:
    w = world()
    first = w.ingest(OBSERVATIONS)
    assert first["changed"] is True and first["processed"] == 2
    shape, n_events, n_audit = w.stateShapeCanonical(), len(w.events()), len(w.audit())
    second = w.ingest(OBSERVATIONS)
    assert second["changed"] is False and second["eventsAppended"] == 0
    assert second["skippedUnchanged"] == 2 and second["runId"] == first["runId"]
    assert w.stateShapeCanonical() == shape
    assert (len(w.events()), len(w.audit())) == (n_events, n_audit)


def test_edited_batch_changes_shape_and_reconciles_mentions() -> None:
    w = world()
    w.ingest(OBSERVATIONS)
    shape = w.stateShapeCanonical()
    edited = [{**OBSERVATIONS[0], "title": "Rates held (edited)", "entities": OBSERVATIONS[1]["entities"]}]
    run = w.ingest(edited + OBSERVATIONS[1:])
    assert run["changed"] is True and run["processed"] == 1
    assert w.stateShapeCanonical() != shape
    g = w.view("graph")
    now = {e["id"] for e in g.neighborsOf("item:ingest:a1", {"via": "mentions", "direction": "out"})}
    assert now == {entityIdFromName("person", "Ada Example")}
    closed = [r for r in g.edges("item:ingest:a1", {"includeClosed": True}) if "validTo" in r]
    assert [r["to"] for r in closed] == [entityIdFromName("org", "Bank Q")]


def test_ingest_mentions_are_multi_valued() -> None:
    w = world()
    w.ingest(OBSERVATIONS[:1])
    out = w.view("graph").neighborsOf("item:ingest:a1", {"via": "mentions"})
    assert len(out) == 2


def test_ingest_is_atomic() -> None:
    w = world()
    bad = [OBSERVATIONS[0], {"externalId": "x", "title": "t", "at": "2026-01-01"}]
    with pytest.raises(ProvenanceError):
        w.ingest(bad)
    assert w.events() == []


# ------------------------------------------------------------------ bitemporal edges
def _teams(w: WorldModel) -> None:
    for kind, key, at in (("player", "p1", "2087"), ("team", "a", "2087"), ("team", "b", "2088"), ("team", "c", "2089")):
        w.upsertEntity(
            {"type": kind, "externalKey": {"system": "g", "id": key}, "source": src(f"{at}-01-01")}
        )


def test_superseding_edge_closes_old_one_and_point_in_time_queries() -> None:
    w = world()
    _teams(w)
    r1 = w.relate({"from": "player:g:p1", "type": "plays-for", "to": "team:g:a", "validFrom": "2087-01-01", "source": src("2087-01-01")})
    r2 = w.relate({"from": "player:g:p1", "type": "plays-for", "to": "team:g:b", "validFrom": "2088-03-01", "source": src("2088-03-01")})
    g = w.view("graph")
    old = g.relationship(r1["id"])
    assert old is not None
    assert old["validTo"] == "2088-03-01T00:00:00.000Z" and old["supersededBy"] == r2["id"]
    assert [e["id"] for e in g.neighborsOf("player:g:p1", {"via": "plays-for"})] == ["team:g:b"]
    at_2087 = g.neighborsOf("player:g:p1", {"via": "plays-for", "at": "2087-06-01"})
    assert [e["id"] for e in at_2087] == ["team:g:a"]
    assert [e["id"] for e in g.incoming("team:g:a", {"at": "2087-06-01"})] == ["player:g:p1"]
    assert len(w.stateShape()["relationships"]) == 2  # nothing deleted


def test_relating_the_same_edge_twice_is_a_noop() -> None:
    w = world()
    _teams(w)
    edge = {"from": "player:g:p1", "type": "plays-for", "to": "team:g:a", "source": src("2087-02-01")}
    w.relate(edge)
    n = len(w.events())
    w.relate({**edge, "source": src("2087-03-01")})
    assert len(w.events()) == n


def test_back_dated_edge_is_closed_by_the_later_one() -> None:
    w = world()
    _teams(w)
    later = w.relate({"from": "player:g:p1", "type": "plays-for", "to": "team:g:c", "validFrom": "2089-01-01", "source": src("2089-01-01")})
    early = w.relate({"from": "player:g:p1", "type": "plays-for", "to": "team:g:a", "validFrom": "2087-01-01", "source": src("2089-02-01")})
    assert early["validTo"] == "2089-01-01T00:00:00.000Z" and early["supersededBy"] == later["id"]
    current = w.view("graph").relationship(later["id"])
    assert current is not None and "validTo" not in current


def test_relate_refuses_unknown_endpoints_and_bad_intervals() -> None:
    w = world()
    _teams(w)
    with pytest.raises(UnknownRecordError):
        w.relate({"from": "player:g:p1", "type": "t", "to": "team:g:zzz", "source": src()})
    with pytest.raises(ValidationError):
        w.relate({"from": "player:g:p1", "type": "t", "to": "team:g:a", "validFrom": "2090-01-01", "validTo": "2089-01-01", "source": src()})


def test_contexts_scope_edges_and_include_parent_in_id() -> None:
    w = world()
    _teams(w)
    s145 = w.upsertContext({"type": "season", "externalKey": {"system": "g", "id": "145"}, "source": src()})
    s144 = w.upsertContext({"type": "season", "externalKey": {"system": "g", "id": "144"}, "source": src()})
    wk_a = w.upsertContext({"type": "week", "attrs": {"n": 14}, "parent": s145["id"], "source": src()})
    wk_b = w.upsertContext({"type": "week", "attrs": {"n": 14}, "parent": s144["id"], "source": src()})
    assert wk_a["id"] != wk_b["id"] and wk_a["id"].startswith("ctx_")
    w.relate({"from": "player:g:p1", "type": "plays-for", "to": "team:g:a", "context": s145["id"], "source": src()})
    w.relate({"from": "player:g:p1", "type": "plays-for", "to": "team:g:b", "context": s144["id"], "source": src()})
    g = w.view("graph")
    # different contexts: neither edge closes the other
    assert len(g.neighborsOf("player:g:p1", {"via": "plays-for"})) == 2
    assert [e["id"] for e in g.neighborsOf("team:g:a", {"inContext": s145["id"]})] == ["player:g:p1"]
    with pytest.raises(UnknownRecordError):
        w.upsertContext({"type": "week", "attrs": {}, "parent": "season:g:999", "source": src()})


# ------------------------------------------------------------------ facts
def test_fact_conflicts_most_recent_wins_and_nothing_is_lost() -> None:
    w = world()
    e = w.upsertEntity({"type": "claim", "externalKey": {"system": "reg", "id": "c1"}, "source": src()})
    f1 = w.assertFact({"about": e["id"], "predicate": "verdict", "value": "holds", "source": src("2026-01-02")})
    f2 = w.assertFact({"about": e["id"], "predicate": "verdict", "value": "not_supported", "source": src("2026-01-03")})
    facts = w.view("facts").about(e["id"])
    assert [f["id"] for f in facts] == [f1["id"], f2["id"]]
    assert facts[0]["supersededBy"] == f2["id"] and "supersededBy" not in facts[1]
    got = w.view("graph").entity(e["id"])
    assert got is not None and got["attrs"]["verdict"] == "not_supported"
    # an older fact arriving late loses
    f0 = w.assertFact({"about": e["id"], "predicate": "verdict", "value": "x", "source": src("2026-01-01")})
    assert f0["supersededBy"] == f2["id"]
    assert [f["id"] for f in w.view("facts").about(e["id"], {"current": True})] == [f2["id"]]


def test_fact_policies() -> None:
    w = world(conflictPolicy="highest-confidence-wins")
    e = w.upsertEntity({"type": "p", "name": "n", "source": src()})
    w.assertFact({"about": e["id"], "predicate": "t", "value": "A", "confidence": 0.9, "source": src("2026-01-01", url="a")})
    w.assertFact({"about": e["id"], "predicate": "t", "value": "B", "confidence": 0.4, "source": src("2026-01-02", url="b")})
    got = w.view("graph").entity(e["id"])
    assert got is not None and got["attrs"]["t"] == "A"
    with pytest.raises(ValidationError, match="needs `confidence`"):
        w.assertFact({"about": e["id"], "predicate": "t", "value": "C", "source": src("2026-01-03")})

    w2 = world(conflictPolicy="source-priority", sourcePriority=["api", "rss"])
    e2 = w2.upsertEntity({"type": "p", "name": "n", "source": src()})
    w2.assertFact({"about": e2["id"], "predicate": "t", "value": "api", "source": {"kind": "api", "at": "2026-01-01"}})
    w2.assertFact({"about": e2["id"], "predicate": "t", "value": "rss", "source": {"kind": "rss", "at": "2026-01-02"}})
    got2 = w2.view("graph").entity(e2["id"])
    assert got2 is not None and got2["attrs"]["t"] == "api"


def test_fact_about_unknown_entity_is_refused_and_duplicate_is_noop() -> None:
    w = world()
    with pytest.raises(UnknownRecordError):
        w.assertFact({"about": "nope", "predicate": "p", "value": 1, "source": src()})
    e = w.upsertEntity({"type": "p", "name": "n", "source": src()})
    fact = {"about": e["id"], "predicate": "p", "value": {"n": 1.0}, "source": src()}
    w.assertFact(fact)
    n = len(w.events())
    w.assertFact(fact)
    assert len(w.events()) == n


# ------------------------------------------------------------------ views
def test_snapshot_view_answers_point_in_time() -> None:
    w = world()
    w.upsertEntity({"type": "team", "externalKey": {"system": "g", "id": "a"}, "attrs": {"coach": "A"}, "source": src("2087-01-01")})
    w.upsertEntity({"type": "team", "externalKey": {"system": "g", "id": "a"}, "attrs": {"coach": "B"}, "source": src("2088-01-01")})
    snap = w.view("snapshot")
    then = snap.entityAt("team:g:a", "2087-06-01")
    assert then is not None and then["attrs"]["coach"] == "A"
    assert snap.entityAt("team:g:a", "2086-01-01") is None


def test_query_traverse_timeline_and_unknown_view() -> None:
    w = world()
    for name, at in (("Gen One", "2026-01-01"), ("Gen Two", "2026-02-01"), ("Gen Three", "2026-03-01")):
        w.upsertEntity({"type": "person", "name": name, "attrs": {"line": "x"}, "source": src(at)})
    one, two, three = (entityIdFromName("person", n) for n in ("Gen One", "Gen Two", "Gen Three"))
    w.relate({"from": three, "type": "child-of", "to": two, "multiValued": True, "source": src()})
    w.relate({"from": two, "type": "child-of", "to": one, "multiValued": True, "source": src()})
    g = w.view("graph")
    assert [e["id"] for e in g.traverse(three, {"via": "child-of", "direction": "ancestors"})] == [two, one]
    assert len(g.query({"type": "person", "attrs": {"line": "x"}})) == 3
    assert len(g.query({"where": lambda e: e["name"].endswith("Two")})) == 1
    t = w.view("timeline").entitiesOfType("person", {"from": "2026-01-15", "to": "2026-02-15"})
    assert [e["id"] for e in t] == [two]
    with pytest.raises(UnknownViewError):
        w.view("nope")


def test_state_shape_is_json_and_canonical() -> None:
    w = world()
    w.ingest(OBSERVATIONS)
    shape = w.stateShape()
    assert json.loads(json.dumps(shape)) == shape
    assert w.stateShapeCanonical() == json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_options_are_validated() -> None:
    with pytest.raises(ValidationError):
        WorldModel({"storage": "local"})
    with pytest.raises(ValidationError):
        WorldModel({"storage": "file"})
    with pytest.raises(ValidationError):
        WorldModel({"conflictPolicy": "loudest"})
    with pytest.raises(ValidationError):
        WorldModel({"storge": "memory"})
