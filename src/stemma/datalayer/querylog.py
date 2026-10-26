"""The query log: every query either engine runs, with its text and row count.

One JSON object per line. Secrets never reach the log: every string in an entry is
scrubbed of registered keys, tokens and passwords, `password "..."` options and bearer
tokens before the line is written. Response bodies are never logged at all; callers
record a status, a length and a hash instead.
"""

from __future__ import annotations

import hashlib
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
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KNOWN: set[str] = set()
_KNOWN_LOCK = threading.Lock()
# Logical invariant: a value this short is not a secret worth scrubbing (it would
# blank out ordinary words).
_MIN_SECRET = 8


def register_secret(value: str | None) -> None:
    """A key, token or password in use: it is scrubbed from every log line and message."""
    if isinstance(value, str) and len(value) >= _MIN_SECRET:
        with _KNOWN_LOCK:
            _KNOWN.add(value)


def redact(sql: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", sql)


def scrub(text: str) -> str:
    """`text` with every registered secret, password option and bearer token replaced."""
    out = _BEARER.sub(lambda m: f"{m.group(1)}***", redact(text))
    with _KNOWN_LOCK:
        known = sorted(_KNOWN, key=len, reverse=True)
    for secret in known:
        out = out.replace(secret, "***")
    return out


def _scrub_all(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_all(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_all(v) for v in value]
    return value


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
        record = {"at": _now(), **_scrub_all(entry)}
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


def digest(raw: bytes) -> str:
    """What a log or an error may say about a response body: its length and hash."""
    return f"{len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()[:16]}"
