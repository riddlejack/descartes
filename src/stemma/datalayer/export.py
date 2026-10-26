"""Turn one run folder into the rows of the evidence-derived tables.

Reads only what parts A and B wrote, by their contracted layouts and interfaces:
- receipts from `store/receipts/*.json` (CONTRACTS section 4);
- claims through the register's `find_claims` and `get_claim` (section 5);
- as-filed SEC facts by re-reading the stored bytes of each `sec_api` receipt, keeping
  only facts filed before the run's `as_of` date, as the evidence service does;
- table cells from Docling's derived view, `store/derived/<sha256>.tables.json`.
Nothing here judges a row; it copies. What it cannot read is a warning on the result,
or a typed error when the run folder itself is broken.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from stemma.datalayer.errors import ExportError
from stemma.datalayer.schema import BY_NAME, TABLES

Row = dict[str, Any]


class ClaimSource(Protocol):
    """The part of the register this module reads (part B's `Register` satisfies it)."""

    def find_claims(self, text: str | None = None, branch_id: str | None = None,
                    tier: str | None = None, entity: str | None = None,
                    include_superseded: bool = False) -> list[dict[str, Any]]: ...

    def get_claim(self, given: str) -> dict[str, Any]: ...


@dataclass
class Export:
    run_id: str
    rows: dict[str, list[Row]] = field(default_factory=lambda: {t.name: [] for t in TABLES})
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.rows.items()}


def _num_repr(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, None
    return float(value), repr(float(value))


def _flag(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_run(run_dir: Path) -> tuple[str, str | None]:
    path = run_dir / "run.json"
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot read {path}: {exc}") from exc
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise ExportError(f"{path} has no run_id")
    as_of = run.get("as_of")
    return run_id, as_of if isinstance(as_of, str) else None


def _receipts(run_dir: Path) -> list[dict[str, Any]]:
    folder = run_dir / "store" / "receipts"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExportError(f"receipt {path.name} is unreadable: {exc}") from exc
        if not isinstance(data, dict) or not data.get("receipt_id"):
            raise ExportError(f"receipt {path.name} has no receipt_id")
        out.append(data)
    return out


def capture_row(run_id: str, r: dict[str, Any]) -> Row:
    http: dict[str, Any] = r["http"] if isinstance(r.get("http"), dict) else {}
    url = (r.get("requested_url") or http.get("requested_url") or http.get("final_url")
           or r.get("source_path") or r.get("requested_path"))
    extractor = r.get("extractor")
    return {
        "run_id": run_id, "receipt_id": r["receipt_id"], "created_at": r.get("created_at"),
        "kind": r.get("kind"), "reason": r.get("reason"), "status": r.get("status"),
        "detail": r.get("detail"), "url": url, "sha256": r.get("sha256"),
        "bytes_len": _int(r.get("bytes_len")), "content_type": r.get("content_type"),
        "text_chars": _int(r.get("text_chars")),
        "served_from_cache": _flag(r.get("served_from_cache")),
        "original_fetched_at": r.get("original_fetched_at"), "as_of": r.get("as_of"),
        "extractor": None if extractor is None else json.dumps(extractor, sort_keys=True),
    }


def sec_facts_from_bytes(body: bytes, as_of: str | None) -> list[dict[str, Any]]:
    """Every fact in a companyconcept answer, as filed, filed before `as_of` if set."""
    data = json.loads(body.decode("utf-8"))
    units = data.get("units") if isinstance(data, dict) else None
    facts: list[dict[str, Any]] = []
    if not isinstance(units, dict):
        return facts
    for unit, rows in units.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or "accn" not in row or "filed" not in row:
                continue
            if as_of is not None and not str(row["filed"]) < as_of:
                continue
            facts.append({"value": row.get("val"), "unit": unit, "start": row.get("start"),
                          "end": row.get("end"), "fy": row.get("fy"), "fp": row.get("fp"),
                          "form": row.get("form"), "accn": row.get("accn"),
                          "filed": row.get("filed")})
    facts.sort(key=lambda f: (str(f["end"]), str(f["filed"]), str(f["accn"]), str(f["unit"])))
    return facts


def _sec_rows(run_dir: Path, run_id: str, as_of: str | None, r: dict[str, Any],
              out: Export) -> None:
    sec: dict[str, Any] = r["sec"] if isinstance(r.get("sec"), dict) else {}
    sha = r.get("sha256")
    if r.get("kind") != "sec_api" or not sec.get("concept") or not sha:
        return
    path = run_dir / "store" / "bytes" / str(sha)
    try:
        facts = sec_facts_from_bytes(path.read_bytes(), as_of)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        out.warnings.append(f"receipt {r['receipt_id']}: SEC answer not read as facts ({exc})")
        return
    for n, f in enumerate(facts, start=1):
        value, value_repr = _num_repr(f["value"])
        out.rows["sec_fact"].append({
            "run_id": run_id, "source_receipt_id": r["receipt_id"], "n": n,
            "cik": _int(sec.get("cik")), "company": sec.get("company"),
            "concept": sec.get("concept"), "value": value, "value_repr": value_repr,
            "unit": f["unit"], "period_start": f["start"], "period_end": f["end"],
            "fiscal_year": _int(f["fy"]), "fiscal_period": f["fp"], "form": f["form"],
            "accession": f["accn"], "filed": f["filed"]})


_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_RULE = re.compile(r"^\s*:?-{3,}:?\s*$")


def markdown_cells(markdown: str) -> list[list[str]]:
    """The cells of a Markdown table, header first, the rule line dropped."""
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(s.strip("|"))]
        if cells and all(_RULE.match(c) for c in cells):
            continue
        rows.append(cells)
    return rows


def _table_rows(run_dir: Path, run_id: str, out: Export) -> None:
    folder = run_dir / "store" / "derived"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.tables.json")):
        sha = path.name.removesuffix(".tables.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tables = data["tables"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            out.warnings.append(f"derived tables {path.name} not read ({exc})")
            continue
        for t in tables:
            for r, cells in enumerate(markdown_cells(str(t.get("markdown", "")))):
                for c, text in enumerate(cells):
                    out.rows["doc_table_cell"].append({
                        "run_id": run_id, "sha256": sha, "table_index": _int(t.get("index")),
                        "row_n": r, "col_n": c, "page": _int(t.get("page")),
                        "cell_text": text})


def claim_rows(run_id: str, rec: dict[str, Any], out: Export) -> None:
    cid = rec["claim_id"]
    value: dict[str, Any] = rec.get("value") or {}
    number, number_repr = _num_repr(value.get("number"))
    deriv: dict[str, Any] = rec.get("derivation") or {}
    latest: dict[str, Any] = rec.get("verdict") or {}
    out.rows["claim"].append({
        "run_id": run_id, "claim_id": cid, "statement": rec.get("statement"),
        "tier": rec.get("tier"), "number": number, "number_repr": number_repr,
        "unit": value.get("unit"), "period": value.get("period"),
        "value_kind": value.get("kind"), "as_written": value.get("as_written"),
        "denominator": value.get("denominator"), "branch_id": rec.get("branch_id"),
        "author": rec.get("author"), "created_at": rec.get("created_at"),
        "derivation_script": deriv.get("script"), "supersedes": rec.get("supersedes"),
        "superseded_by": rec.get("superseded_by"), "verdict": latest.get("verdict")})
    for n, s in enumerate(rec.get("support") or [], start=1):
        out.rows["claim_support"].append({"run_id": run_id, "claim_id": cid, "n": n,
                                          "receipt_id": s.get("receipt_id"),
                                          "quote": s.get("quote")})
    for n, i in enumerate(deriv.get("inputs") or [], start=1):
        out.rows["claim_input"].append({"run_id": run_id, "claim_id": cid, "n": n,
                                        "input_claim_id": i})
    for n, name in enumerate(rec.get("entities") or [], start=1):
        out.rows["claim_entity"].append({"run_id": run_id, "claim_id": cid, "n": n,
                                         "name": name})
    for n, v in enumerate(rec.get("verdict_history") or [], start=1):
        out.rows["claim_verdict"].append({
            "run_id": run_id, "claim_id": cid, "n": n, "verdict": v.get("verdict"),
            "evidence": v.get("evidence"), "at_filing": _flag(v.get("at_filing")),
            "session": v.get("session")})
    if rec.get("superseded_by"):
        out.rows["claim_supersession"].append({"run_id": run_id, "old_claim_id": cid,
                                               "new_claim_id": rec["superseded_by"]})


def default_claim_source(run_dir: Path) -> ClaimSource | None:
    if not (run_dir / "register").is_dir():
        return None
    from stemma.register.store import (  # type: ignore[import-not-found,unused-ignore]
        open_register,  # part B; present once B has landed
    )

    reg: ClaimSource = open_register(run_dir)
    return reg


def export_run(run_dir: Path, claims: ClaimSource | None = None,
               use_default_register: bool = True) -> Export:
    """All rows of one run. `claims` defaults to the run's own register."""
    run_id, as_of = read_run(run_dir)
    out = Export(run_id)
    for r in _receipts(run_dir):
        out.rows["capture"].append(capture_row(run_id, r))
        _sec_rows(run_dir, run_id, as_of, r, out)
    _table_rows(run_dir, run_id, out)
    source = claims if claims is not None else (
        default_claim_source(run_dir) if use_default_register else None)
    if source is not None:
        for c in source.find_claims(include_superseded=True):
            claim_rows(run_id, source.get_claim(c["claim_id"]), out)
    _check_shape(out)
    return out


def _check_shape(out: Export) -> None:
    for name, rows in out.rows.items():
        table = BY_NAME[name]
        cols = set(table.column_names)
        seen: set[tuple[Any, ...]] = set()
        for row in rows:
            if set(row) != cols:
                raise ExportError(f"{name} row has columns {sorted(set(row) ^ cols)} wrong")
            key = tuple(row[k] for k in table.key)
            if any(k is None for k in key):
                raise ExportError(f"{name} row has an empty key: {key}")
            if key in seen:
                raise ExportError(f"{name} has two rows with key {key}")
            seen.add(key)


def column_values(rows: Sequence[Row], columns: Sequence[str]) -> list[tuple[Any, ...]]:
    return [tuple(r[c] for c in columns) for r in rows]
