"""Where exported rows are written: a SQLite file, or the Postgres that Zetaris reads.

Each write replaces one run's rows in every table inside one transaction, so a reader
sees the old rows or the new ones, never half of each, and writing the same run twice
changes nothing.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer import schema
from stemma.datalayer.errors import SinkError
from stemma.datalayer.export import Export, column_values
from stemma.datalayer.querylog import QueryLog, register_secret, scrub
from stemma.datalayer.safepath import check_sqlite_file


class Sink(Protocol):
    name: str

    def write(self, export: Export) -> dict[str, int]: ...


class SqliteSink:
    name = "sqlite"

    def __init__(self, path: Path, log: QueryLog, root: Path | None = None) -> None:
        self.path = path  # relative to `root` when given (host writes in a run folder)
        self.root = root
        self.log = log

    def resolved(self) -> Path:
        if self.root is not None:
            return check_sqlite_file(self.root, self.path.as_posix())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.resolved(), timeout=30)
        for ddl in schema.sqlite_ddl():
            con.execute(ddl)
        return con

    def write(self, export: Export) -> dict[str, int]:
        start = time.monotonic()
        try:
            con = self.connect()
            try:
                with con:
                    for t in schema.TABLES:
                        con.execute(f"DELETE FROM {t.name} WHERE run_id = ?", (export.run_id,))
                        cols = t.column_names
                        con.executemany(
                            f"INSERT INTO {t.name} ({', '.join(cols)}) VALUES "
                            f"({', '.join('?' for _ in cols)})",
                            column_values(export.rows[t.name], cols))
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise SinkError(f"SQLite {self.path}: {scrub(str(exc))}") from exc
        counts = export.counts()
        self.log.write(engine="sqlite", purpose="sync", run_id=export.run_id, rows=counts,
                       ms=round((time.monotonic() - start) * 1000))
        return counts


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    schema: str = "public"

    def __repr__(self) -> str:  # never print the password
        return (f"PostgresConfig(host={self.host!r}, port={self.port}, "
                f"database={self.database!r}, user={self.user!r}, schema={self.schema!r})")


# Logical invariants: what may be spliced into role DDL, which takes no parameters.
_PLAIN_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_PLAIN_SECRET = re.compile(r"[A-Za-z0-9]{16,128}")


class PostgresSink:
    """The database Zetaris registers as a datasource (pg8000, BSD-3-Clause)."""

    name = "postgres"

    def __init__(self, config: PostgresConfig, log: QueryLog) -> None:
        self.config = config
        self.log = log
        register_secret(config.password)

    def connect(self) -> Any:
        import pg8000.dbapi  # type: ignore[import-untyped]  # optional extra `datalayer`

        c = self.config
        try:
            return pg8000.dbapi.connect(host=c.host, port=c.port, database=c.database,
                                        user=c.user, password=c.password, timeout=30)
        except Exception as exc:
            raise SinkError(f"cannot connect to Postgres at {c.host}:{c.port}: "
                            f"{type(exc).__name__}") from exc

    def ensure_schema(self, con: Any) -> None:
        cur = con.cursor()
        for ddl in schema.postgres_ddl(self.config.schema):
            cur.execute(ddl)
        con.commit()

    def ensure_reader(self, reader: PostgresConfig) -> None:
        """The role Zetaris's datasource connects as: it can read the tables and nothing else.

        Created by this (owning) connection; DDL takes no parameters, so the name and the
        password are restricted to letters and digits before they reach the text."""
        if not _PLAIN_NAME.fullmatch(reader.user) or not _PLAIN_SECRET.fullmatch(reader.password):
            raise SinkError("the reader role's name or password has characters other than "
                            "letters, digits and underscore")
        s, r = self.config.schema, reader.user
        con = self.connect()
        try:
            self.ensure_schema(con)
            cur = con.cursor()
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (r,))
            verb = "ALTER" if cur.fetchall() else "CREATE"
            cur.execute(f"{verb} ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        f"NOINHERIT PASSWORD '{reader.password}'")
            for stmt in (f"REVOKE ALL ON DATABASE {self.config.database} FROM PUBLIC",
                         f"REVOKE CREATE ON SCHEMA {s} FROM PUBLIC",
                         f"GRANT CONNECT ON DATABASE {self.config.database} TO {r}",
                         f"GRANT USAGE ON SCHEMA {s} TO {r}",
                         f"GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r}",
                         f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT SELECT ON TABLES TO {r}",
                         f"ALTER ROLE {r} SET default_transaction_read_only = on"):
                cur.execute(stmt)
            con.commit()
        except SinkError:
            raise
        except Exception as exc:
            con.rollback()
            raise SinkError(f"creating the reader role failed: {type(exc).__name__}") from exc
        finally:
            con.close()
        self.log.write(engine="postgres", purpose="reader_role", role=r)

    def write(self, export: Export) -> dict[str, int]:
        start = time.monotonic()
        con = self.connect()
        try:
            self.ensure_schema(con)
            cur = con.cursor()
            s = self.config.schema
            for t in schema.TABLES:
                cur.execute(f"DELETE FROM {s}.{t.name} WHERE run_id = %s", (export.run_id,))
                cols = t.column_names
                rows = column_values(export.rows[t.name], cols)
                if rows:
                    cur.executemany(
                        f"INSERT INTO {s}.{t.name} ({', '.join(cols)}) VALUES "
                        f"({', '.join('%s' for _ in cols)})", rows)
            con.commit()
        except SinkError:
            raise
        except Exception as exc:
            con.rollback()
            raise SinkError(f"Postgres write failed: {type(exc).__name__}: "
                            f"{scrub(str(exc))[:300]}") from exc
        finally:
            con.close()
        counts = export.counts()
        self.log.write(engine="postgres", purpose="sync", run_id=export.run_id, rows=counts,
                       ms=round((time.monotonic() - start) * 1000))
        return counts
