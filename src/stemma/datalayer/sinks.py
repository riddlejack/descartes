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


def _sqlstate(exc: Exception) -> str | None:
    """Postgres's structured error code from a pg8000 error (never its message text)."""
    info = exc.args[0] if exc.args else None
    return str(info.get("C")) if isinstance(info, dict) and info.get("C") else None


def new_password() -> str:
    pw = "".join(secrets.choice(_ALPHABET) for _ in range(32))
    register_secret(pw)
    return pw


class PostgresSink:
    """The Postgres that Zetaris reads: one database per run (pg8000, BSD-3-Clause).

    `config` connects as the owning role to the cluster's own database; each run gets a
    database of its own, holding its tables in `public`."""

    name = "postgres"

    def __init__(self, config: PostgresConfig, log: QueryLog) -> None:
        self.config = config
        self.log = log
        register_secret(config.password)

    def connect(self, database: str | None = None) -> Any:
        import pg8000.dbapi  # type: ignore[import-untyped]  # optional extra `datalayer`

        c = self.config
        try:
            return pg8000.dbapi.connect(host=c.host, port=c.port,
                                        database=database or c.database,
                                        user=c.user, password=c.password, timeout=30)
        except Exception as exc:
            raise SinkError(f"cannot connect to Postgres at {c.host}:{c.port}: "
                            f"{type(exc).__name__}") from exc

    @staticmethod
    def _plain(*names: str) -> None:
        for n in names:
            if not _PLAIN_NAME.fullmatch(n):
                raise SinkError(f"{n!r} is not a plain identifier")

    def _run(self, what: str, fn: Any, database: str | None = None,
             autocommit: bool = False) -> Any:
        con = self.connect(database)
        con.autocommit = autocommit
        try:
            out = fn(con, con.cursor())
            if not autocommit:
                con.commit()
            return out
        except SinkError:
            if not autocommit:
                con.rollback()
            raise
        except Exception as exc:
            if not autocommit:
                con.rollback()
            raise SinkError(f"{what} failed: {type(exc).__name__}: "
                            f"{scrub(str(exc))[:300]}") from exc
        finally:
            con.close()

    def ensure_database(self, space: RunSpace) -> None:
        """The run's database and its tables (CREATE DATABASE runs outside a transaction)."""
        self._plain(space.database)

        def make(con: Any, cur: Any) -> None:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (space.database,))
            if not cur.fetchall():
                try:
                    cur.execute(f"CREATE DATABASE {space.database}")
                except Exception as exc:
                    # 42P04 duplicate_database: a concurrent sync made it first.
                    if _sqlstate(exc) != "42P04":
                        raise
            cur.execute(f"REVOKE ALL ON DATABASE {space.database} FROM PUBLIC")

        self._run("creating the run's database", make, autocommit=True)

        def tables(con: Any, cur: Any) -> None:
            cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            for ddl in schema.postgres_ddl("public"):
                cur.execute(ddl)

        self._run("creating the run's tables", tables, space.database)

    def role_exists(self, space: RunSpace) -> bool:
        def go(con: Any, cur: Any) -> bool:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (space.role,))
            return bool(cur.fetchall())
        result: bool = self._run("looking up the run's role", go)
        return result

    def create_run_role(self, space: RunSpace) -> str:
        """A fresh login role that can read this run's database and nothing else.

        Never reuses a role: if one of that name exists, this refuses. Returns its
        password, which goes only into Zetaris's datasource."""
        self._plain(space.role, space.database)
        pw = new_password()
        r = space.role
        self.ensure_database(space)

        def cluster(con: Any, cur: Any) -> None:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (r,))
            if cur.fetchall():
                raise SinkError(f"role {r} already exists; a run's role is never reused")
            cur.execute("SELECT datname FROM pg_database WHERE datallowconn")
            dbs = [str(row[0]) for row in cur.fetchall()]
            self._plain(*dbs)
            cur.execute(f"CREATE ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        f"NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 "
                        f"PASSWORD '{pw}'")
            for d in dbs:
                cur.execute(f"REVOKE ALL ON DATABASE {d} FROM PUBLIC")
            cur.execute(f"GRANT CONNECT ON DATABASE {space.database} TO {r}")

        self._run("creating the run's role", cluster)

        def grants(con: Any, cur: Any) -> None:
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {r}")
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {r}")

        self._run("granting the run's role its reads", grants, space.database)
        self.log.write(engine="postgres", purpose="run_role", role=r, database=space.database)
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
        """Everything the run's role could do beyond reading its own database's tables.

        Cluster-wide facts are checked from the owning database; table and schema facts
        inside the run's database, the only one the role may connect to (also checked)."""
        r = space.role

        def cluster(con: Any, cur: Any) -> list[str]:
            found: list[str] = []
            cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
                        "rolbypassrls, rolinherit FROM pg_roles WHERE rolname = %s", (r,))
            rows = cur.fetchall()
            if not rows:
                return [f"role {r} does not exist"]
            names = ("superuser", "createrole", "createdb", "replication", "bypassrls",
                     "inherit")
            found += [f"has {n}" for n, flag in zip(names, rows[0], strict=True) if flag]
            for label, sql, args in (
                ("member of another role", ("SELECT count(*) FROM pg_auth_members m JOIN "
                 "pg_roles x ON x.oid = m.member WHERE x.rolname = %s"), (r,)),
                ("owns a database", ("SELECT count(*) FROM pg_database WHERE datdba = "
                 "(SELECT oid FROM pg_roles WHERE rolname = %s)"), (r,)),
                ("can create in a database", ("SELECT count(*) FROM pg_database WHERE "
                 "has_database_privilege(%s, oid, 'CREATE, TEMPORARY')"), (r,)),
                ("can connect to another database", ("SELECT count(*) FROM pg_database "
                 "WHERE datallowconn AND datname <> %s AND "
                 "has_database_privilege(%s, oid, 'CONNECT')"), (space.database, r)),
            ):
                cur.execute(sql, args)
                if cur.fetchall()[0][0]:
                    found.append(label)
            return found

        def inside(con: Any, cur: Any) -> list[str]:
            found: list[str] = []
            for label, sql, args in (
                ("owns a relation", ("SELECT count(*) FROM pg_class WHERE relowner = "
                 "(SELECT oid FROM pg_roles WHERE rolname = %s)"), (r,)),
                ("owns a schema", ("SELECT count(*) FROM pg_namespace WHERE nspowner = "
                 "(SELECT oid FROM pg_roles WHERE rolname = %s)"), (r,)),
                # pg_catalog.pg_settings is the documented interface to SET: updating it
                # changes the session's settings, never stored data.
                ("can change a table", ("SELECT count(*) FROM pg_class c WHERE c.relkind IN "
                 "('r','p','v','m','f') AND c.oid <> 'pg_catalog.pg_settings'::regclass "
                 "AND has_table_privilege(%s, c.oid, "
                 "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')"), (r,)),
                ("can create in a schema", ("SELECT count(*) FROM pg_namespace WHERE "
                 "has_schema_privilege(%s, oid, 'CREATE')"), (r,)),
                ("can read outside its tables", ("SELECT count(*) FROM pg_class c JOIN "
                 "pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN "
                 "('r','p','v','m','f') AND n.nspname !~ '^pg_' AND n.nspname NOT IN "
                 "('information_schema', 'public') AND "
                 "has_table_privilege(%s, c.oid, 'SELECT')"), (r,)),
                ("can change a sequence", ("SELECT count(*) FROM pg_class c WHERE "
                 "CASE WHEN c.relkind = 'S' THEN has_sequence_privilege(%s, c.oid, 'UPDATE') "
                 "ELSE false END"), (r,)),
            ):
                cur.execute(sql, args)
                if cur.fetchall()[0][0]:
                    found.append(label)
            return found

        out: list[str] = self._run("checking the run's role", cluster)
        if out != [f"role {r} does not exist"]:
            out += self._run("checking the run's role", inside, space.database)
        return out

    def write(self, export: Export) -> dict[str, int]:
        start = time.monotonic()
        space = RunSpace(export.run_id)
        self.ensure_database(space)

        def go(con: Any, cur: Any) -> None:
            for t in schema.TABLES:
                cur.execute(f"DELETE FROM public.{t.name}")
                cols = t.column_names
                rows = column_values(export.rows[t.name], cols)
                if rows:
                    cur.executemany(
                        f"INSERT INTO public.{t.name} ({', '.join(cols)}) VALUES "
                        f"({', '.join('%s' for _ in cols)})", rows)

        self._run("Postgres write", go, space.database)
        counts = export.counts()
        self.log.write(engine="postgres", purpose="sync", run_id=export.run_id,
                       database=space.database, rows=counts,
                       ms=round((time.monotonic() - start) * 1000))
        return counts
