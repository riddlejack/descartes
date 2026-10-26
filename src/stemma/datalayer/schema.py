"""The evidence-derived tables: one definition, rendered for SQLite, Postgres and Zetaris.

Every table carries `run_id`, because one run folder is one store (CONTRACTS section 4)
and the shared database holds several runs side by side; no query ever mixes them.
Numbers are kept twice, as a double and as the exact text Python wrote (`repr`), so a
reader can prove the copy it got back is the number that was filed.

The Zetaris form is a Unified Semantic Layer: the tables compiled as one model with
their primary and foreign keys (Zetaris turns each foreign key into a data-quality
rule), then each table activated from the Postgres datasource that holds the rows.
"""

from __future__ import annotations

from dataclasses import dataclass

# Column types used here, and how each engine spells them.
_SQLITE = {"text": "TEXT", "int": "INTEGER", "real": "REAL"}
_POSTGRES = {"text": "TEXT", "int": "BIGINT", "real": "DOUBLE PRECISION"}
_USL = {"text": "string", "int": "bigint", "real": "double"}


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # "text" | "int" | "real"
    required: bool = False


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    table: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    name: str
    doc: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    foreign: tuple[ForeignKey, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def _c(name: str, type_: str = "text", required: bool = False) -> Column:
    return Column(name, type_, required)


RUN = _c("run_id", required=True)

TABLES: tuple[Table, ...] = (
    Table("sync_state", "Which sync the rows come from: one generation per sync, and the "
          "register log position it read.", (
        RUN, _c("generation", required=True), _c("register_position", "int"),
        _c("synced_at"),
    ), ("run_id",)),
    Table("capture", "The capture manifest: one row per receipt, failures included.", (
        RUN, _c("receipt_id", required=True), _c("created_at"), _c("kind"), _c("reason"),
        _c("status"), _c("detail"), _c("url"), _c("sha256"), _c("bytes_len", "int"),
        _c("content_type"), _c("text_chars", "int"), _c("served_from_cache", "int"),
        _c("original_fetched_at"), _c("as_of"), _c("extractor"),
    ), ("run_id", "receipt_id")),
    Table("claim", "The claim register: one row per claim, superseded ones included.", (
        RUN, _c("claim_id", required=True), _c("statement"), _c("tier"),
        _c("number", "real"), _c("number_repr"), _c("unit"), _c("period"), _c("value_kind"),
        _c("as_written"), _c("denominator"), _c("branch_id"), _c("author"),
        _c("created_at"), _c("derivation_script"), _c("supersedes"), _c("superseded_by"),
        _c("verdict"),
    ), ("run_id", "claim_id")),
    Table("claim_support", "Each claim's support: the receipt and the exact quotation.", (
        RUN, _c("claim_id", required=True), _c("n", "int", required=True), _c("receipt_id"),
        _c("quote"),
    ), ("run_id", "claim_id", "n"), (
        ForeignKey(("run_id", "claim_id"), "claim", ("run_id", "claim_id")),
        ForeignKey(("run_id", "receipt_id"), "capture", ("run_id", "receipt_id")),
    )),
    Table("claim_input", "The claims a derivation reads, in the order it names them.", (
        RUN, _c("claim_id", required=True), _c("n", "int", required=True),
        _c("input_claim_id"),
    ), ("run_id", "claim_id", "n"), (
        ForeignKey(("run_id", "claim_id"), "claim", ("run_id", "claim_id")),
    )),
    Table("claim_entity", "The names each claim is about.", (
        RUN, _c("claim_id", required=True), _c("n", "int", required=True), _c("name"),
    ), ("run_id", "claim_id", "n"), (
        ForeignKey(("run_id", "claim_id"), "claim", ("run_id", "claim_id")),
    )),
    Table("claim_verdict", "Every verdict recorded on a claim, oldest first.", (
        RUN, _c("claim_id", required=True), _c("n", "int", required=True), _c("verdict"),
        _c("evidence"), _c("at_filing", "int"), _c("session"),
    ), ("run_id", "claim_id", "n"), (
        ForeignKey(("run_id", "claim_id"), "claim", ("run_id", "claim_id")),
    )),
    Table("claim_supersession", "Corrections: the old claim and the claim that replaced it.", (
        RUN, _c("old_claim_id", required=True), _c("new_claim_id"),
    ), ("run_id", "old_claim_id"), (
        ForeignKey(("run_id", "old_claim_id"), "claim", ("run_id", "claim_id")),
        ForeignKey(("run_id", "new_claim_id"), "claim", ("run_id", "claim_id")),
    )),
    Table("sec_fact", "As-filed XBRL facts, each tied to the receipt of the answer it came in.", (
        RUN, _c("source_receipt_id", required=True), _c("n", "int", required=True),
        _c("cik", "int"), _c("company"), _c("concept"), _c("value", "real"), _c("value_repr"),
        _c("unit"), _c("period_start"), _c("period_end"), _c("fiscal_year", "int"),
        _c("fiscal_period"), _c("form"), _c("accession"), _c("filed"),
    ), ("run_id", "source_receipt_id", "n"), (
        ForeignKey(("run_id", "source_receipt_id"), "capture", ("run_id", "receipt_id")),
    )),
    Table("doc_table_cell", "Cells of tables in captured documents (Docling's derived view; "
          "never quoted).", (
        RUN, _c("sha256", required=True), _c("table_index", "int", required=True),
        _c("row_n", "int", required=True), _c("col_n", "int", required=True), _c("page", "int"),
        _c("cell_text"),
    ), ("run_id", "sha256", "table_index", "row_n", "col_n")),
)

BY_NAME = {t.name: t for t in TABLES}


def _ddl(table: Table, types: dict[str, str], prefix: str = "") -> str:
    cols = [f"  {c.name} {types[c.type]}{' NOT NULL' if c.required else ''}"
            for c in table.columns]
    cols.append(f"  PRIMARY KEY ({', '.join(table.key)})")
    return f"CREATE TABLE IF NOT EXISTS {prefix}{table.name} (\n" + ",\n".join(cols) + "\n)"


def sqlite_ddl() -> list[str]:
    return [_ddl(t, _SQLITE) for t in TABLES]


def postgres_ddl(schema: str) -> list[str]:
    return [f"CREATE SCHEMA IF NOT EXISTS {schema}"] + [
        _ddl(t, _POSTGRES, f"{schema}.") for t in TABLES]


def usl_compile(model: str, namespace: str) -> str:
    """One COMPILE USL statement for the whole model (sent intact, never split)."""
    bodies = []
    for t in TABLES:
        cols = [f"    {c.name} {_USL[c.type]}{' NOT NULL' if c.required else ''}"
                for c in t.columns]
        cols.append(f"    PRIMARY KEY ({', '.join(t.key)})")
        for fk in t.foreign:
            cols.append(f"    FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES "
                        f"{fk.table}({', '.join(fk.ref_columns)})")
        bodies.append(f"CREATE TABLE {t.name} (\n" + ",\n".join(cols) + "\n);")
    return (f"COMPILE USL IF NOT EXISTS {model} DEPLOY NAMESPACE {namespace} DDL\n"
            + "\n".join(bodies))


def usl_activate(model: str, namespace: str, datasource: str) -> list[str]:
    """One ACTIVATE statement per table, reading the Postgres datasource's table."""
    return [f"ACTIVATE USL TABLE {namespace}.{model}.{t.name} AS SELECT "
            f"{', '.join(t.column_names)} FROM {datasource}.{t.name}" for t in TABLES]
