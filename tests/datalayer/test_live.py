"""Live tests against the local Postgres container and a running Zetaris.

Skipped unless their settings are present (they need running services, not the web):
- `live_postgres`: `STEMMA_PG_PASSWORD` (and the container from
  `hackathon/zetaris/compose.yaml`);
- `live_zetaris`: also `ZETARIS_API_KEY`, with the semantic layer set up by
  `python -m stemma.datalayer setup-zetaris`.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from stemma.datalayer import Settings, query_log, schema, sqlite_path, sync
from stemma.datalayer.engines import SqliteEngine, ZetarisEngine
from stemma.datalayer.export import export_run
from stemma.datalayer.sinks import PostgresSink
from stemma.datalayer.values import EvidenceValues
from stemma.datalayer.zetaris import ZetarisClient

from .conftest import C_FARE, C_NEW, C_OLD, C_PASSENGERS, C_REVENUE, RUN_ID, FakeRegister

ALL = [C_REVENUE, C_PASSENGERS, C_FARE, C_OLD, C_NEW]


def _settings(mode: str) -> Settings:
    env = dict(os.environ)
    env["STEMMA_DATALAYER"] = mode
    return Settings.from_env(env)


def _table_rows_pg(sink: PostgresSink, table: schema.Table) -> list[tuple[object, ...]]:
    con = sink.connect()
    try:
        cur = con.cursor()
        cur.execute(f"SELECT {', '.join(table.column_names)} FROM {sink.config.schema}."
                    f"{table.name} WHERE run_id = %s ORDER BY {', '.join(table.key)}", (RUN_ID,))
        return [tuple(r) for r in cur.fetchall()]
    finally:
        con.close()


def _table_rows_sqlite(path: Path, table: schema.Table) -> list[tuple[object, ...]]:
    con = sqlite3.connect(path)
    try:
        return [tuple(r) for r in con.execute(
            f"SELECT {', '.join(table.column_names)} FROM {table.name} WHERE run_id = ? "
            f"ORDER BY {', '.join(table.key)}", (RUN_ID,))]
    finally:
        con.close()


@pytest.mark.live_postgres
@pytest.mark.skipif(not os.environ.get("STEMMA_PG_PASSWORD"), reason="no local Postgres")
def test_postgres_holds_exactly_the_sqlite_rows(run_dir: Path, register: FakeRegister) -> None:
    s = _settings("sqlite")
    assert s.postgres is not None
    sync(run_dir, s, register)
    sink = PostgresSink(s.postgres, query_log(run_dir))
    sink.write(export_run(run_dir, register))
    for table in schema.TABLES:
        assert _table_rows_pg(sink, table) == _table_rows_sqlite(sqlite_path(run_dir), table), \
            table.name


@pytest.mark.live_zetaris
@pytest.mark.skipif(not (os.environ.get("ZETARIS_API_KEY")
                         and os.environ.get("STEMMA_PG_PASSWORD")), reason="no Zetaris")
def test_invented_run_reads_back_through_zetaris(run_dir: Path, register: FakeRegister) -> None:
    s = _settings("zetaris")
    assert s.zetaris is not None
    sync(run_dir, s, register)  # SQLite and Postgres
    log = query_log(run_dir)
    client = ZetarisClient(s.zetaris, log)
    engine = ZetarisEngine(client, s.namespace, s.model)
    caps = client.run(f"SELECT receipt_id, status FROM {engine.table('capture')} "
                      f"WHERE run_id = '{RUN_ID}' ORDER BY receipt_id", ("receipt_id", "status"))
    assert caps == [("r-annual", "ok"), ("r-sec", "ok"), ("r-timeout", "timeout")]
    via_zetaris = EvidenceValues(engine, RUN_ID).values(ALL)
    local = EvidenceValues(SqliteEngine(sqlite_path(run_dir), log), RUN_ID).values(ALL)
    assert via_zetaris == local
    assert [v.hex() for v in via_zetaris.values()] == [v.hex() for v in local.values()]
    assert any(e.get("engine") == "zetaris" and e.get("rows") == 5 for e in log.entries)
