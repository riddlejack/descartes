"""Part D's MCP tool, `query_data`, registered on the one `stemma` server by part B.

Like part A's tools, it serves the run folder named by `STEMMA_RUN_DIR`, and a typed
failure comes back as `{"error": <code>, "message": ...}`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stemma.datalayer import schema
from stemma.datalayer.errors import DataLayerError
from stemma.datalayer.query import QUERY_ROWS, run_query


def _tables_text() -> str:
    return "\n".join(f"- {t.name}({', '.join(c for c in t.column_names if c != 'run_id')}): "
                     f"{t.doc}" for t in schema.TABLES)


def query_data(sql: str) -> dict[str, Any]:
    result: dict[str, Any]
    run_dir = os.environ.get("STEMMA_RUN_DIR")
    if not run_dir:
        return {"error": "no_run_dir", "message": "STEMMA_RUN_DIR is not set for this server"}
    try:
        result = run_query(Path(run_dir).resolve(), sql)
    except DataLayerError as exc:
        result = {"error": exc.code, "message": exc.detail}
    return result


query_data.__doc__ = f"""Run one read-only SELECT over this run's evidence tables (through Zetaris
when configured, SQLite otherwise) and return columns and rows (at most {QUERY_ROWS}).
Each row must carry a receipt or claim id; a figure taken from a row is still filed as
a claim with its own support. Give every computed column an alias. Plain functions only
(count, sum, min, max, avg, round, lower, upper, substr, coalesce and a few more); no
semicolons or comments. Tables:
{_tables_text()}"""

TOOLS: tuple[Callable[..., dict[str, Any]], ...] = (query_data,)


def register_tools(server: Any) -> None:
    """Add part D's MCP tools to the one `stemma` server (wired by part B)."""
    for tool in TOOLS:
        server.add_tool(tool, name=tool.__name__, description=(tool.__doc__ or "").strip())
