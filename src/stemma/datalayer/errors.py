"""Typed errors of the data layer. Each says what happened; none suggests a way round."""

from __future__ import annotations


class DataLayerError(Exception):
    """Base class: something in the data layer failed."""

    code = "datalayer_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")
        self.detail = detail


class ExportError(DataLayerError):
    """A run folder could not be turned into rows (a missing or unreadable file)."""

    code = "export_failed"


class SinkError(DataLayerError):
    """Rows could not be written to SQLite or Postgres."""

    code = "sink_failed"


class QueryError(DataLayerError):
    """A query failed, or its answer had an unexpected shape."""

    code = "query_failed"


class ZetarisUnavailable(QueryError):
    """Zetaris could not be reached, or refused the credentials."""

    code = "zetaris_unavailable"


class ValueMismatch(QueryError):
    """A stored number and its exact text form disagree: the copy is not faithful."""

    code = "value_mismatch"
