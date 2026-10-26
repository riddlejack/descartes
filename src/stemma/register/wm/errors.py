"""Typed errors of the World Model engine.

Every refusal is one of these, and each message says what happened. None of them
suggests a way round the check that raised it.
"""

from __future__ import annotations


class WorldModelError(Exception):
    """Base class: anything the engine refuses or cannot do."""


class ProvenanceError(WorldModelError):
    """A write arrived without provenance (`source` with `kind` and `at`)."""


class ValidationError(WorldModelError):
    """A write's input does not have the shape the engine accepts."""


class UnknownRecordError(WorldModelError):
    """A write or read names an entity, context, fact or proposal that does not exist."""


class UnknownViewError(WorldModelError):
    """`view(name)` was asked for a view the engine does not have."""


class ProposalStateError(WorldModelError):
    """A merge proposal was approved or rejected when it is no longer pending."""


class StorageError(WorldModelError):
    """The log could not be opened, locked, read or appended."""


class LogCorruptError(StorageError):
    """A committed line of the log is not a valid transaction, or the sequence has a gap."""
