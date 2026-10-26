"""The two read paths over the evidence-derived tables, behind one small interface.

Both run the same SQL text; only the table names differ (a SQLite table, or a table of
the Zetaris semantic layer). So the equality of the two paths is a property of the
data, not of two hand-written queries that might drift apart.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer.errors import QueryError
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.safepath import check_sqlite_file
from stemma.datalayer.zetaris import ZetarisClient


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

    def query(self, sql: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
        start = time.monotonic()
        path = self.resolved()
        if not path.is_file():
            self.log.write(engine=self.name, purpose="query", sql=sql, ok=False,
                           error="no SQLite file")
            raise QueryError(f"no evidence tables at {path}; sync the run first")
        try:
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                  timeout=30)
            try:
                rows = [tuple(r) for r in con.execute(sql).fetchall()]
            finally:
                con.close()
        except sqlite3.Error as exc:
            self.log.write(engine=self.name, purpose="query", sql=sql, ok=False, error=str(exc))
            raise QueryError(f"SQLite: {exc}") from exc
        self.log.write(engine=self.name, purpose="query", sql=sql, ok=True, rows=len(rows),
                       ms=round((time.monotonic() - start) * 1000))
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
