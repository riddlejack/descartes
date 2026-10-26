"""`query_data(sql)`: read-only SQL over the run's evidence tables (CONTRACTS section 5a).

An agent's SQL is checked before it reaches any engine, by preparing it in SQLite
against an empty copy of the schema with an authorizer that allows only reading these
tables and calling a short list of plain functions. So only one SELECT, in the subset
SQLite and Zetaris's Spark SQL share, gets through: no writes, no other tables, no
functions that reach files, Java or the network, no recursion, no comments. Its result
must carry receipt or claim ids, so every row says where it came from.

The checked SQL then runs scoped to this run: each table it reads is replaced, by a
leading `WITH`, with that table's rows for this run only, because the shared Postgres
behind Zetaris holds several runs and no run may see another's discovery. It runs on
Zetaris when configured (falling back to SQLite, logged, when Zetaris cannot be
reached) and on SQLite otherwise. Every query is logged with its text, backend and row
count. A figure taken from a row is still filed as a claim with its own support.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stemma.datalayer import schema
from stemma.datalayer.engines import SqliteEngine, ZetarisEngine
from stemma.datalayer.errors import DataLayerError, QueryError, SinkError, ZetarisUnavailable
from stemma.datalayer.export import ClaimSource
from stemma.datalayer.zetaris import Transport, ZetarisClient

if TYPE_CHECKING:
    from stemma.datalayer import Settings

# Resource limits: rows returned per call, and the length of one SQL text.
QUERY_ROWS = 500
MAX_SQL_CHARS = 20_000

# Logical invariant: functions both engines have and that only compute on values.
ALLOWED_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "ifnull", "instr", "length", "like", "lower", "ltrim",
    "max", "min", "nullif", "replace", "round", "rtrim", "substr", "sum", "trim", "upper",
})
# Logical invariant: the columns that name where a row came from.
ID_COLUMNS = frozenset({"receipt_id", "source_receipt_id", "sha256", "claim_id",
                        "input_claim_id", "old_claim_id", "new_claim_id"})
TABLE_NAMES = frozenset(t.name for t in schema.TABLES)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
NOTE = "A figure taken from these rows is still filed as a claim with its own support."


class QueryRefused(DataLayerError):
    """The SQL is not one read-only query over the evidence tables."""

    code = "query_refused"


@dataclass(frozen=True)
class Checked:
    sql: str
    tables: frozenset[str]
    columns: tuple[str, ...]


_ACTION_NAMES = {v: k for k, v in vars(sqlite3).items()
                 if k.startswith("SQLITE_") and isinstance(v, int) and k not in (
                     "SQLITE_OK", "SQLITE_DENY", "SQLITE_IGNORE")}


def check(sql: str) -> Checked:
    s = sql.strip() if isinstance(sql, str) else ""
    if not s:
        raise QueryRefused("the query is empty")
    if len(s) > MAX_SQL_CHARS:
        raise QueryRefused(f"the query is longer than {MAX_SQL_CHARS} characters")
    if ";" in s or "--" in s or "/*" in s:
        raise QueryRefused("send one statement with no semicolon and no comments")
    if not re.match(r"(?i)(select|with)\b", s):
        raise QueryRefused("only a SELECT (optionally starting WITH) is allowed")
    if re.match(r"(?i)with\s+recursive\b", s):
        raise QueryRefused("recursive queries are not allowed")
    reads: set[str] = set()
    denied: list[str] = []

    def authorize(action: int, a1: str | None, a2: str | None, db: str | None,
                  trigger: str | None) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and a1 in TABLE_NAMES and db in ("main", None):
            reads.add(a1)
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (a2 or "").lower() in ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        what = _ACTION_NAMES.get(action, str(action)).removeprefix("SQLITE_").lower()
        denied.append(f"{what} {a2 if action == sqlite3.SQLITE_FUNCTION else a1}")
        return sqlite3.SQLITE_DENY

    con = sqlite3.connect(":memory:")
    try:
        for ddl in schema.sqlite_ddl():
            con.execute(ddl)
        con.set_authorizer(authorize)
        try:
            cur = con.execute(s)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            if denied:
                raise QueryRefused(f"not allowed: {denied[0]}") from exc
            raise QueryRefused(f"not a valid query over the evidence tables: {exc}") from exc
        columns = tuple(d[0] for d in cur.description or ())
    finally:
        con.close()
    if not reads:
        raise QueryRefused("the query reads none of the evidence tables")
    if not any(c.lower() in ID_COLUMNS for c in columns):
        raise QueryRefused("each row must carry the receipt or claim ids it came from: "
                           f"select one of {', '.join(sorted(ID_COLUMNS))}")
    return Checked(s, frozenset(reads), columns)


def scoped(checked: Checked, table: Any, run_id: str) -> str:
    """The checked SQL with each table it reads limited to this run's rows."""
    if not _RUN_ID.fullmatch(run_id):
        raise QueryRefused(f"{run_id!r} is not a run id")
    ctes = ", ".join(f"{t} AS (SELECT * FROM {table(t)} WHERE run_id = '{run_id}')"
                     for t in sorted(checked.tables))
    m = re.match(r"(?i)with\b", checked.sql)
    if m:
        return f"WITH {ctes}, {checked.sql[m.end():].lstrip()}"
    return f"WITH {ctes} {checked.sql}"


def _shape(columns: tuple[str, ...], rows: list[tuple[Any, ...]], backend: str,
           note: str = NOTE) -> dict[str, Any]:
    truncated = len(rows) > QUERY_ROWS
    kept = rows[:QUERY_ROWS]
    return {"backend": backend, "columns": list(columns), "rows": [list(r) for r in kept],
            "row_count": len(kept), "truncated": truncated, "note": note}


def run_query(run_dir: Path, sql: str, settings: Settings | None = None,
              claims: ClaimSource | None = None,
              transport: Transport | None = None) -> dict[str, Any]:
    """`claims` defaults to the run's register; `transport` to real HTTP (tests pass fakes)."""
    from stemma.datalayer import SQLITE_REL, Settings, query_log, sync
    from stemma.datalayer.export import read_run

    s = settings or Settings.from_env()
    log = query_log(run_dir)
    checked = check(sql)
    run_id, _ = read_run(run_dir)
    local = SqliteEngine(SQLITE_REL, log, root=run_dir)
    try:
        sync(run_dir, s, claims)
        zetaris_ok = s.mode == "zetaris"
    except (SinkError, ZetarisUnavailable) as exc:
        if s.mode != "zetaris":
            raise
        log.write(engine="zetaris", purpose="fallback", fallback_to="sqlite",
                  level="loud", reason=str(exc))
        zetaris_ok = False
    if zetaris_ok:
        assert s.zetaris is not None  # logical invariant: checked in Settings.from_env
        client = (ZetarisClient(s.zetaris, log) if transport is None
                  else ZetarisClient(s.zetaris, log, transport))
        engine = ZetarisEngine(client, s.namespace, s.model)
        text = scoped(checked, engine.table, run_id)
        try:
            rows = engine.client.run_page(text, checked.columns, QUERY_ROWS + 1,
                                          purpose="query_data", backend="zetaris")
            return _shape(checked.columns, rows, "zetaris")
        except ZetarisUnavailable as exc:
            log.write(engine="zetaris", purpose="fallback", fallback_to="sqlite",
                      level="loud", reason=str(exc))
    text = scoped(checked, local.table, run_id)
    cols, rows = local.query_page(text, QUERY_ROWS + 1, purpose="query_data",
                                  backend="sqlite")
    note = NOTE if s.mode == "sqlite" else "Zetaris was unavailable; answered from SQLite. " + NOTE
    return _shape(cols, rows, "sqlite", note)


__all__ = ["QUERY_ROWS", "Checked", "QueryError", "QueryRefused", "check", "run_query",
           "scoped"]
