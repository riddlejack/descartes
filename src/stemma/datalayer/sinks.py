"""Where exported rows are written: a SQLite file, or the Postgres that Zetaris reads.

Each write replaces one run's rows in every table inside one transaction, so a reader
sees the old rows or the new ones, never half of each, and writing the same run twice
changes nothing. In Postgres each run has its own schema, read by its own fresh role
that can read that schema and nothing else (checked, not assumed).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer import schema
from stemma.datalayer.errors import SinkError
from stemma.datalayer.export import Export, column_values
from stemma.datalayer.querylog import QueryLog, register_secret, scrub
from stemma.datalayer.runspace import RunSpace
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

    def __repr__(self) -> str:  # never print the password
        return (f"PostgresConfig(host={self.host!r}, port={self.port}, "
                f"database={self.database!r}, user={self.user!r})")


# Logical invariants: what may be spliced into DDL, which takes no parameters.
_PLAIN_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_PLAIN_SECRET = re.compile(r"[A-Za-z0-9]{32}")
_ALPHABET = string.ascii_letters + string.digits


def new_password() -> str:
    pw = "".join(secrets.choice(_ALPHABET) for _ in range(32))
    register_secret(pw)
    return pw


class PostgresSink:
    """The database Zetaris registers, one schema per run (pg8000, BSD-3-Clause)."""

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

    @staticmethod
    def _plain(*names: str) -> None:
        for n in names:
            if not _PLAIN_NAME.fullmatch(n):
                raise SinkError(f"{n!r} is not a plain identifier")

    def ensure_schema(self, con: Any, space: RunSpace) -> None:
        self._plain(space.schema)
        cur = con.cursor()
        for ddl in schema.postgres_ddl(space.schema):
            cur.execute(ddl)
        con.commit()

    def _run(self, what: str, fn: Any) -> Any:
        con = self.connect()
        try:
            out = fn(con, con.cursor())
            con.commit()
            return out
        except SinkError:
            con.rollback()
            raise
        except Exception as exc:
            con.rollback()
            raise SinkError(f"{what} failed: {type(exc).__name__}: "
                            f"{scrub(str(exc))[:300]}") from exc
        finally:
            con.close()

    def role_exists(self, space: RunSpace) -> bool:
        def go(con: Any, cur: Any) -> bool:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (space.role,))
            return bool(cur.fetchall())
        result: bool = self._run("looking up the run's role", go)
        return result

    def create_run_role(self, space: RunSpace) -> str:
        """A fresh login role that can read this run's schema and nothing else.

        Never reuses a role: if one of that name exists, this refuses. Returns its
        password, which goes only into Zetaris's datasource."""
        self._plain(space.role, space.schema, self.config.database)
        pw = new_password()
        db, r, sch = self.config.database, space.role, space.schema

        def go(con: Any, cur: Any) -> None:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (r,))
            if cur.fetchall():
                raise SinkError(f"role {r} already exists; a run's role is never reused")
            self.ensure_schema(con, space)
            cur = con.cursor()
            cur.execute("SELECT datname FROM pg_database WHERE datallowconn")
            others = [str(row[0]) for row in cur.fetchall()]
            self._plain(*others)
            for stmt in (
                (f"CREATE ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                 f"NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 PASSWORD '{pw}'"),
                *(f"REVOKE ALL ON DATABASE {d} FROM PUBLIC" for d in others),
                "REVOKE ALL ON SCHEMA public FROM PUBLIC",
                f"GRANT CONNECT ON DATABASE {db} TO {r}",
                f"GRANT USAGE ON SCHEMA {sch} TO {r}",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO {r}",
            ):
                cur.execute(stmt)

        self._run("creating the run's role", go)
        self.log.write(engine="postgres", purpose="run_role", role=r, schema=sch)
        return pw

    def reset_password(self, space: RunSpace) -> str:
        """A new password for this run's own role (one this run created, per its record)."""
        self._plain(space.role)
        pw = new_password()

        def go(con: Any, cur: Any) -> None:
            cur.execute(f"ALTER ROLE {space.role} PASSWORD '{pw}'")

        self._run("resetting the run's role password", go)
        return pw

    def write_problems(self, space: RunSpace) -> list[str]:
        """Everything the run's role could do beyond reading its own schema; empty if none."""
        r, sch = space.role, space.schema

        def go(con: Any, cur: Any) -> list[str]:
            found: list[str] = []
            cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
                        "rolbypassrls, rolinherit FROM pg_roles WHERE rolname = %s", (r,))
            rows = cur.fetchall()
            if not rows:
                return [f"role {r} does not exist"]
            names = ("superuser", "createrole", "createdb", "replication", "bypassrls",
                     "inherit")
            found += [f"has {n}" for n, flag in zip(names, rows[0], strict=True) if flag]
            checks = (
                ("member of another role", ("SELECT count(*) FROM pg_auth_members m JOIN "
                 "pg_roles x ON x.oid = m.member WHERE x.rolname = %s"), (r,)),
                ("owns a relation", ("SELECT count(*) FROM pg_class WHERE relowner = "
                 "(SELECT oid FROM pg_roles WHERE rolname = %s)"), (r,)),
                ("owns a schema", ("SELECT count(*) FROM pg_namespace WHERE nspowner = "
                 "(SELECT oid FROM pg_roles WHERE rolname = %s)"), (r,)),
                ("owns a database", ("SELECT count(*) FROM pg_database WHERE datdba = "
                 "(SELECT oid FROM pg_roles WHERE rolname = %s)"), (r,)),
                # pg_catalog.pg_settings is the documented interface to SET: updating it
                # changes the session's settings, never stored data.
                ("can change a table", ("SELECT count(*) FROM pg_class c WHERE c.relkind IN "
                 "('r','p','v','m','f') AND c.oid <> 'pg_catalog.pg_settings'::regclass "
                 "AND has_table_privilege(%s, c.oid, "
                 "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')"), (r,)),
                ("can connect to another database", ("SELECT count(*) FROM pg_database "
                 "WHERE datallowconn AND datname <> current_database() AND "
                 "has_database_privilege(%s, oid, 'CONNECT')"), (r,)),
                ("can read outside its schema", ("SELECT count(*) FROM pg_class c JOIN "
                 "pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN "
                 "('r','p','v','m','f') AND n.nspname !~ '^pg_' AND n.nspname <> "
                 "'information_schema' AND n.nspname <> %s AND "
                 "has_table_privilege(%s, c.oid, 'SELECT')"), (sch, r)),
                ("can create in a schema", ("SELECT count(*) FROM pg_namespace WHERE "
                 "has_schema_privilege(%s, oid, 'CREATE')"), (r,)),
                ("can create in the database", ("SELECT count(*) FROM pg_database WHERE "
                 "has_database_privilege(%s, oid, 'CREATE, TEMPORARY')"), (r,)),
                ("can change a sequence", ("SELECT count(*) FROM pg_class c WHERE "
                 "CASE WHEN c.relkind = 'S' THEN has_sequence_privilege(%s, c.oid, 'UPDATE') "
                 "ELSE false END"), (r,)),
            )
            for label, sql, args in checks:
                cur.execute(sql, args)
                if cur.fetchall()[0][0]:
                    found.append(label)
            return found

        result: list[str] = self._run("checking the run's role", go)
        return result

    def write(self, export: Export) -> dict[str, int]:
        start = time.monotonic()
        space = RunSpace(export.run_id)

        def go(con: Any, cur: Any) -> None:
            self.ensure_schema(con, space)
            cur = con.cursor()
            for t in schema.TABLES:
                cur.execute(f"DELETE FROM {space.schema}.{t.name}")
                cols = t.column_names
                rows = column_values(export.rows[t.name], cols)
                if rows:
                    cur.executemany(
                        f"INSERT INTO {space.schema}.{t.name} ({', '.join(cols)}) VALUES "
                        f"({', '.join('%s' for _ in cols)})", rows)

        self._run("Postgres write", go)
        counts = export.counts()
        self.log.write(engine="postgres", purpose="sync", run_id=export.run_id,
                       schema=space.schema, rows=counts,
                       ms=round((time.monotonic() - start) * 1000))
        return counts
