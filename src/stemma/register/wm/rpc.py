"""Generic JSON-lines RPC over stdin/stdout, serving this package by name.

    python -I -m stemma.register.wm.rpc

It knows nothing about World Model: it calls whatever attribute it is asked to,
on the package or on an object an earlier call returned, and sends back the value.
The conformance bridge (`conformance/bridge.ts`) is its only client.

Messages, one JSON object per line:
  client -> server   {"op": "call", "target": <ref>, "name": <str|null>, "args": [...]}
                     {"op": "get", "target": <ref>, "name": <str>}
                     {"op": "release", "ids": [<ref>, ...]}
                     {"op": "return", "value": ...} | {"op": "throw", "error": {...}}
                       (replies to a callback)
  server -> client   {"op": "result", "value": ...}
                     {"op": "error", "error": {"type", "message", "traceback"}}
                     {"op": "callback", "fn": <id>, "args": [...]}

Ref 0 is the package. `name: null` calls the target itself (a class, a function).

Values are JSON, except these tagged objects (a single reserved key):
  {"$ref": {"id", "type", "methods"}}   a Python object that is not JSON data
  {"$fn": <id>}                         a client function; calling it here sends a
                                        "callback" and blocks until the reply
  {"$date": <ISO string>}               a datetime (a JavaScript Date)
  {"$undef": 1}                         a missing attribute (JavaScript undefined)
  {"$map": {...}}                       a plain object whose one key is reserved
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import IO, Any

RESERVED = frozenset({"$ref", "$fn", "$date", "$undef", "$map"})


class RemoteError(Exception):
    """A client function called back from here threw."""


class Server:
    def __init__(self, module_name: str, inp: IO[str], out: IO[str]) -> None:
        self._inp, self._out = inp, out
        self._objects: dict[int, Any] = {0: importlib.import_module(module_name)}
        self._next = 1

    # ------------------------------------------------------------ encoding
    def encode(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=UTC)
            return {"$date": dt.astimezone(UTC).isoformat().replace("+00:00", "Z")}
        if isinstance(value, date):
            return {"$date": value.isoformat() + "T00:00:00Z"}
        if isinstance(value, (list, tuple)):
            return [self.encode(v) for v in value]
        if isinstance(value, dict) and all(isinstance(k, str) for k in value):
            body = {k: self.encode(v) for k, v in value.items()}
            return {"$map": body} if len(body) == 1 and next(iter(body)) in RESERVED else body
        ref = self._next
        self._next += 1
        self._objects[ref] = value
        methods = sorted(
            n for n in dir(value) if not n.startswith("_") and callable(getattr(value, n, None))
        )
        return {"$ref": {"id": ref, "type": type(value).__name__, "methods": methods}}

    def decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if not isinstance(value, dict):
            return value
        if len(value) == 1:
            (key, inner), = value.items()
            if key == "$map":
                return {k: self.decode(v) for k, v in inner.items()}
            if key == "$date":
                return datetime.fromisoformat(inner)
            if key == "$undef":
                return None
            if key == "$fn":
                return self._client_function(inner)
            if key == "$ref":
                return self._objects[inner["id"] if isinstance(inner, dict) else inner]
        return {k: self.decode(v) for k, v in value.items()}

    def _client_function(self, fn_id: int) -> Callable[..., Any]:
        def call_client(*args: Any) -> Any:
            self._send({"op": "callback", "fn": fn_id, "args": [self.encode(a) for a in args]})
            return self.serve(until_reply=True)

        call_client.__name__ = f"client_function_{fn_id}"
        return call_client

    # ------------------------------------------------------------ transport
    def _send(self, message: dict[str, Any]) -> None:
        self._out.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
        self._out.flush()

    def serve(self, until_reply: bool = False) -> Any:
        """Answer requests until stdin closes, or (inside a callback) until the
        client's reply arrives; requests the client makes meanwhile nest."""
        for line in self._inp:
            if not line.strip():
                continue
            message = json.loads(line)
            op = message.get("op")
            if op == "return":
                if not until_reply:
                    raise RuntimeError("rpc: a callback reply arrived with no callback pending")
                return self.decode(message.get("value"))
            if op == "throw":
                err = message.get("error") or {}
                raise RemoteError(f"{err.get('name', 'Error')}: {err.get('message', '')}")
            self._send(self._handle(message))
        if until_reply:
            raise RuntimeError("rpc: client closed the connection during a callback")
        return None

    def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        op = message.get("op")
        try:
            if op == "release":
                for ref in message.get("ids", []):
                    if ref != 0:
                        self._objects.pop(ref, None)
                return {"op": "result", "value": None}
            target = self._objects[message["target"]]
            name: str | None = message.get("name")
            if op == "get":
                if name is None or not hasattr(target, name):
                    return {"op": "result", "value": {"$undef": 1}}
                return {"op": "result", "value": self.encode(getattr(target, name))}
            if op == "call":
                fn = target if name is None else getattr(target, name)
                args = [self.decode(a) for a in message.get("args", [])]
                return {"op": "result", "value": self.encode(fn(*args))}
            raise ValueError(f"rpc: unknown op {op!r}")
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the client
            return {
                "op": "error",
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            }


def main() -> None:
    # The protocol owns the real stdout; anything else printed goes to stderr.
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    package = (__spec__.parent if __spec__ is not None else None) or "stemma.register.wm"
    with open(sys.stdin.fileno(), encoding="utf-8", closefd=False) as inp:
        Server(package, inp, out).serve()


if __name__ == "__main__":
    main()
