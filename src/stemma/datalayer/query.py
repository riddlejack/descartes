"""`query_data(sql)`: read-only SQL over the run's evidence tables (CONTRACTS section 5a).

Isolation is structural. The agent's SQL never reaches Zetaris, Postgres or the run's
SQLite file. For each call, code-written queries fetch this run's rows of the tables the
query reads (through Zetaris when configured, from the run's SQLite file otherwise, each
logged with its row count) into a fresh in-memory SQLite database that holds nothing
else; the agent's SQL then runs there, with `query_only` on. Whatever its text, it can
see only this run's rows, and it cannot write anything that outlives the call. Zetaris
receives only fixed SELECTs, through a datasource registered with a Postgres role that
can only read.

The SQL checks stay as a second line: one statement, no comments, no recursion, reads of
the evidence tables only, a short list of plain functions, and a result that carries
receipt or claim ids so every row says where it came from. Each call is logged with its
text, backend and row count. A figure taken from a row is still filed as a claim with its
own support.
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

# Resource limits: rows returned per call, the length of one SQL text, and the rows of
# one table loaded into a call's private copy.
QUERY_ROWS = 500
MAX_SQL_CHARS = 20_000
LOAD_ROWS = 200_000

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


def _authorizer(reads: set[str], denied: list[str]) -> Any:
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

    return authorize


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
    con = sqlite3.connect(":memory:")
    try:
        for ddl in schema.sqlite_ddl():
            con.execute(ddl)
        con.set_authorizer(_authorizer(reads, denied))
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


def _coerce(table: schema.Table, row: tuple[Any, ...]) -> tuple[Any, ...]:
    """A fetched row with each value in its column's type (Zetaris may send text)."""
    out: list[Any] = []
    for col, v in zip(table.columns, row, strict=True):
        if v is None or col.type == "text":
            out.append(None if v is None else str(v))
        elif col.type == "int":
            out.append(int(v) if not isinstance(v, str) else int(float(v)))
        else:
            out.append(float(v))
    return tuple(out)


def load_sql(table_name: str, qualified: str, run_id: str) -> str:
    """The code-written query that fetches one table's rows for this run."""
    if not _RUN_ID.fullmatch(run_id):
        raise QueryRefused(f"{run_id!r} is not a run id")
    t = schema.BY_NAME[table_name]
    return (f"SELECT {', '.join(t.column_names)} FROM {qualified} WHERE run_id = '{run_id}' "
            f"ORDER BY {', '.join(t.key)}")


def answer(checked: Checked, rows: dict[str, list[tuple[Any, ...]]]
           ) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
    """Run the checked SQL in a private in-memory database holding only `rows`."""
    con = sqlite3.connect(":memory:")
    try:
        for ddl in schema.sqlite_ddl():
            con.execute(ddl)
        with con:
            for name, data in rows.items():
                t = schema.BY_NAME[name]
                con.executemany(f"INSERT INTO {name} ({', '.join(t.column_names)}) VALUES "
                                f"({', '.join('?' for _ in t.columns)})",
                                [_coerce(t, r) for r in data])
        con.execute("PRAGMA query_only = ON")
        reads: set[str] = set()
        denied: list[str] = []
        con.set_authorizer(_authorizer(reads, denied))  # the second line, again
        try:
            cur = con.execute(checked.sql)
            cols = tuple(d[0] for d in cur.description or ())
            out = [tuple(r) for r in cur.fetchmany(QUERY_ROWS + 1)]
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise QueryRefused(f"not allowed: {denied[0]}" if denied else
                               f"the query failed: {exc}") from exc
    finally:
        con.close()
    return cols, out


def _shape(columns: tuple[str, ...], rows: list[tuple[Any, ...]], backend: str,
           note: str = NOTE) -> dict[str, Any]:
    truncated = len(rows) > QUERY_ROWS
    kept = rows[:QUERY_ROWS]
    return {"backend": backend, "columns": list(columns), "rows": [list(r) for r in kept],
            "row_count": len(kept), "truncated": truncated, "note": note}


def _checked_load(name: str, data: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    if len(data) > LOAD_ROWS:
        raise QueryError(f"{name} holds more than {LOAD_ROWS} rows for this run")
    return data


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
        use_zetaris = s.mode == "zetaris"
    except (SinkError, ZetarisUnavailable) as exc:
        if s.mode != "zetaris":
            raise
        log.write(engine="zetaris", purpose="fallback", fallback_to="sqlite", level="loud",
                  reason=str(exc))
        use_zetaris = False
    rows: dict[str, list[tuple[Any, ...]]] = {}
    backend = "sqlite"
    if use_zetaris:
        assert s.zetaris is not None  # logical invariant: checked in Settings.from_env
        client = (ZetarisClient(s.zetaris, log) if transport is None
                  else ZetarisClient(s.zetaris, log, transport))
        engine = ZetarisEngine(client, s.namespace, s.model)
        try:
            for name in sorted(checked.tables):
                cols = schema.BY_NAME[name].column_names
                rows[name] = _checked_load(name, client.run_page(
                    load_sql(name, engine.table(name), run_id), cols, LOAD_ROWS + 1,
                    purpose="query_data_load"))
            backend = "zetaris"
        except ZetarisUnavailable as exc:
            log.write(engine="zetaris", purpose="fallback", fallback_to="sqlite",
                      level="loud", reason=str(exc))
            rows = {}
    if backend == "sqlite":
        for name in sorted(checked.tables):
            _, data = local.query_page(load_sql(name, local.table(name), run_id),
                                       LOAD_ROWS + 1, purpose="query_data_load")
            rows[name] = _checked_load(name, data)
    cols_out, out = answer(checked, rows)
    log.write(engine="memory", purpose="query_data", backend=backend, sql=checked.sql,
              rows=min(len(out), QUERY_ROWS), loaded={k: len(v) for k, v in rows.items()})
    note = NOTE if backend == "zetaris" or s.mode == "sqlite" else (
        "Zetaris was unavailable; answered from SQLite. " + NOTE)
    return _shape(cols_out, out, backend, note)


__all__ = ["LOAD_ROWS", "QUERY_ROWS", "Checked", "QueryError", "QueryRefused", "answer",
           "check", "load_sql", "run_query"]
