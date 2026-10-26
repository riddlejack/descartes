"""Record shapes (World Model docs/aggregate-shapes.md), as the engine returns them.

Keys are camelCase because the same dicts cross the conformance bridge unchanged.
Every record is plain JSON data; callers get copies, never the engine's own state.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Required, TypedDict

EntityState = Literal["emerging", "active", "archived"]
ConflictPolicy = Literal["most-recent-wins", "highest-confidence-wins", "source-priority", "manual"]
EventKind = Literal[
    "upsertEntity", "upsertContext", "relate", "assertFact", "alias", "merge-proposal"
]


class Provenance(TypedDict, total=False):
    """Where a write came from. `kind` and `at` are required on every write."""

    kind: Required[str]
    at: Required[str]
    by: str
    url: str
    checksum: str
    runId: str


class ExternalKey(TypedDict):
    system: str
    id: str


class Entity(TypedDict):
    type: str
    id: str
    attrs: dict[str, Any]
    schemaVersion: int
    source: Provenance
    createdAt: str
    updatedAt: str
    aliases: list[str]
    state: EntityState
    name: NotRequired[str]
    externalKey: NotRequired[ExternalKey]


class Context(TypedDict):
    type: str
    id: str
    attrs: dict[str, Any]
    schemaVersion: int
    source: Provenance
    createdAt: str
    updatedAt: str
    parent: NotRequired[str]
    startedAt: NotRequired[str]
    endedAt: NotRequired[str]
    externalKey: NotRequired[ExternalKey]


# A bitemporal edge. `validTo` unset means open; a superseding edge closes it
# (`validTo` + `supersededBy`) and nothing is deleted. Functional form because
# `from` is a Python keyword.
Relationship = TypedDict(
    "Relationship",
    {
        "id": str,
        "from": str,
        "type": str,
        "to": str,
        "validFrom": str,
        "schemaVersion": int,
        "source": Provenance,
        "createdAt": str,
        "multiValued": bool,
        "context": NotRequired[str],
        "attrs": NotRequired[dict[str, Any]],
        "validTo": NotRequired[str],
        "supersededBy": NotRequired[str],
    },
)


class Fact(TypedDict):
    id: str
    about: str
    predicate: str
    value: Any
    confidence: float | None
    source: Provenance
    assertedAt: str
    context: NotRequired[str]
    supersededBy: NotRequired[str]


class Alias(TypedDict):
    aliasId: str
    primaryId: str
    mergedAt: str
    mergedBy: str
    reason: str


class MergeProposal(TypedDict):
    id: str
    leftId: str
    rightId: str
    leftName: str
    rightName: str
    similarity: float
    reason: str
    status: Literal["pending", "approved", "rejected"]
    proposedAt: str
    resolvedAt: NotRequired[str]
    resolvedBy: NotRequired[str]
    resolution: NotRequired[str]


class WorldEvent(TypedDict):
    id: str
    seq: int
    kind: EventKind
    payload: dict[str, Any]
    schemaVersion: int
    at: str
    tx: int


class AuditEntry(TypedDict):
    seq: int
    at: str
    kind: str
    actor: str
    summary: str
    eventIds: list[str]
    runId: NotRequired[str]


class Transaction(TypedDict):
    """One line of the log: one mutation, its audit fields and the events it appended."""

    tx: int
    at: str
    op: str
    actor: str
    summary: str
    events: list[WorldEvent]
    runId: NotRequired[str]


class IngestReport(TypedDict):
    runId: str
    processed: int
    skippedUnchanged: int
    eventsAppended: int
    changed: bool


# ---------------------------------------------------------------- write inputs
# The dict shapes the write methods accept (the runner sends exactly these).
# Unknown keys are refused, so a misspelt field fails loudly instead of vanishing.


class UpsertEntityInput(TypedDict, total=False):
    type: Required[str]
    source: Required[Provenance]
    name: str
    externalKey: ExternalKey
    attrs: dict[str, Any]
    state: EntityState


class UpsertContextInput(TypedDict, total=False):
    type: Required[str]
    source: Required[Provenance]
    externalKey: ExternalKey
    attrs: dict[str, Any]
    parent: str
    startedAt: str
    endedAt: str


RelateInput = TypedDict(
    "RelateInput",
    {
        "from": Required[str],
        "type": Required[str],
        "to": Required[str],
        "source": Required[Provenance],
        "context": str,
        "attrs": dict[str, Any],
        "validFrom": str,
        "validTo": str,
        "multiValued": bool,
    },
    total=False,
)


class AssertFactInput(TypedDict, total=False):
    about: Required[str]
    predicate: Required[str]
    value: Required[Any]
    source: Required[Provenance]
    confidence: float
    context: str


class MentionInput(TypedDict):
    type: str
    name: str


class IngestObservation(TypedDict, total=False):
    externalId: Required[str]
    title: Required[str]
    at: Required[str]
    source: Required[Provenance]
    type: str
    text: str
    entities: list[MentionInput]
