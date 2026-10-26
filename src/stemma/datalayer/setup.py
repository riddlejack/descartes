"""Provision one run in Zetaris: its own Postgres role, datasource and semantic layer.

Zetaris cannot read a file on the laptop, so the run's rows live in the run's own
Postgres database. Provisioning, done once per run on its first Zetaris-mode sync:
1. a fresh Postgres role that can read that database and nothing else (never a reused
   role; refused if one of that name exists), checked for any write capability before
   Zetaris is given it, and again on every later sync;
2. a Zetaris datasource for that database, connecting as that role;
3. one Unified Semantic Layer model over the run's tables, with their keys and foreign
   keys (each foreign key becomes a Zetaris data-quality rule).

Whether a step is done is decided by the run's own record of completed steps
(`datalayer/provisioned.json`) and by Zetaris's structured answers (the rows of
`SHOW DATASOURCES`), never by the text of an error message. Statements go one at a
time, as Lightning SQL requires, each logged with its password redacted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer import schema
from stemma.datalayer.errors import DataLayerError, SinkError
from stemma.datalayer.runspace import RunSpace
from stemma.datalayer.safepath import read_file, write_file

RECORD_REL = Path("datalayer", "provisioned.json")


class RoleSink(Protocol):
    def role_exists(self, space: RunSpace) -> bool: ...
    def role_marker(self, space: RunSpace) -> str | None: ...
    def create_run_role(self, space: RunSpace, marker: str) -> str: ...
    def repair_run_role(self, space: RunSpace, marker: str) -> str: ...
    def reset_password(self, space: RunSpace) -> str: ...
    def write_problems(self, space: RunSpace) -> list[str]: ...
    def drop_run(self, space: RunSpace, marker: str | None) -> list[str]: ...


class Statements(Protocol):
    def execute(self, sql: str) -> Any: ...
    def cells(self, sql: str) -> list[list[str]]: ...


@dataclass
class Record:
    run_id: str
    done: list[str] = field(default_factory=list)
    role_marker: str | None = None  # written before the role is made, so a crash can repair
    cleaned_at: str | None = None

    def as_json(self, space: RunSpace) -> bytes:
        return json.dumps({"run_id": self.run_id, "database": space.database, "role": space.role,
                           "datasource": space.datasource, "model": space.model,
                           "done": self.done, "role_marker": self.role_marker,
                           "cleaned_at": self.cleaned_at}, indent=2).encode()


def _parent(namespace: str) -> str:
    return namespace.rsplit(".", 1)[0]


def datasource_sql(space: RunSpace, jdbc_host: str, database: str, password: str) -> str:
    url = f"jdbc:postgresql://{jdbc_host}:5432/{space.database}"
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
    marker = data.get("role_marker")
    return Record(run_id, [str(d) for d in data["done"]],
                  marker if isinstance(marker, str) else None,
                  data.get("cleaned_at") if isinstance(data.get("cleaned_at"), str) else None)


def cleaned_at(run_dir: Path, run_id: str) -> str | None:
    """When the run's Zetaris and Postgres copies were cleaned up, or None."""
    return _load(run_dir, run_id).cleaned_at


def provision(run_dir: Path, run_id: str, sink: RoleSink, client: Statements,
              namespace: str, jdbc_host: str, database: str) -> Record:
    space = RunSpace(run_id)
    record = _load(run_dir, run_id)

    def mark(step: str) -> None:
        record.done.append(step)
        write_file(run_dir, RECORD_REL.as_posix(), record.as_json(space))

    password: str | None = None
    if record.cleaned_at is not None:
        raise SinkError(f"run {run_id} was cleaned up on {record.cleaned_at}; it is not "
                        "provisioned again")
    if "role" not in record.done:
        if record.role_marker is None:  # recorded before the role exists
            record.role_marker = uuid.uuid4().hex
            write_file(run_dir, RECORD_REL.as_posix(), record.as_json(space))
        if sink.role_exists(space):
            # Only a role carrying this run's recorded marker is finished; any other
            # refuses inside repair_run_role.
            password = sink.repair_run_role(space, record.role_marker)
        else:
            password = sink.create_run_role(space, record.role_marker)
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


class CleanupFailed(SinkError):
    """A cleanup step failed; nothing that depends on it was removed."""

    code = "cleanup_failed"


def _listed(client: Statements, sql: str, name: str) -> bool:
    return name.lower() in {row[0].strip().lower() for row in client.cells(sql) if row}


def deprovision(space: RunSpace, client: Statements, namespace: str) -> list[str]:
    """Remove a run's semantic layer, then its datasource, from Zetaris.

    Whether each exists is read from Zetaris's own listings, never from an error message.
    A failure stops at once and is raised with what was and was not removed, so the
    datasource is never dropped while the semantic layer built on it remains."""
    removed: list[str] = []
    steps = (
        (f"semantic layer {space.model}",
         f"SHOW NAMESPACES OR TABLES IN {namespace}", space.model,
         f"REMOVE USL {space.model} NAMESPACE {namespace}"),
        (f"datasource {space.datasource}", "SHOW DATASOURCES", space.datasource,
         f"DROP DATASOURCE {space.datasource}"),
    )
    for label, listing, name, statement in steps:
        try:
            if _listed(client, listing, name):
                client.execute(statement)
                if _listed(client, listing, name):
                    raise CleanupFailed(f"{label} is still listed after its removal")
                removed.append(label)
        except DataLayerError as exc:
            left = [lb for lb, *_ in steps if lb not in removed]
            raise CleanupFailed(f"removing {label} failed ({exc.detail}); removed: "
                                f"{removed or 'nothing'}; left in place: {left}") from None
    return removed


def cleanup(run_dir: Path, run_id: str, sink: RoleSink, client: Statements,
            namespace: str, log: Any) -> list[str]:
    """Remove a run's copies from Zetaris and Postgres, on request, never automatically.

    Refuses a run whose driver status says it is running. Zetaris goes first (its
    datasource reads the database), then the database, then the role, each step logged;
    the first failure stops it. The run's own folder, its SQLite copy and any pack made
    from it are untouched: a replay needs none of what is removed here."""
    status = run_dir / "status.json"
    if status.exists():
        state = json.loads(status.read_text(encoding="utf-8")).get("state")
        if state == "running":
            raise CleanupFailed(f"run {run_id} is still running; nothing was removed")
    space = RunSpace(run_id)
    record = _load(run_dir, run_id)
    removed: list[str] = []
    try:
        removed += deprovision(space, client, namespace)
        removed += sink.drop_run(space, record.role_marker)
    except DataLayerError as exc:
        log.write(engine="datalayer", purpose="cleanup", run_id=run_id, ok=False,
                  removed=removed, error=str(exc))
        raise
    record.cleaned_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    record.done = []
    write_file(run_dir, RECORD_REL.as_posix(), record.as_json(space))
    log.write(engine="datalayer", purpose="cleanup", run_id=run_id, ok=True, removed=removed)
    return removed
