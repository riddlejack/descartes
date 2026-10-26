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

from stemma.datalayer import Settings, query_log, schema, setup, sqlite_path, sync
from stemma.datalayer.engines import SqliteEngine, ZetarisEngine
from stemma.datalayer.errors import SinkError
from stemma.datalayer.export import Export, export_run
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.runspace import RunSpace
from stemma.datalayer.sinks import PostgresConfig, PostgresSink
from stemma.datalayer.values import EvidenceValues
from stemma.datalayer.zetaris import ZetarisClient

from .conftest import (
    C_FARE,
    C_NEW,
    C_OLD,
    C_PASSENGERS,
    C_REVENUE,
    FakeRegister,
    claims,
    make_run,
)

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

    def run(self, sql: str, database: str | None = None) -> None:
        con = self.sink.connect(database)
        con.autocommit = True
        try:
            con.cursor().execute(sql)
        finally:
            con.close()

    def drop_database(self, space: RunSpace) -> None:
        con = self.sink.connect()
        con.autocommit = True
        cur = con.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s", (space.database,))
        cur.execute(f"DROP DATABASE IF EXISTS {space.database}")
        con.close()

    def drop_role(self, space: RunSpace) -> None:
        con = self.sink.connect()
        con.autocommit = True
        cur = con.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (space.role,))
        if cur.fetchall():
            cur.execute(f"DROP OWNED BY {space.role}")
            cur.execute(f"DROP ROLE {space.role}")
        con.close()

    def as_role(self, space: RunSpace, password: str, database: str | None = None) -> Any:
        c = self.sink.config
        return PostgresSink(PostgresConfig(c.host, c.port, c.database, space.role, password),
                            QueryLog()).connect(database or space.database)


@pytest.fixture
def owner() -> Iterator[Owner]:
    s = _settings("sqlite")
    assert s.postgres is not None
    o = Owner(s.postgres)
    yield o
    for space in o.made:  # databases first: a role's grants on them go with them
        o.drop_database(space)
    for space in o.made:
        o.drop_role(space)


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
    con = owner.sink.connect(space.database)
    try:
        for t in schema.TABLES:
            if t.name == "sync_state":
                continue  # a new generation per export, by design
            cols, key = ", ".join(t.column_names), ", ".join(t.key)
            cur = con.cursor()
            cur.execute(f"SELECT {cols} FROM public.{t.name} ORDER BY {key}")
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
def test_a_runs_role_reads_its_database_and_nothing_else(tmp_path: Path,
                                                         owner: Owner) -> None:
    _, mine = owner.export(tmp_path, "mine")
    _, theirs = owner.export(tmp_path, "theirs")
    password = owner.sink.create_run_role(mine)
    assert owner.sink.write_problems(mine) == []
    with pytest.raises(SinkError, match="cannot connect"):
        owner.as_role(mine, password, theirs.database)  # another run's database
    with pytest.raises(SinkError, match="cannot connect"):
        owner.as_role(mine, password, "postgres")
    con = owner.as_role(mine, password)
    try:
        cur = con.cursor()
        cur.execute("SELECT count(*) FROM public.claim")
        assert cur.fetchall()[0][0] == 5
        con.rollback()
        for sql in ("INSERT INTO public.claim (run_id, claim_id) VALUES ('x', 'y')",
                    "UPDATE public.claim SET number = 0",
                    "DELETE FROM public.claim",
                    "TRUNCATE public.claim",
                    "CREATE TABLE public.planted (x int)",
                    "CREATE SCHEMA planted",
                    "CREATE TEMP TABLE planted (x int)",
                    "ALTER TABLE public.claim ADD COLUMN x int"):
            assert _refused(con, sql), sql
        cur = con.cursor()
        cur.execute("GRANT SELECT ON public.claim TO PUBLIC")  # a warning, no effect
        con.commit()
        cur.execute("SET default_transaction_read_only = off")  # nothing rests on this
        assert _refused(con, "INSERT INTO public.claim (run_id, claim_id) VALUES ('x', 'y')")
    finally:
        con.close()
    check = owner.sink.connect(mine.database)
    try:
        cur = check.cursor()
        cur.execute("SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' AND grantee = 'PUBLIC'")
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
@pytest.mark.parametrize("grant,problem,inside", [
    ("GRANT INSERT ON public.claim TO {r}", "can change a table", True),
    ("GRANT CREATE ON SCHEMA public TO {r}", "can create in a schema", True),
    ("ALTER TABLE public.claim OWNER TO {r}", "owns a relation", True),
    ("GRANT pg_write_all_data TO {r}", "member of another role", False),
    ("GRANT TEMPORARY ON DATABASE {d} TO {r}", "can create in a database", False),
    ("GRANT CONNECT ON DATABASE {o} TO {r}", "can connect to another database", False),
    (("CREATE SCHEMA extra; CREATE TABLE extra.t (x int); GRANT USAGE ON SCHEMA extra TO {r}; "
     "GRANT SELECT ON extra.t TO {r}"), "can read outside its tables", True),
])
def test_any_capability_beyond_reading_is_found(tmp_path: Path, owner: Owner, grant: str,
                                                problem: str, inside: bool) -> None:
    _, space = owner.export(tmp_path, "w")
    _, other = owner.export(tmp_path, "o")
    owner.sink.create_run_role(space)
    for stmt in grant.format(r=space.role, d=space.database, o=other.database).split("; "):
        owner.run(stmt, space.database if inside else None)
    assert problem in owner.sink.write_problems(space)


ZETARIS = pytest.mark.skipif(not (os.environ.get("ZETARIS_API_KEY")
                                 and os.environ.get("STEMMA_PG_PASSWORD")), reason="no Zetaris")


@pytest.mark.live_zetaris
@ZETARIS
def test_invented_run_round_trip_through_zetaris(tmp_path: Path, owner: Owner,
                                                 request: pytest.FixtureRequest) -> None:
    from stemma.datalayer import input_lookup
    from stemma.datalayer.query import run_query

    run = make_run(tmp_path)  # the invented run: receipts, claims, verdicts, a correction
    reg = FakeRegister(claims())
    s = _settings("zetaris")
    assert s.zetaris is not None
    run_id = json.loads((run / "run.json").read_text())["run_id"]
    space = RunSpace(run_id)
    owner.made.append(space)
    zclean = ZetarisClient(s.zetaris, QueryLog())
    setup.deprovision(space, zclean, s.namespace)  # the fixed invented run id, from before
    owner.drop_database(space)
    owner.drop_role(space)
    request.addfinalizer(lambda: setup.deprovision(space, zclean, s.namespace))
    sync(run, s, reg)  # SQLite, the run's Postgres database, and its Zetaris provisioning
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
    out = run_query(run, "SELECT v.claim_id, v.n, v.verdict, c.superseded_by FROM "
                    "claim_verdict v JOIN claim c ON c.claim_id = v.claim_id "
                    "ORDER BY v.claim_id, v.n", s, reg)
    assert out["backend"] == "zetaris"
    assert sorted((r[0], r[2]) for r in out["rows"]) == sorted(
        (c, h["verdict"]) for c in reg.records for h in reg.records[c]["verdict_history"])
    alerts: list[dict[str, Any]] = []
    lookup = input_lookup(run, lambda g: (g, float(reg.records[g]["value"]["number"])), s,
                          reg, alerts.append)
    for cid in ALL:
        got = lookup(cid)
        assert got[1].hex() == float(reg.records[cid]["value"]["number"]).hex()
    assert alerts == []
    zlines = [e for e in log.entries if e.get("engine") == "zetaris" and e.get("ok")]
    assert zlines and all("rows" in e and "sql" in e for e in zlines)
