"""Docling's table view (Docling 2.142.1, optional extra `evidence-tables`).

A derived view of stored bytes, never used for quoting: quotations are checked
against the stored text only. Results are kept in `store/derived/<sha256>.tables.json`
with Docling's version, so a document is converted once.
"""

from __future__ import annotations

import io
import json
from typing import Any

from stemma.evidence import limits

DERIVED_NOTE = "derived view from Docling; never quote from it, quote the stored text"


def docling_version() -> str | None:
    try:
        from importlib import metadata

        return metadata.version("docling")
    except Exception:  # noqa: BLE001 - not installed
        return None


def convert_tables(body: bytes, content_type: str) -> list[dict[str, Any]]:
    from docling.datamodel.base_models import (  # type: ignore[import-not-found]
        DocumentStream,
    )
    from docling.document_converter import (  # type: ignore[import-not-found]
        DocumentConverter,
    )

    suffix = ".pdf" if content_type == "application/pdf" else ".html"
    stream = DocumentStream(name=f"document{suffix}", stream=io.BytesIO(body))
    result = DocumentConverter().convert(stream)
    doc = result.document
    tables: list[dict[str, Any]] = []
    for index, table in enumerate(doc.tables):
        page = None
        if getattr(table, "prov", None):
            page = getattr(table.prov[0], "page_no", None)
        tables.append({"index": index, "page": page,
                       "markdown": table.export_to_markdown(doc=doc)})
    return tables


def bounded(tables: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    kept: list[dict[str, Any]] = []
    size = 0
    for table in tables:
        size += len(table["markdown"])
        if size > limits.TABLES_MAX_CHARS:
            return kept, True
        kept.append(table)
    return kept, False


def dumps(tables: list[dict[str, Any]], version: str) -> bytes:
    return json.dumps({"docling": version, "tables": tables}, ensure_ascii=False).encode()
