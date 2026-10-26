"""`query_data`: one read-only query, scoped to the run, logged; hostile inputs refused."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import stemma.datalayer as dl
from stemma.datalayer import Settings, query_log, sqlite_path
from stemma.datalayer import query as q
from stemma.datalayer.query import QueryRefused, check, run_query
from stemma.datalayer.tools import TOOLS, query_data, register_tools

from .conftest import C_NEW, C_PASSENGERS, C_REVENUE, RUN_ID, FakeRegister
from .test_zetaris_path import FakeZetaris, _fake_db

SQLITE = Settings.from_env({})


def _q(run_dir: Path, register: FakeRegister, sql: str) -> dict[str, Any]:
    return run_query(run_dir, sql, SQLITE, register)


def test_a_plain_query(run_dir: Path, register: FakeRegister) -> None:
    out = _q(run_dir, register, "SELECT claim_id, number FROM claim WHERE value_kind = 'count' "
             "AND superseded_by IS NULL ORDER BY number")
    assert out["backend"] == "sqlite" and out["columns"] == ["claim_id", "number"]
    assert out["rows"] == [[C_NEW, 8.0], [C_PASSENGERS, 1920000.0]]
    assert out["truncated"] is False and "filed as a claim" in out["note"]


def test_joins_and_aggregates_with_ids(run_dir: Path, register: FakeRegister) -> None:
    out = _q(run_dir, register,
             "SELECT s.receipt_id, c.status, count(*) AS n FROM claim_support s "
             "JOIN capture c ON c.receipt_id = s.receipt_id GROUP BY s.receipt_id, c.status")
    assert out["rows"] == [["r-annual", "ok", 4]]


def test_the_agents_own_with_is_kept(run_dir: Path, register: FakeRegister) -> None:
    out = _q(run_dir, register, "WITH big AS (SELECT claim_id, number FROM claim "
             "WHERE number > 1000) SELECT claim_id FROM big ORDER BY claim_id")
    assert [r[0] for r in out["rows"]] == sorted([C_REVENUE, C_PASSENGERS])


def test_logged_with_text_backend_and_rows(run_dir: Path, register: FakeRegister) -> None:
    _q(run_dir, register, "SELECT claim_id FROM claim")
    lines = [json.loads(x) for x in query_log(run_dir).file.read_text().splitlines()]  # type: ignore[union-attr]
    (e,) = [x for x in lines if x.get("purpose") == "query_data"]
    assert e["backend"] == "sqlite" and e["rows"] == 5 and e["sql"] == "SELECT claim_id FROM claim"
    (load,) = [x for x in lines if x.get("purpose") == "query_data_load"]
    assert f"run_id = '{RUN_ID}'" in load["sql"] and load["rows"] == 5


def test_other_runs_rows_never_show(run_dir: Path, register: FakeRegister) -> None:
    _q(run_dir, register, "SELECT claim_id FROM claim")
    con = sqlite3.connect(sqlite_path(run_dir))
    with con:
        con.execute("INSERT INTO claim (run_id, claim_id, number, number_repr) "
                    "VALUES ('another-run', ?, 1.0, '1.0')", ("b" * 64,))
    con.close()
    monkey_sync = dl.sync
    try:
        dl.sync = lambda *a, **k: {}  # type: ignore[assignment]  # keep the planted row
        out = run_query(run_dir, "SELECT claim_id FROM claim", SQLITE, register)
    finally:
        dl.sync = monkey_sync
    assert "b" * 64 not in [r[0] for r in out["rows"]] and out["row_count"] == 5


@pytest.mark.parametrize("sql,match", [
    ("", "empty"),
    ("DELETE FROM claim", "only a SELECT"),
    ("UPDATE claim SET number = 0", "only a SELECT"),
    ("INSERT INTO claim (run_id, claim_id) VALUES ('a', 'b')", "only a SELECT"),
    ("DROP TABLE claim", "only a SELECT"),
    ("PRAGMA table_info(claim)", "only a SELECT"),
    ("ATTACH DATABASE '/tmp/x' AS x", "only a SELECT"),
    ("SELECT claim_id FROM claim; DROP TABLE claim", "semicolon"),
    ("SELECT claim_id FROM claim -- hidden", "comments"),
    ("SELECT claim_id /* x */ FROM claim", "comments"),
    ("SELECT name AS claim_id FROM sqlite_master", "not allowed: read sqlite_master"),
    ("SELECT claim_id, load_extension('x') FROM claim", "not allowed: function load_extension"),
    ("SELECT claim_id, random() FROM claim", "not allowed: function random"),
    ("SELECT claim_id, printf('%s', number) FROM claim", "not allowed: function printf"),
    ("SELECT claim_id, reflect('java.lang.Runtime', 'exec', 'x') FROM claim",
     "no such function: reflect"),
    ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT n AS claim_id FROM r",
     "recursive"),
    ("SELECT count(*) FROM claim", "must carry the receipt or claim ids"),
    ("SELECT 1 AS claim_id", "reads none"),
    ("SELECT claim_id FROM nowhere", "not a valid query"),
    ("SELECT x" + " " * 20_001 + "FROM claim", "longer than"),
])
def test_refused(sql: str, match: str) -> None:
    with pytest.raises(QueryRefused, match=match):
        check(sql)


def test_truncation_is_said(run_dir: Path, register: FakeRegister,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(q, "QUERY_ROWS", 2)
    out = _q(run_dir, register, "SELECT claim_id FROM claim ORDER BY claim_id")
    assert out["row_count"] == 2 and out["truncated"] is True


ZENV = {"STEMMA_DATALAYER": "zetaris", "ZETARIS_API_KEY": "key-not-real",
        "ZETARIS_REST_URL": "http://zetaris.invalid/api/v1.0", "STEMMA_PG_PASSWORD": "pw"}


def _sqlite_only_sync(run_dir: Path, settings: Settings | None = None,
                      claims: Any = None) -> dict[str, dict[str, int]]:
    return {"sqlite": dl.SqliteSink(dl.SQLITE_REL, query_log(run_dir), root=run_dir).write(
        dl.export_run(run_dir, claims))}


def _plant_other_run(db: Path) -> None:
    con = sqlite3.connect(db)
    with con:
        con.execute("INSERT INTO claim (run_id, claim_id, statement, number, number_repr) "
                    "VALUES ('another-run', ?, 'secret of another run', 9.0, '9.0')", ("c" * 64,))
    con.close()


@pytest.mark.parametrize("sql", [
    "SELECT run_id, claim_id FROM main.claim",
    "SELECT run_id, claim_id FROM claim",
    "SELECT c.run_id, c.claim_id FROM main.claim AS c WHERE c.run_id <> 'x'",
])
def test_no_text_reaches_another_runs_rows_on_zetaris(tmp_path: Path, run_dir: Path,
                                                      register: FakeRegister, sql: str,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "sync", _sqlite_only_sync)
    db = _fake_db(tmp_path, run_dir, register)
    _plant_other_run(db)
    fake = FakeZetaris(db)
    out = run_query(run_dir, sql, Settings.from_env(ZENV), register, fake)
    assert out["backend"] == "zetaris" and {r[0] for r in out["rows"]} == {RUN_ID}
    assert out["row_count"] == 5
    sent = [c["body"]["sql"] for c in fake.calls]
    expected = (f"SELECT {', '.join(dl.schema.BY_NAME['claim'].column_names)} FROM "
                f"lightning.metastore.stemma.evidence.claim WHERE run_id = '{RUN_ID}' "
                "ORDER BY run_id, claim_id")
    assert sent == [expected]  # only code-written text reaches Zetaris


def test_no_text_reaches_another_runs_rows_on_sqlite(run_dir: Path,
                                                     register: FakeRegister) -> None:
    _q(run_dir, register, "SELECT claim_id FROM claim")
    _plant_other_run(sqlite_path(run_dir))
    monkey_sync = dl.sync
    try:
        dl.sync = lambda *a, **k: {}  # type: ignore[assignment]  # keep the planted row
        out = run_query(run_dir, "SELECT run_id, claim_id FROM main.claim", SQLITE, register)
    finally:
        dl.sync = monkey_sync
    assert {r[0] for r in out["rows"]} == {RUN_ID} and out["row_count"] == 5


def test_zetaris_text_values_keep_their_types(run_dir: Path, register: FakeRegister,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "sync", _sqlite_only_sync)
    cols = dl.schema.BY_NAME["claim"].column_names
    row = {c: None for c in cols} | {"run_id": RUN_ID, "claim_id": C_NEW, "number": "8.0",
                                     "number_repr": "8.0", "value_kind": "count"}
    body = json.dumps({"data": [row]}).encode()
    out = run_query(run_dir, "SELECT claim_id, number FROM claim WHERE number > 7.5",
                    Settings.from_env(ZENV), register, FakeZetaris(body=body))
    assert out["rows"] == [[C_NEW, 8.0]]


def test_zetaris_unreachable_answers_from_sqlite_loudly(run_dir: Path, register: FakeRegister,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "sync", _sqlite_only_sync)
    out = run_query(run_dir, "SELECT claim_id FROM claim", Settings.from_env(ZENV), register,
                    FakeZetaris(unreachable=True))
    assert out["backend"] == "sqlite" and out["row_count"] == 5
    assert "Zetaris was unavailable" in out["note"]
    lines = [json.loads(x) for x in query_log(run_dir).file.read_text().splitlines()]  # type: ignore[union-attr]
    assert any(e.get("purpose") == "fallback" and e.get("level") == "loud" for e in lines)


def test_tool_returns_typed_errors(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> None:
    monkeypatch.delenv("STEMMA_RUN_DIR", raising=False)
    assert query_data("SELECT claim_id FROM claim")["error"] == "no_run_dir"
    monkeypatch.setenv("STEMMA_RUN_DIR", str(run_dir))
    assert query_data("DROP TABLE claim")["error"] == "query_refused"


def test_register_tools_adds_query_data() -> None:
    added: list[tuple[str, str]] = []

    class Server:
        def add_tool(self, fn: Any, name: str, description: str) -> None:
            added.append((name, description))

    register_tools(Server())
    assert [n for n, _ in added] == ["query_data"] and len(TOOLS) == 1
    assert "claim_support(claim_id, n, receipt_id, quote)" in added[0][1]
