"""Provision one run in Zetaris: its own Postgres role, datasource and semantic layer.

Zetaris cannot read a file on the laptop, so the run's rows live in the run's own
Postgres schema. Provisioning, done once per run on its first Zetaris-mode sync:
1. a fresh Postgres role that can read that schema and nothing else (never a reused
   role; refused if one of that name exists), checked for any write capability before
   Zetaris is given it, and again on every later sync;
2. a Zetaris datasource for that schema, connecting as that role;
3. one Unified Semantic Layer model over the run's tables, with their keys and foreign
   keys (each foreign key becomes a Zetaris data-quality rule).

Whether a step is done is decided by the run's own record of completed steps
(`datalayer/provisioned.json`) and by Zetaris's structured answers (the rows of
`SHOW DATASOURCES`), never by the text of an error message. Statements go one at a
time, as Lightning SQL requires, each logged with its password redacted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer import schema
from stemma.datalayer.errors import SinkError
from stemma.datalayer.runspace import RunSpace
from stemma.datalayer.safepath import read_file, write_file

RECORD_REL = Path("datalayer", "provisioned.json")


class RoleSink(Protocol):
    def role_exists(self, space: RunSpace) -> bool: ...
    def create_run_role(self, space: RunSpace) -> str: ...
    def reset_password(self, space: RunSpace) -> str: ...
    def write_problems(self, space: RunSpace) -> list[str]: ...


class Statements(Protocol):
    def execute(self, sql: str) -> Any: ...
    def cells(self, sql: str) -> list[list[str]]: ...


@dataclass
class Record:
    run_id: str
    done: list[str] = field(default_factory=list)

    def as_json(self, space: RunSpace) -> bytes:
        return json.dumps({"run_id": self.run_id, "schema": space.schema, "role": space.role,
                           "datasource": space.datasource, "model": space.model,
                           "done": self.done}, indent=2).encode()


def _parent(namespace: str) -> str:
    return namespace.rsplit(".", 1)[0]


def datasource_sql(space: RunSpace, jdbc_host: str, database: str, password: str) -> str:
    url = f"jdbc:postgresql://{jdbc_host}:5432/{database}?currentSchema={space.schema}"
    return (f'CREATE DATASOURCE {space.datasource} DESCRIBE BY "Stemma evidence of one run" '
            f'OPTIONS (jdbcdriver "org.postgresql.Driver", jdbcurl "{url}", '
            f'username "{space.role}", password "{password}", fetchsize "1000")')


def zetaris_steps(space: RunSpace, namespace: str) -> list[tuple[str, str]]:
    steps = [("register_tables", f"REGISTER DATASOURCE TABLES FROM {space.datasource}"),
             ("namespace_parent", f"CREATE NAMESPACE IF NOT EXISTS {_parent(namespace)}"),
             ("namespace", f"CREATE NAMESPACE IF NOT EXISTS {namespace}"),
             ("usl_compile", schema.usl_compile(space.model, namespace))]
    steps += [(f"usl_activate:{t.name}", sql) for t, sql in zip(
        schema.TABLES, schema.usl_activate(space.model, namespace, space.datasource),
        strict=True)]
    return steps


def complete(record: Record, space: RunSpace, namespace: str) -> bool:
    return {"role", "datasource"} | {n for n, _ in zetaris_steps(space, namespace)} <= set(
        record.done)


def _load(run_dir: Path, run_id: str) -> Record:
    raw = read_file(run_dir, RECORD_REL.as_posix())
    if raw is None:
        return Record(run_id)
    data = json.loads(raw)
    if data.get("run_id") != run_id or not isinstance(data.get("done"), list):
        raise SinkError(f"{RECORD_REL} belongs to another run or is damaged")
    return Record(run_id, [str(d) for d in data["done"]])


def provision(run_dir: Path, run_id: str, sink: RoleSink, client: Statements,
              namespace: str, jdbc_host: str, database: str) -> Record:
    space = RunSpace(run_id)
    record = _load(run_dir, run_id)

    def mark(step: str) -> None:
        record.done.append(step)
        write_file(run_dir, RECORD_REL.as_posix(), record.as_json(space))

    password: str | None = None
    if "role" not in record.done:
        if sink.role_exists(space):
            raise SinkError(f"role {space.role} exists but this run did not create it; "
                            "a role is never reused")
        password = sink.create_run_role(space)
        mark("role")
    problems = sink.write_problems(space)
    if problems:
        raise SinkError(f"role {space.role} can do more than read its schema: "
                        f"{', '.join(problems)}")
    if "datasource" not in record.done:
        listed = {c.strip().lower() for row in client.cells("SHOW DATASOURCES") for c in row}
        if space.datasource.lower() in listed:
            client.execute(f"DROP DATASOURCE {space.datasource}")  # half-made earlier
        client.execute(datasource_sql(space, jdbc_host, database,
                                      password or sink.reset_password(space)))
        mark("datasource")
    for name, sql in zetaris_steps(space, namespace):
        if name not in record.done:
            client.execute(sql)
            mark(name)
    return record
