"""A client for Meterless Nightshift's Runtime (contract 0.90.0, OpenAI-compatible).

Built to the published contract (Meterless repository,
`docs/agent-ready/add-meterless-provider.md`, read 26 Oct 2026):
- base URL normalised to `<host>/v1` from whatever was pasted, never `/v1/v1`;
- `Authorization: Bearer <token>` on every call; the token comes from
  `METERLESS_RUNTIME_TOKEN` or `<stateDir>/proxy-token`; the placeholder `rt-local` is
  refused;
- `meterless-app: stemma` on every request, `meterless-scope: user` on chat;
- model ids are routing modes (`meterless/deep` and so on), read from `GET /v1/models`;
  unknown `meterless/*` ids are kept;
- status comes from `GET /v1/models`: 401 or 403 is unauthorized, 200 with meterless
  ids is online, anything else is offline (`/healthz` proves nothing);
- a 180 s timeout, since a cold model load can take minutes.

Every chat call writes a receipt: the route asked for, the model the answer names, any
`meterless-*` response headers, the request's digest and sizes, timing and usage. The
token and the message text are never logged. This client never falls back to another
provider; that choice belongs to the caller, which must log it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from stemma.datalayer.querylog import QueryLog

CONTRACT = "0.90.0"
APP_ID = "stemma"
DEFAULT_BASE_URL = "http://127.0.0.1:8088/v1"
# Resource limit from the contract: a cold model load can take minutes.
TIMEOUT_S = 180.0
# Resource limit: a status probe or Auto Detect attempt is quick or it is offline.
PROBE_TIMEOUT_S = 3.0
# The contract's Auto Detect order.
DETECT_PORTS = (8088, 8090, 8787, 11434)
DETECT_HOSTS = ("127.0.0.1", "localhost", "[::1]")
PLACEHOLDER_TOKEN = "rt-local"


class NightshiftError(Exception):
    """The runtime answered, but not with something usable."""


class NightshiftOffline(NightshiftError):
    """Nothing usable is listening at the base URL."""


class NightshiftUnauthorized(NightshiftError):
    """The runtime is up and refused the token (401 or 403)."""


@dataclass(frozen=True)
class HttpAnswer:
    status: int
    headers: dict[str, str]
    body: bytes


# (method, url, body, headers, timeout) -> HttpAnswer; raises NightshiftOffline when no
# connection could be made.
Transport = Callable[[str, str, bytes | None, dict[str, str], float], HttpAnswer]


def urllib_transport(method: str, url: str, body: bytes | None, headers: dict[str, str],
                     timeout: float) -> HttpAnswer:
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return HttpAnswer(int(resp.status), {k.lower(): v for k, v in resp.headers.items()},
                              resp.read())
    except HTTPError as exc:
        return HttpAnswer(int(exc.code), {k.lower(): v for k, v in exc.headers.items()},
                          exc.read())
    except (URLError, OSError, TimeoutError) as exc:
        raise NightshiftOffline(f"no answer from {url}: {getattr(exc, 'reason', exc)}") from exc


_SUFFIXES = ("/v1/chat/completions", "/v1/models", "/healthz", "/v1")


def normalise_base_url(pasted: str) -> str:
    """Any pasted runtime address, as `<scheme>://<host>/v1`."""
    s = pasted.strip().rstrip("/")
    if not s:
        raise NightshiftError("the runtime address is empty")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        s = "http://" + s
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if s.endswith(suffix):
                s = s[: -len(suffix)].rstrip("/")
                changed = True
    if not re.match(r"^https?://[^/]+$", s):
        raise NightshiftError(f"not a runtime address: {pasted!r}")
    return s + "/v1"


def read_token(env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    token = e.get("METERLESS_RUNTIME_TOKEN", "").strip()
    if not token and e.get("METERLESS_STATE_DIR"):
        path = Path(e["METERLESS_STATE_DIR"]) / "proxy-token"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise NightshiftUnauthorized(f"cannot read the runtime token file: {exc}") from exc
    if not token:
        raise NightshiftUnauthorized("no runtime token: set METERLESS_RUNTIME_TOKEN or "
                                     "METERLESS_STATE_DIR")
    if token == PLACEHOLDER_TOKEN:
        raise NightshiftUnauthorized("the placeholder token rt-local is not a runtime token")
    return token


@dataclass(frozen=True)
class Status:
    state: str  # "online" | "unauthorized" | "offline"
    models: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class ChatResult:
    text: str | None
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    route: str
    served_model: str | None
    usage: dict[str, Any]
    receipt: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


class NightshiftClient:
    def __init__(self, base_url: str, token: str, log: QueryLog,
                 transport: Transport = urllib_transport, app_id: str = APP_ID) -> None:
        if token == PLACEHOLDER_TOKEN:
            raise NightshiftUnauthorized("the placeholder token rt-local is not a runtime token")
        self.base_url = normalise_base_url(base_url)
        self._token = token
        self.log = log
        self.transport = transport
        self.app_id = app_id

    def __repr__(self) -> str:  # never print the token
        return f"NightshiftClient(base_url={self.base_url!r}, app_id={self.app_id!r})"

    @classmethod
    def from_env(cls, log: QueryLog, env: Mapping[str, str] | None = None,
                 transport: Transport = urllib_transport) -> NightshiftClient:
        e = os.environ if env is None else env
        return cls(e.get("METERLESS_BASE_URL", DEFAULT_BASE_URL), read_token(e), log, transport)

    def _headers(self, chat: bool) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._token}", "meterless-app": self.app_id,
             "Accept": "application/json"}
        if chat:
            h["meterless-scope"] = "user"
            h["Content-Type"] = "application/json"
        return h

    def status(self) -> Status:
        try:
            ans = self.transport("GET", f"{self.base_url}/models", None, self._headers(False),
                                 PROBE_TIMEOUT_S)
        except NightshiftOffline as exc:
            return Status("offline", detail=str(exc))
        if ans.status in (401, 403):
            return Status("unauthorized", detail=f"HTTP {ans.status}")
        if ans.status != 200:
            return Status("offline", detail=f"HTTP {ans.status}")
        try:
            data = json.loads(ans.body.decode("utf-8"))
            ids = tuple(str(m["id"]) for m in data["data"] if isinstance(m, dict) and "id" in m)
        except (ValueError, KeyError, TypeError):
            return Status("offline", detail="GET /v1/models did not answer a model list")
        meterless = tuple(i for i in ids if i.startswith("meterless/"))
        if not meterless:
            return Status("offline", ids, "the model list has no meterless/* routes")
        return Status("online", ids)

    def models(self) -> tuple[str, ...]:
        s = self.status()
        if s.state == "unauthorized":
            raise NightshiftUnauthorized(s.detail)
        if s.state != "online":
            raise NightshiftOffline(s.detail)
        return s.models

    def chat(self, messages: Sequence[Mapping[str, Any]], route: str = "meterless/deep",
             tools: Sequence[Mapping[str, Any]] | None = None, purpose: str = "chat",
             **params: Any) -> ChatResult:
        """One chat completion through the runtime, with a logged receipt."""
        if not route.startswith("meterless/"):
            raise NightshiftError(f"{route!r} is not a meterless routing mode")
        body: dict[str, Any] = {"model": route, "messages": list(messages), **params}
        if tools:
            body["tools"] = list(tools)
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        local_id = str(uuid.uuid4())
        receipt: dict[str, Any] = {
            "provider": "nightshift", "contract": CONTRACT, "purpose": purpose,
            "local_request_id": local_id, "route": route, "base_url": self.base_url,
            "request_sha256": hashlib.sha256(payload).hexdigest(),
            "request_bytes": len(payload), "messages": len(body["messages"]),
            "tools": len(body.get("tools", [])),
        }
        start = time.monotonic()
        try:
            ans = self.transport("POST", f"{self.base_url}/chat/completions", payload,
                                 self._headers(True), TIMEOUT_S)
        except NightshiftOffline as exc:
            self.log.write(**receipt, ok=False, error=str(exc),
                           ms=round((time.monotonic() - start) * 1000))
            raise
        receipt["ms"] = round((time.monotonic() - start) * 1000)
        receipt["http_status"] = ans.status
        receipt["runtime_headers"] = {k: v for k, v in sorted(ans.headers.items())
                                      if k.startswith(("meterless", "x-meterless", "x-request"))}
        try:
            if ans.status in (401, 403):
                raise NightshiftUnauthorized(f"HTTP {ans.status}: the runtime refused the token")
            if ans.status != 200:
                raise NightshiftError(f"HTTP {ans.status}: {ans.body[:300]!r}")
            try:
                raw = json.loads(ans.body.decode("utf-8"))
                choice = raw["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise NightshiftError(f"not a chat completion: {ans.body[:300]!r}") from exc
        except NightshiftError as exc:
            self.log.write(**receipt, ok=False, error=str(exc))
            raise
        served = raw.get("model") if isinstance(raw.get("model"), str) else None
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        receipt.update({"ok": True, "served_model": served, "response_id": raw.get("id"),
                        "finish_reason": choice.get("finish_reason"), "usage": usage,
                        "response_sha256": hashlib.sha256(ans.body).hexdigest()})
        self.log.write(**receipt)
        content = message.get("content")
        return ChatResult(
            text=content if isinstance(content, str) else None,
            tool_calls=list(message.get("tool_calls") or []),
            finish_reason=choice.get("finish_reason"), route=route, served_model=served,
            usage=usage, receipt=receipt, raw=raw)


def detect(token: str, log: QueryLog, transport: Transport = urllib_transport) -> str | None:
    """The contract's Auto Detect: the first address that is online or unauthorized."""
    for port in DETECT_PORTS:
        for host in DETECT_HOSTS:
            client = NightshiftClient(f"http://{host}:{port}", token, log, transport)
            if client.status().state in ("online", "unauthorized"):
                return client.base_url
    return None
