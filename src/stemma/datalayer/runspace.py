"""Per-run names in the shared Postgres and in Zetaris.

Each run's tables live in their own Postgres schema, read by their own role, served by
their own Zetaris datasource and semantic layer, so nothing a query says can reach
another run's rows. Names come from a hash of the run id, so they are always plain
identifiers whatever the run id holds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class RunSpace:
    run_id: str

    @property
    def key(self) -> str:
        # Logical invariant: 16 hex digits (64 bits) keep names unique across runs and
        # inside Postgres's 63-character identifier limit.
        return "r" + hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()[:16]

    @property
    def schema(self) -> str:
        return f"ev_{self.key}"

    @property
    def role(self) -> str:
        return f"stemma_ro_{self.key}"

    @property
    def datasource(self) -> str:
        return f"stemma_{self.key}"

    @property
    def model(self) -> str:
        return f"evidence_{self.key}"
