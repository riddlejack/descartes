"""The two read paths over the evidence-derived tables, behind one small interface.

Both run the same SQL text; only the table names differ (a SQLite table, or a table of
the Zetaris semantic layer). So the equality of the two paths is a property of the
data, not of two hand-written queries that might drift apart.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer.errors import QueryError
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.safepath import check_sqlite_file
from stemma.datalayer.zetaris import ROW_LIMIT, ZetarisClient


class Engine(Protocol):
    name: str

    def table(self, name: str) -> str: ...

    def query(self, sql: str, columns: Sequence[str]) -> list[tuple[Any, ...]]: ...


class SqliteEngine:
    name = "sqlite"

    def __init__(self, path: Path, log: QueryLog, root: Path | None = None) -> None:
        self.path = path  # relative to `root` when given
        self.root = root
        self.log = log

    def table(self, name: str) -> str:
        return f"main.{name}"

    def resolved(self) -> Path:
        if self.root is not None:
            return check_sqlite_file(self.root, self.path.as_posix())
        return self.path

    def query_page(self, sql: str, limit: int, purpose: str = "query", **extra: Any
                   ) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
        """Column names and at most `limit` rows, from a read-only connection."""
        start = time.monotonic()
        path = self.resolved()
        entry: dict[str, Any] = {"engine": self.name, "purpose": purpose, "sql": sql, **extra}
        if not path.is_file():
            self.log.write(**entry, ok=False, error="no SQLite file")
            raise QueryError(f"no evidence tables at {path}; sync the run first")
        try:
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            try:
                cur = con.execute(sql)
                cols = tuple(d[0] for d in cur.description or ())
                rows = [tuple(r) for r in cur.fetchmany(limit)]
            finally:
                con.close()
        except sqlite3.Error as exc:
            self.log.write(**entry, ok=False, error=str(exc))
            raise QueryError(f"SQLite: {exc}") from exc
        self.log.write(**entry, ok=True, rows=len(rows),
                       ms=round((time.monotonic() - start) * 1000))
        return cols, rows

    def query_many(self, sqls: Sequence[str], limit: int, purpose: str = "query",
                   between: Callable[[int], None] | None = None
                   ) -> list[list[tuple[Any, ...]]]:
        """Several reads inside one read transaction, so they see one state of the file.

        A writer cannot commit while the transaction is open (it waits, up to the
        connection's time limit), so every result comes from the same sync."""
        path = self.resolved()
        if not path.is_file():
            self.log.write(engine=self.name, purpose=purpose, ok=False, error="no SQLite file")
            raise QueryError(f"no evidence tables at {path}; sync the run first")
        out: list[list[tuple[Any, ...]]] = []
        try:
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30,
                                  isolation_level=None)
            try:
                con.execute("BEGIN")
                for i, sql in enumerate(sqls):
                    start = time.monotonic()
                    rows = [tuple(r) for r in con.execute(sql).fetchmany(limit)]
                    self.log.write(engine=self.name, purpose=purpose, sql=sql, ok=True,
                                   rows=len(rows), ms=round((time.monotonic() - start) * 1000))
                    out.append(rows)
                    if between is not None:
                        between(i)
                con.execute("COMMIT")
            finally:
                con.close()
        except sqlite3.Error as exc:
            self.log.write(engine=self.name, purpose=purpose, ok=False, error=str(exc))
            raise QueryError(f"SQLite: {exc}") from exc
        return out

    def query(self, sql: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
        _, rows = self.query_page(sql, ROW_LIMIT)
        if len(rows) >= ROW_LIMIT:
            raise QueryError(f"the answer reached the {ROW_LIMIT}-row limit and may be cut short")
        return rows


class ZetarisEngine:
    name = "zetaris"

    def __init__(self, client: ZetarisClient, namespace: str, model: str) -> None:
        self.client = client
        self.namespace = namespace
        self.model = model

    def table(self, name: str) -> str:
        return f"{self.namespace}.{self.model}.{name}"

    def query(self, sql: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
        return self.client.run(sql, columns)
