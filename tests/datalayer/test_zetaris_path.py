"""The Zetaris path with a fake transport (no network), and its equality with SQLite.

The fake answers each query by running it on its own SQLite copy of the exported rows
and returning them as JSON, which is what Zetaris's REST endpoint does over Postgres.
It proves the client, the engine and the number handling; it does not prove Zetaris.
The same equality against a real Zetaris is `test_live.py`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from stemma.datalayer import Settings, query_log, schema, setup, sync, values_backend
from stemma.datalayer.engines import ZetarisEngine
from stemma.datalayer.errors import QueryError, ZetarisUnavailable
from stemma.datalayer.export import column_values, export_run
from stemma.datalayer.querylog import QueryLog, redact
from stemma.datalayer.sinks import PostgresConfig
from stemma.datalayer.values import EvidenceValues, FallbackValues
from stemma.datalayer.zetaris import ROW_LIMIT, ZetarisClient, ZetarisConfig

from .conftest import C_FARE, C_NEW, C_OLD, C_PASSENGERS, C_REVENUE, RUN_ID, FakeRegister

NS, MODEL = "lightning.metastore.stemma", "evidence"
CFG = ZetarisConfig("http://zetaris.invalid/api/v1.0", "key-not-real", "1")


class FakeZetaris:
    def __init__(self, rows_db: Path | None = None, status: int = 200,
                 body: bytes | None = None, unreachable: bool = False) -> None:
        self.rows_db = rows_db
        self.status = status
        self.body = body
        self.unreachable = unreachable
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes | None, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body) if body else None,
                           "headers": headers})
        if self.unreachable:
            raise ZetarisUnavailable(f"cannot reach {url}: connection refused")
        if self.body is not None:
            return self.status, self.body
        assert body is not None and self.rows_db is not None
        sql = re.sub(rf"{re.escape(NS)}\.{MODEL}\.", "", json.loads(body)["sql"])
        con = sqlite3.connect(self.rows_db)
        rows = [list(r) for r in con.execute(sql).fetchall()]
        con.close()
        return self.status, json.dumps({"data": rows}).encode()


def _fake_db(tmp_path: Path, run_dir: Path, register: FakeRegister) -> Path:
    db = tmp_path / "fake-zetaris-rows.sqlite"
    con = sqlite3.connect(db)
    ex = export_run(run_dir, register)
    with con:
        for ddl in schema.sqlite_ddl():
            con.execute(ddl)
        for t in schema.TABLES:
            cols = t.column_names
            con.executemany(f"INSERT INTO {t.name} VALUES ({', '.join('?' for _ in cols)})",
                            column_values(ex.rows[t.name], cols))
    con.close()
    return db


def _zetaris_values(fake: FakeZetaris, log: QueryLog) -> EvidenceValues:
    return EvidenceValues(ZetarisEngine(ZetarisClient(CFG, log, fake), NS, MODEL), RUN_ID)


ALL = [C_REVENUE, C_PASSENGERS, C_FARE, C_OLD, C_NEW]


def test_both_paths_give_the_same_numbers(tmp_path: Path, run_dir: Path,
                                          register: FakeRegister) -> None:
    sync(run_dir, Settings.from_env({}), register)
    local = values_backend(run_dir, Settings.from_env({}))
    remote = _zetaris_values(FakeZetaris(_fake_db(tmp_path, run_dir, register)),
                             query_log(run_dir))
    a, b = local.values(ALL), remote.values(ALL)
    assert a == b
    assert all(type(x) is float for x in b.values())
    assert [x.hex() for x in a.values()] == [x.hex() for x in b.values()]


def test_request_shape_and_headers(tmp_path: Path, run_dir: Path, register: FakeRegister) -> None:
    fake = FakeZetaris(_fake_db(tmp_path, run_dir, register))
    v = _zetaris_values(fake, QueryLog())
    v.values([C_REVENUE])
    v.values([C_REVENUE])
    first, second = fake.calls
    assert first["url"] == "http://zetaris.invalid/api/v1.0/sql-editor/sqls/run"
    h = first["headers"]
    assert h["Authorization"] == "Bearer key-not-real" and h["X-Org-ID"] == "1"
    assert uuid.UUID(h["X-Request-ID"]) and h["X-Request-ID"] != second["headers"]["X-Request-ID"]
    body = first["body"]
    assert set(body) == {"queryId", "sql", "source", "limit"} and body["limit"] == ROW_LIMIT
    assert body["sql"].startswith(f"SELECT claim_id, number, number_repr FROM {NS}.{MODEL}.claim")


def test_each_query_logged_with_text_and_row_count(tmp_path: Path, run_dir: Path,
                                                   register: FakeRegister) -> None:
    log = QueryLog(tmp_path / "q.jsonl")
    _zetaris_values(FakeZetaris(_fake_db(tmp_path, run_dir, register)), log).values(
        [C_REVENUE, C_NEW])
    (entry,) = [json.loads(line) for line in (tmp_path / "q.jsonl").read_text().splitlines()]
    assert entry["engine"] == "zetaris" and entry["ok"] is True and entry["rows"] == 2
    assert C_REVENUE in entry["sql"] and "query_id" in entry and "ms" in entry


def test_refused_key_is_unavailable_and_logged(tmp_path: Path) -> None:
    log = QueryLog()
    with pytest.raises(ZetarisUnavailable, match="401"):
        _zetaris_values(FakeZetaris(status=401, body=b"{}"), log).values([C_REVENUE])
    assert log.entries[-1]["ok"] is False


@pytest.mark.parametrize("status,body,match", [
    (500, b"engine crashed", "HTTP 500"),
    (200, b"<html>login</html>", "not JSON"),
    (200, b'{"message": "ok"}', "no row list"),
    (200, b'{"data": ["one", "two"]}', "neither a list"),
])
def test_bad_answers_are_typed_errors(status: int, body: bytes, match: str) -> None:
    with pytest.raises(QueryError, match=match):
        _zetaris_values(FakeZetaris(status=status, body=body), QueryLog()).values([C_REVENUE])


def test_rows_as_objects_are_read_by_column_name() -> None:
    body = json.dumps({"data": [{"NUMBER": 8.0, "claim_id": C_NEW, "number_repr": "8.0"}]})
    got = _zetaris_values(FakeZetaris(body=body.encode()), QueryLog()).values([C_NEW])
    assert got == {C_NEW: 8.0}


def test_a_full_page_is_an_error_not_a_partial_answer() -> None:
    rows = [[f"{i:064x}", 1.0, "1.0"] for i in range(ROW_LIMIT)]
    body = json.dumps({"data": rows}).encode()
    with pytest.raises(QueryError, match="row limit"):
        _zetaris_values(FakeZetaris(body=body), QueryLog()).values([C_NEW])


def test_number_text_mismatch_from_zetaris_is_caught() -> None:
    body = json.dumps({"data": [[C_NEW, 8.000001, "8.0"]]}).encode()
    with pytest.raises(QueryError, match="filed as 8.0"):
        _zetaris_values(FakeZetaris(body=body), QueryLog()).values([C_NEW])


def test_fallback_only_when_unreachable_and_logged(tmp_path: Path, run_dir: Path,
                                                   register: FakeRegister) -> None:
    sync(run_dir, Settings.from_env({}), register)
    log = QueryLog()
    local = values_backend(run_dir, Settings.from_env({}))
    assert isinstance(local, EvidenceValues)
    down = FallbackValues(_zetaris_values(FakeZetaris(unreachable=True), log), local, log)
    assert down.values([C_FARE]) == local.values([C_FARE])
    assert any(e.get("purpose") == "fallback" for e in log.entries)
    broken = FallbackValues(_zetaris_values(FakeZetaris(status=500, body=b"x"), log), local, log)
    with pytest.raises(QueryError, match="HTTP 500"):
        broken.values([C_FARE])  # an error from Zetaris is never papered over


def test_passwords_never_reach_the_log() -> None:
    pg = PostgresConfig("h", 5432, "stemma", "stemma", "s3cret-Value!")
    stmt = setup.statements(pg, "stemma-evidence-pg", "stemma_pg", NS, MODEL)[0].sql
    assert "s3cret-Value!" in stmt
    assert "s3cret-Value!" not in redact(stmt) and 'password "***"' in redact(stmt)
    assert "s3cret" not in repr(pg) and "key-not-real" not in repr(CFG)


def test_semantic_layer_statements() -> None:
    pg = PostgresConfig("h", 5432, "stemma", "stemma", "pw")
    stmts = setup.statements(pg, "stemma-evidence-pg", "stemma_pg", NS, MODEL)
    compile_ = next(s.sql for s in stmts if s.purpose == "usl_compile")
    assert compile_.startswith(f"COMPILE USL IF NOT EXISTS {MODEL} DEPLOY NAMESPACE {NS} DDL")
    assert compile_.count("CREATE TABLE") == len(schema.TABLES)
    assert "FOREIGN KEY (run_id, receipt_id) REFERENCES capture(run_id, receipt_id)" in compile_
    activates = [s.sql for s in stmts if s.purpose == "usl_activate"]
    assert len(activates) == len(schema.TABLES)
    assert activates[0].startswith(f"ACTIVATE USL TABLE {NS}.{MODEL}.capture AS SELECT run_id")
    assert "jdbc:postgresql://stemma-evidence-pg:5432/stemma" in stmts[0].sql
