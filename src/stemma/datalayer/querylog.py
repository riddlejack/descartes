"""The query log: every query either engine runs, with its text and row count.

One JSON object per line. Secrets never reach the log: a `password "..."` option in a
statement is replaced before the line is written.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.datalayer.safepath import append_line

logger = logging.getLogger("stemma.datalayer")

_SECRET = re.compile(r"""(password\s+)(["'])(?:(?!\2).)*\2""", re.IGNORECASE)


def redact(sql: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", sql)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class QueryLog:
    """Appends entries to `path` (if given) and to the `stemma.datalayer` logger.

    With `root`, `path` is relative to it and every append follows no symlink."""

    def __init__(self, path: Path | None = None, root: Path | None = None) -> None:
        self.path = path
        self.root = root
        self.entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def file(self) -> Path | None:
        if self.path is None:
            return None
        return self.root / self.path if self.root is not None else self.path

    def write(self, **entry: Any) -> dict[str, Any]:
        if "sql" in entry and isinstance(entry["sql"], str):
            entry["sql"] = redact(entry["sql"])
        record = {"at": _now(), **entry}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.entries.append(record)
            if self.path is not None and self.root is not None:
                append_line(self.root, self.path.as_posix(), line)
            elif self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        logger.info("%s", line)
        return record
