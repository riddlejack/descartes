"""Live tests against the local Postgres container and a running Zetaris.

Skipped unless their settings are present (they need running services, not the web):
- `live_postgres`: `STEMMA_PG_PASSWORD` (and the container from
  `hackathon/zetaris/compose.yaml`);
- `live_zetaris`: also `ZETARIS_API_KEY`.
Every role and schema a test creates is its own, under a fresh run id, and is dropped
at the end.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from stemma.datalayer import Settings, query_log, schema, sqlite_path, sync
from stemma.datalayer.engines import SqliteEngine, ZetarisEngine
from stemma.datalayer.errors import SinkError
from stemma.datalayer.export import Export, export_run
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.runspace import RunSpace
from stemma.datalayer.sinks import PostgresConfig, PostgresSink
from stemma.datalayer.values import EvidenceValues
from stemma.datalayer.zetaris import ZetarisClient

from .conftest import C_FARE, C_NEW, C_OLD, C_PASSENGERS, C_REVENUE, FakeRegister, claims

ALL = [C_REVENUE, C_PASSENGERS, C_FARE, C_OLD, C_NEW]
PG = pytest.mark.skipif(not os.environ.get("STEMMA_PG_PASSWORD"), reason="no local Postgres")


def _settings(mode: str) -> Settings:
    env = dict(os.environ)
    env["STEMMA_DATALAYER"] = mode
    return Settings.from_env(env)


def _fresh_run(tmp_path: Path, name: str) -> Path:
    run_id = f"live-{name}-{uuid.uuid4().hex[:8]}"
    run = tmp_path / run_id
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"run_id": run_id, "as_of": None}))
    return run


class Owner:
    """The owning connection, which drops every role and schema the test made."""

    def __init__(self, config: PostgresConfig) -> None:
        self.sink = PostgresSink(config, QueryLog())
        self.made: list[RunSpace] = []

    def export(self, tmp_path: Path, name: str) -> tuple[Export, RunSpace]:
        ex = export_run(_fresh_run(tmp_path, name), FakeRegister(claims()))
        space = RunSpace(ex.run_id)
        self.made.append(space)
        self.sink.write(ex)
        return ex, space

    def run(self, sql: str) -> None:
        con = self.sink.connect()
        try:
            con.cursor().execute(sql)
            con.commit()
        finally:
            con.close()

    def as_role(self, space: RunSpace, password: str) -> Any:
        c = self.sink.config
        return PostgresSink(PostgresConfig(c.host, c.port, c.database, space.role, password),
                            QueryLog()).connect()


@pytest.fixture
def owner() -> Iterator[Owner]:
    s = _settings("sqlite")
    assert s.postgres is not None
    o = Owner(s.postgres)
    yield o
    con = o.sink.connect()
    cur = con.cursor()
    for space in o.made:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (space.role,))
        if cur.fetchall():
            cur.execute(f"DROP OWNED BY {space.role}")
            cur.execute(f"DROP ROLE {space.role}")
        cur.execute(f"DROP SCHEMA IF EXISTS {space.schema} CASCADE")
    con.commit()
    con.close()


def _refused(con: Any, sql: str) -> bool:
    cur = con.cursor()
    try:
        cur.execute(sql)
    except Exception as exc:  # noqa: BLE001 - any refusal from Postgres counts
        con.rollback()
        return "permission denied" in str(exc) or "must be owner" in str(exc)
    con.rollback()
    return False


@pytest.mark.live_postgres
@PG
def test_postgres_holds_exactly_the_sqlite_rows(tmp_path: Path, owner: Owner) -> None:
    run = _fresh_run(tmp_path, "rows")
    reg = FakeRegister(claims())
    sync(run, _settings("sqlite"), reg)
    ex = export_run(run, reg)
    space = RunSpace(ex.run_id)
    owner.made.append(space)
    owner.sink.write(ex)
    con = owner.sink.connect()
    try:
        for t in schema.TABLES:
            if t.name == "sync_state":
                continue  # a new generation per export, by design
            cols, key = ", ".join(t.column_names), ", ".join(t.key)
            cur = con.cursor()
            cur.execute(f"SELECT {cols} FROM {space.schema}.{t.name} ORDER BY {key}")
            pg_rows = [tuple(r) for r in cur.fetchall()]
            lite = sqlite3.connect(sqlite_path(run))
            lite_rows = [tuple(r) for r in lite.execute(
                f"SELECT {cols} FROM {t.name} ORDER BY {key}")]
            lite.close()
            assert pg_rows == lite_rows, t.name
    finally:
        con.close()


@pytest.mark.live_postgres
@PG
def test_a_runs_role_reads_its_schema_and_nothing_else(tmp_path: Path, owner: Owner) -> None:
    _, mine = owner.export(tmp_path, "mine")
    _, theirs = owner.export(tmp_path, "theirs")
    password = owner.sink.create_run_role(mine)
    assert owner.sink.write_problems(mine) == []
    con = owner.as_role(mine, password)
    try:
        cur = con.cursor()
        cur.execute(f"SELECT count(*) FROM {mine.schema}.claim")
        assert cur.fetchall()[0][0] == 5
        con.rollback()
        for sql in (f"SELECT count(*) FROM {theirs.schema}.claim",
                    f"INSERT INTO {mine.schema}.claim (run_id, claim_id) VALUES ('x', 'y')",
                    f"UPDATE {mine.schema}.claim SET number = 0",
                    f"DELETE FROM {mine.schema}.claim",
                    f"TRUNCATE {mine.schema}.claim",
                    f"CREATE TABLE {mine.schema}.planted (x int)",
                    "CREATE TABLE public.planted (x int)",
                    "CREATE TEMP TABLE planted (x int)",
                    f"ALTER TABLE {mine.schema}.claim ADD COLUMN x int"):
            assert _refused(con, sql), sql
        cur = con.cursor()
        cur.execute(f"GRANT SELECT ON {mine.schema}.claim TO PUBLIC")  # a warning, no effect
        con.commit()
        cur = con.cursor()
        cur.execute("SET default_transaction_read_only = off")  # nothing rests on this
        assert _refused(con, f"INSERT INTO {mine.schema}.claim (run_id, claim_id) "
                             "VALUES ('x', 'y')")
    finally:
        con.close()
    check = owner.sink.connect()
    try:
        cur = check.cursor()
        cur.execute("SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE table_schema = %s AND grantee = 'PUBLIC'", (mine.schema,))
        assert cur.fetchall()[0][0] == 0  # the role could not share what it reads
    finally:
        check.close()


@pytest.mark.live_postgres
@PG
def test_an_existing_role_is_never_reused(owner: Owner) -> None:
    space = RunSpace(f"live-planted-{uuid.uuid4().hex}")
    owner.made.append(space)
    owner.run(f"CREATE ROLE {space.role} LOGIN SUPERUSER")  # planted ahead of the run
    with pytest.raises(SinkError, match="never reused"):
        owner.sink.create_run_role(space)
    assert "has superuser" in owner.sink.write_problems(space)


@pytest.mark.live_postgres
@PG
@pytest.mark.parametrize("grant,problem", [
    ("GRANT INSERT ON {s}.claim TO {r}", "can change a table"),
    ("GRANT CREATE ON SCHEMA {s} TO {r}", "can create in a schema"),
    ("ALTER TABLE {s}.claim OWNER TO {r}", "owns a relation"),
    ("GRANT pg_write_all_data TO {r}", "member of another role"),
    ("GRANT TEMPORARY ON DATABASE stemma TO {r}", "can create in the database"),
    ("GRANT SELECT ON ALL TABLES IN SCHEMA {o} TO {r}", "can read outside its schema"),
])
def test_any_capability_beyond_reading_is_found(tmp_path: Path, owner: Owner, grant: str,
                                                problem: str) -> None:
    _, space = owner.export(tmp_path, "w")
    _, other = owner.export(tmp_path, "o")
    owner.sink.create_run_role(space)
    owner.run(grant.format(s=space.schema, r=space.role, o=other.schema))
    assert problem in owner.sink.write_problems(space)


@pytest.mark.live_zetaris
@pytest.mark.skipif(not (os.environ.get("ZETARIS_API_KEY")
                         and os.environ.get("STEMMA_PG_PASSWORD")), reason="no Zetaris")
def test_invented_run_reads_back_through_zetaris(tmp_path: Path, owner: Owner) -> None:
    run = _fresh_run(tmp_path, "z")
    reg = FakeRegister(claims())
    s = _settings("zetaris")
    assert s.zetaris is not None
    run_id = json.loads((run / "run.json").read_text())["run_id"]
    owner.made.append(RunSpace(run_id))
    sync(run, s, reg)  # SQLite, the run's Postgres schema, and its Zetaris provisioning
    log = query_log(run)
    client = ZetarisClient(s.zetaris, log)
    engine = ZetarisEngine(client, s.namespace, RunSpace(run_id).model)
    caps = client.run(f"SELECT receipt_id, status FROM {engine.table('capture')} "
                      "ORDER BY receipt_id", ("receipt_id", "status"))
    assert caps == [("r-annual", "ok"), ("r-sec", "ok"), ("r-timeout", "timeout")]
    via_zetaris = EvidenceValues(engine, run_id).values(ALL)
    local = EvidenceValues(SqliteEngine(Path("datalayer/evidence.sqlite"), log, root=run),
                           run_id).values(ALL)
    assert [v.hex() for v in via_zetaris.values()] == [v.hex() for v in local.values()]
