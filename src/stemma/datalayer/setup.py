"""Register the evidence tables in Zetaris as one Unified Semantic Layer.

Zetaris cannot read a file on the laptop, so the rows live in a Postgres container
that Zetaris registers as a JDBC datasource (its "zero copy" federation: Zetaris holds
the connection, Postgres holds the rows). On top of the datasource tables, one USL
model declares every table with its primary and foreign keys, which Zetaris turns into
data-quality rules (for example: every support row cites a receipt in the manifest).

The statements are sent one at a time, as Lightning SQL requires; the COMPILE USL
statement is sent whole. Each is logged (the datasource password redacted).
"""

from __future__ import annotations

from dataclasses import dataclass

from stemma.datalayer import schema
from stemma.datalayer.sinks import PostgresConfig


@dataclass(frozen=True)
class Statement:
    sql: str
    purpose: str
    tolerate: tuple[str, ...] = ()  # error texts meaning "already done"


def _parent(namespace: str) -> str:
    return namespace.rsplit(".", 1)[0]


def statements(pg: PostgresConfig, jdbc_host: str, datasource: str, namespace: str,
               model: str) -> list[Statement]:
    url = f"jdbc:postgresql://{jdbc_host}:5432/{pg.database}"
    out = [
        Statement(f'CREATE DATASOURCE {datasource} DESCRIBE BY "Stemma evidence tables" '
                  f'OPTIONS (jdbcdriver "org.postgresql.Driver", jdbcurl "{url}", '
                  f'username "{pg.user}", password "{pg.password}", fetchsize "1000")',
                  "datasource", ("already exist",)),
        Statement(f"REGISTER DATASOURCE TABLES FROM {datasource}", "register_tables"),
        Statement(f"CREATE NAMESPACE IF NOT EXISTS {_parent(namespace)}", "namespace"),
        Statement(f"CREATE NAMESPACE IF NOT EXISTS {namespace}", "namespace"),
        Statement(schema.usl_compile(model, namespace), "usl_compile"),
    ]
    out += [Statement(s, "usl_activate") for s in schema.usl_activate(model, namespace,
                                                                     datasource)]
    return out


def teardown(datasource: str, namespace: str, model: str) -> list[Statement]:
    return [Statement(f"REMOVE USL {namespace}.{model}", "teardown"),
            Statement(f"DROP DATASOURCE {datasource}", "teardown")]
