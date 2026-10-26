"""Claim values for recorded calculation scripts, read through a query engine.

`EvidenceValues` implements part B's `stemma.analysis.ClaimValues` interface
(`values(ids) -> {id: number}`) and behaves as B's register-backed reader does: ids
are normalised the same way, a prefix must name exactly one claim, a claim with no
number is an error, and the result is keyed by the ids as given. The only difference
is where the numbers come from: a query over the evidence-derived tables, in Zetaris
or in SQLite.

Each number is read back with its exact text form; if the two disagree the copy is
not faithful and the read fails, never rounds.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from stemma.datalayer.engines import Engine
from stemma.datalayer.errors import QueryError, ValueMismatch, ZetarisUnavailable
from stemma.datalayer.querylog import QueryLog

# Logical invariants: the shapes of a claim id (as part B's register accepts them) and
# of a run id; only these ever reach SQL text, so nothing else can be injected.
_CLAIM_ID = re.compile(r"[0-9a-f]{12,64}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

COLUMNS = ("claim_id", "number", "number_repr")


class ClaimValueError(QueryError):
    """A named claim is unknown, ambiguous or has no number."""

    code = "claim_value"


def normalise_claim_id(given: str) -> str:
    """The same normalisation as part B's `Register.resolve_claim_id`."""
    g = given.strip().lower()
    if g.startswith("claim:"):
        g = g.split(":", 1)[1]
    g = g.removeprefix("c:")
    if not _CLAIM_ID.fullmatch(g):
        raise ClaimValueError(f"{given!r} is not a claim id")
    return g


def values_sql(engine: Engine, run_id: str, prefixes: Sequence[str]) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise ClaimValueError(f"{run_id!r} is not a run id")
    for p in prefixes:
        if not _CLAIM_ID.fullmatch(p):
            raise ClaimValueError(f"{p!r} is not a claim id")
    match = " OR ".join(f"claim_id LIKE '{p}%'" for p in sorted(set(prefixes)))
    return (f"SELECT {', '.join(COLUMNS)} FROM {engine.table('claim')} "
            f"WHERE run_id = '{run_id}' AND ({match}) ORDER BY claim_id")


class EvidenceValues:
    def __init__(self, engine: Engine, run_id: str) -> None:
        self.engine = engine
        self.run_id = run_id

    def values(self, ids: Sequence[str]) -> dict[str, float]:
        if not ids:
            raise ClaimValueError("name the claim ids this script reads")
        wanted = {i: normalise_claim_id(i) for i in ids}
        rows = self.engine.query(values_sql(self.engine, self.run_id, list(wanted.values())),
                                 COLUMNS)
        out: dict[str, float] = {}
        for given, prefix in wanted.items():
            hits = [r for r in rows if str(r[0]).startswith(prefix)]
            if len(hits) != 1:
                raise ClaimValueError(f"no single claim has id {given}")
            _, number, number_repr = hits[0]
            if number is None or number_repr is None:
                raise ClaimValueError(f"claim {given} has no number")
            try:
                exact = float(str(number_repr))
                stored = float(number)
            except (TypeError, ValueError) as exc:
                raise ValueMismatch(f"claim {given}: unreadable number {number!r}") from exc
            if exact != stored:
                raise ValueMismatch(f"claim {given}: the engine gave {stored!r}, the claim "
                                    f"was filed as {number_repr}")
            out[given] = exact
        return out


class FallbackValues:
    """Zetaris first; SQLite only when Zetaris cannot be reached, and logged as such.

    The fallback changes no number: both paths read the same rows (shown by the
    equality test), and an answer Zetaris gives is never second-guessed by SQLite.
    """

    def __init__(self, primary: EvidenceValues, fallback: EvidenceValues, log: QueryLog) -> None:
        self.primary = primary
        self.fallback = fallback
        self.log = log

    def values(self, ids: Sequence[str]) -> dict[str, float]:
        try:
            return self.primary.values(ids)
        except ZetarisUnavailable as exc:
            self.log.write(engine=self.primary.engine.name, purpose="fallback",
                           fallback_to=self.fallback.engine.name, reason=str(exc))
            return self.fallback.values(ids)
