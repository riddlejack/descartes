"""A small client for Zetaris's REST SQL endpoint (`POST /sql-editor/sqls/run`).

The request shape follows Zetaris's own getting-started helpers: a bearer API key, the
`X-Org-ID` header, a fresh UUID `X-Request-ID` per call, and a body with `queryId`,
`sql`, `source` and `limit`. One statement per call, as Lightning SQL requires.
Every call is logged with its text, row count, time taken and request id, failures
included. The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from stemma.datalayer.errors import QueryError, ZetarisUnavailable
from stemma.datalayer.querylog import QueryLog, digest, register_secret

DEFAULT_REST_URL = "http://localhost:8888/api/v1.0"
# Resource limit: seconds one REST call may take (a cold Spark query on emulated amd64
# can take minutes).
TIMEOUT_S = 600
# Resource limit: rows asked for per query. An answer that fills it may be cut short,
# so it is an error, never a silent partial result.
ROW_LIMIT = 10_000

# (url, body, headers, timeout) -> (status, body)
Transport = Callable[[str, bytes | None, dict[str, str], float], tuple[int, bytes]]


def urllib_transport(url: str, body: bytes | None, headers: dict[str, str],
                     timeout: float) -> tuple[int, bytes]:
    req = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except (URLError, OSError, TimeoutError) as exc:
        raise ZetarisUnavailable(f"cannot reach {url}: {getattr(exc, 'reason', exc)}") from exc


@dataclass(frozen=True)
class ZetarisConfig:
    rest_url: str
    api_key: str
    org_id: str = "1"

    def __repr__(self) -> str:  # never print the key
        return f"ZetarisConfig(rest_url={self.rest_url!r}, org_id={self.org_id!r})"


def rows_of(answer: Any, columns: Sequence[str] | None) -> list[tuple[Any, ...]]:
    """The rows of an answer, as tuples in `columns` order when rows come as objects."""
    data = answer.get("data") if isinstance(answer, dict) else None
    if data is None and isinstance(answer, dict) and "rows" in answer:
        data = answer["rows"]
    if not isinstance(data, list):
        keys = sorted(str(k) for k in answer)[:10] if isinstance(answer, dict) else []
        raise QueryError(f"the answer has no row list ({type(answer).__name__}, keys {keys})")
    out: list[tuple[Any, ...]] = []
    for row in data:
        if isinstance(row, list):
            out.append(tuple(row))
        elif isinstance(row, dict):
            if columns is None:
                raise QueryError("rows came as objects and no column order was given")
            lowered = {str(k).lower(): v for k, v in row.items()}
            missing = [c for c in columns if c.lower() not in lowered]
            if missing:
                raise QueryError(f"row lacks columns {missing}: {sorted(lowered)}")
            out.append(tuple(lowered[c.lower()] for c in columns))
        else:
            raise QueryError(f"a row is neither a list nor an object ({type(row).__name__})")
    return out


class ZetarisClient:
    def __init__(self, config: ZetarisConfig, log: QueryLog,
                 transport: Transport = urllib_transport) -> None:
        self.config = config
        self.log = log
        self.transport = transport
        register_secret(config.api_key)

    def _headers(self, request_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "X-Org-ID": self.config.org_id,
                "X-Request-ID": request_id, "Content-Type": "application/json"}

    def call(self, path: str, body: dict[str, Any] | None) -> Any:
        request_id = str(uuid.uuid4())
        url = self.config.rest_url.rstrip("/") + path
        payload = None if body is None else json.dumps(body).encode("utf-8")
        status, raw = self.transport(url, payload, self._headers(request_id), TIMEOUT_S)
        if status in (401, 403):
            raise ZetarisUnavailable(f"HTTP {status}: Zetaris refused the API key")
        if status >= 400:
            raise QueryError(f"HTTP {status} ({digest(raw)})")
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except ValueError as exc:
            raise QueryError(f"the answer is not JSON ({digest(raw)})") from exc

    def _send(self, sql: str, purpose: str, columns: Sequence[str] | None, parse: bool,
              limit: int = ROW_LIMIT, **extra: Any
              ) -> tuple[Any, list[tuple[Any, ...]] | None]:
        query_id = str(uuid.uuid4())
        start = time.monotonic()
        entry: dict[str, Any] = {"engine": "zetaris", "purpose": purpose, "query_id": query_id,
                                 "sql": sql, **extra}
        try:
            answer = self.call("/sql-editor/sqls/run", {"queryId": query_id, "sql": sql,
                                                         "source": "SqlEditor",
                                                         "limit": limit})
            rows = rows_of(answer, columns) if parse else None
        except QueryError as exc:
            self.log.write(**entry, ok=False, error=str(exc),
                           ms=round((time.monotonic() - start) * 1000))
            raise
        self.log.write(**entry, ok=True, rows=None if rows is None else len(rows),
                       ms=round((time.monotonic() - start) * 1000))
        return answer, rows

    def run_page(self, sql: str, columns: Sequence[str] | None, limit: int,
                 purpose: str = "query", **extra: Any) -> list[tuple[Any, ...]]:
        """At most `limit` rows of one query; the caller decides what a full page means."""
        _, rows = self._send(sql, purpose, columns, True, limit, **extra)
        assert rows is not None  # logical invariant: parse=True always yields rows
        return rows

    def run(self, sql: str, columns: Sequence[str] | None = None) -> list[tuple[Any, ...]]:
        """Run one query and return its rows. Logged whether it succeeds or not."""
        rows = self.run_page(sql, columns, ROW_LIMIT)
        if len(rows) >= ROW_LIMIT:
            raise QueryError(f"the answer reached the {ROW_LIMIT}-row limit and may be cut short")
        return rows

    def execute(self, sql: str) -> Any:
        """Run one statement (DDL, registration) and return Zetaris's answer as given."""
        answer, _ = self._send(sql, "statement", None, False)
        return answer

    def datasources(self) -> Any:
        return self.call("/datasource/datasources", None)
