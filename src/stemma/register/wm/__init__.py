"""The Meterless World Model engine, in Python, clean-room from its specification.

`WorldModel`, `entityIdFromName`, `normalizeName` and `decide` are the conformance
surface; their camelCase names are the spec's, because World Model's own runner
calls them unchanged through a bridge that holds no logic (see `conformance/`).
The claim register imports this same module.
"""

from .errors import (
    LogCorruptError,
    ProposalStateError,
    ProvenanceError,
    StorageError,
    UnknownRecordError,
    UnknownViewError,
    ValidationError,
    WorldModelError,
)
from .ids import (
    canonicalJson,
    contextIdFromAttrs,
    entityIdFromName,
    externalId,
    factId,
    normalizeName,
    relationshipId,
    sha256Hex,
)
from .model import FactsView, GraphView, SnapshotView, TimelineView, WorldModel
from .resolve import AUTO_MERGE_AT, REVIEW_LOWER, decide, levenshtein, nameSimilarity

__all__ = [
    "AUTO_MERGE_AT",
    "REVIEW_LOWER",
    "FactsView",
    "GraphView",
    "LogCorruptError",
    "ProposalStateError",
    "ProvenanceError",
    "SnapshotView",
    "StorageError",
    "TimelineView",
    "UnknownRecordError",
    "UnknownViewError",
    "ValidationError",
    "WorldModel",
    "WorldModelError",
    "canonicalJson",
    "contextIdFromAttrs",
    "decide",
    "entityIdFromName",
    "externalId",
    "factId",
    "levenshtein",
    "nameSimilarity",
    "normalizeName",
    "relationshipId",
    "sha256Hex",
]
