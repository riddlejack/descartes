"""The data layer: the evidence-derived tables, kept in SQLite and served by Zetaris.

What a run's evidence holds as records (the capture manifest, the claim register with
its verdicts and corrections, as-filed SEC facts, tables found in captured documents)
is copied into one set of tables (`schema`). They live in two places:
- a SQLite file in the run folder, always: the path that needs nothing installed;
- a Postgres database that Zetaris registers as a datasource and serves as one
  Unified Semantic Layer, when `STEMMA_DATALAYER=zetaris`.
Recorded calculation scripts read claim values through either path with one interface
(`EvidenceValues`, part B's `ClaimValues`). Every query is logged with its text and
row count in `<run>/datalayer/queries.jsonl`.

Settings (environment; names only in `.env.example`, values never in the repo):
- `STEMMA_DATALAYER`: `sqlite` (default) or `zetaris`;
- `STEMMA_DATALAYER_FALLBACK`: `sqlite` (default; logged) or `none`;
- `ZETARIS_REST_URL`, `ZETARIS_API_KEY`, `ZETARIS_ORG_ID`;
- `STEMMA_ZETARIS_NAMESPACE`, `STEMMA_ZETARIS_MODEL`, `STEMMA_ZETARIS_DATASOURCE`;
- `STEMMA_PG_HOST`, `STEMMA_PG_PORT`, `STEMMA_PG_DB`, `STEMMA_PG_USER`,
  `STEMMA_PG_PASSWORD`, and `STEMMA_PG_JDBC_HOST` (the host name as Zetaris sees it).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from stemma.datalayer.engines import SqliteEngine, ZetarisEngine
from stemma.datalayer.errors import DataLayerError
from stemma.datalayer.export import ClaimSource, export_run, read_run
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.sinks import PostgresConfig, PostgresSink, SqliteSink
from stemma.datalayer.values import EvidenceValues, FallbackValues
from stemma.datalayer.zetaris import DEFAULT_REST_URL, ZetarisClient, ZetarisConfig

__all__ = ["DataLayerError", "InputMismatch", "Settings", "input_lookup", "query_log",
           "sqlite_path", "sync", "values_backend"]

DATALAYER_DIR = "datalayer"


def sqlite_path(run_dir: Path) -> Path:
    return run_dir / DATALAYER_DIR / "evidence.sqlite"


def query_log(run_dir: Path) -> QueryLog:
    return QueryLog(run_dir / DATALAYER_DIR / "queries.jsonl")


@dataclass(frozen=True)
class Settings:
    mode: str
    fallback: str
    zetaris: ZetarisConfig | None
    postgres: PostgresConfig | None
    namespace: str
    model: str
    datasource: str
    jdbc_host: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        mode = e.get("STEMMA_DATALAYER", "sqlite")
        fallback = e.get("STEMMA_DATALAYER_FALLBACK", "sqlite")
        if mode not in ("sqlite", "zetaris"):
            raise DataLayerError(f"STEMMA_DATALAYER must be sqlite or zetaris, not {mode!r}")
        if fallback not in ("sqlite", "none"):
            raise DataLayerError(f"STEMMA_DATALAYER_FALLBACK must be sqlite or none, "
                                 f"not {fallback!r}")
        zcfg = None
        if e.get("ZETARIS_API_KEY"):
            zcfg = ZetarisConfig(e.get("ZETARIS_REST_URL", DEFAULT_REST_URL),
                                 e["ZETARIS_API_KEY"], e.get("ZETARIS_ORG_ID", "1"))
        pcfg = None
        if e.get("STEMMA_PG_PASSWORD"):
            pcfg = PostgresConfig(e.get("STEMMA_PG_HOST", "127.0.0.1"),
                                  int(e.get("STEMMA_PG_PORT", "5544")),
                                  e.get("STEMMA_PG_DB", "stemma"),
                                  e.get("STEMMA_PG_USER", "stemma"), e["STEMMA_PG_PASSWORD"])
        if mode == "zetaris" and (zcfg is None or pcfg is None):
            raise DataLayerError("STEMMA_DATALAYER=zetaris needs ZETARIS_API_KEY and "
                                 "STEMMA_PG_PASSWORD")
        return cls(mode, fallback, zcfg, pcfg,
                   e.get("STEMMA_ZETARIS_NAMESPACE", "lightning.metastore.stemma"),
                   e.get("STEMMA_ZETARIS_MODEL", "evidence"),
                   e.get("STEMMA_ZETARIS_DATASOURCE", "stemma_pg"),
                   e.get("STEMMA_PG_JDBC_HOST", "stemma-evidence-pg"))


def sync(run_dir: Path, settings: Settings | None = None,
         claims: ClaimSource | None = None) -> dict[str, dict[str, int]]:
    """Copy the run's evidence records into SQLite, and into Postgres in Zetaris mode.

    `claims` defaults to the run's own register."""
    s = settings or Settings.from_env()
    log = query_log(run_dir)
    export = export_run(run_dir, claims)
    for w in export.warnings:
        log.write(engine="export", purpose="warning", run_id=export.run_id, warning=w)
    out = {"sqlite": SqliteSink(sqlite_path(run_dir), log).write(export)}
    if s.mode == "zetaris":
        assert s.postgres is not None  # logical invariant: checked in Settings.from_env
        out["postgres"] = PostgresSink(s.postgres, log).write(export)
    return out


def values_backend(run_dir: Path, settings: Settings | None = None
                   ) -> EvidenceValues | FallbackValues:
    """The `ClaimValues` backend the settings choose, for `stemma.analysis.set_backend`."""
    s = settings or Settings.from_env()
    log = query_log(run_dir)
    run_id, _ = read_run(run_dir)
    local = EvidenceValues(SqliteEngine(sqlite_path(run_dir), log), run_id)
    if s.mode == "sqlite":
        return local
    assert s.zetaris is not None  # logical invariant: checked in Settings.from_env
    remote = EvidenceValues(ZetarisEngine(ZetarisClient(s.zetaris, log), s.namespace, s.model),
                            run_id)
    return remote if s.fallback == "none" else FallbackValues(remote, local, log)


InputLookup = Callable[[str], tuple[str, float]]


class InputMismatch(DataLayerError):
    """The data layer's number for a derivation input differs from the register's."""

    code = "input_mismatch"


def input_lookup(run_dir: Path, register_lookup: InputLookup,
                 settings: Settings | None = None,
                 claims: ClaimSource | None = None) -> InputLookup:
    """Part B's derivation-input lookup, with each number read through the data layer.

    For `file_claim` re-runs, called in the MCP server process (outside the sandbox).
    The register still resolves the id and owns the number; the data layer's copy is
    what the re-run receives, and it must equal the register's exactly, or the lookup
    fails loudly. So the numbers a script gets come from Zetaris queries, logged, and
    can never differ from what was filed. The run is synced once, on the first input.
    """
    s = settings or Settings.from_env()
    state: dict[str, EvidenceValues | FallbackValues] = {}

    def lookup(given: str) -> tuple[str, float]:
        cid, filed = register_lookup(given)
        if "backend" not in state:
            sync(run_dir, s, claims)
            state["backend"] = values_backend(run_dir, s)
        served = state["backend"].values([cid])[cid]
        if served != filed:
            raise InputMismatch(f"claim {cid[:12]}: the data layer gives {served!r}, "
                                f"the register {filed!r}")
        return cid, served

    return lookup
