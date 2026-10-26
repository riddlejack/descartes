"""The generic RPC server the conformance bridge talks to (no World Model logic)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest


class Client:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def recv(self) -> Any:
        assert self.proc.stdout is not None
        return json.loads(self.proc.stdout.readline())

    def call(self, target: int, name: str | None, *args: Any) -> Any:
        self.send({"op": "call", "target": target, "name": name, "args": list(args)})
        return self.recv()


@pytest.fixture
def client() -> Iterator[Client]:
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "stemma.register.wm.rpc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    yield Client(proc)
    assert proc.stdin is not None
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0


def test_calls_package_functions_by_name(client: Client) -> None:
    assert client.call(0, "normalizeName", "José  García") == {
        "op": "result",
        "value": "jose garcia",
    }


def test_objects_come_back_as_refs_and_errors_as_typed_errors(client: Client) -> None:
    client.send({"op": "get", "target": 0, "name": "WorldModel"})
    cls = client.recv()["value"]["$ref"]
    world = client.call(cls["id"], None, {"storage": "memory"})["value"]["$ref"]
    assert world["type"] == "WorldModel" and "upsertEntity" in world["methods"]
    err = client.call(world["id"], "upsertEntity", {"type": "x", "name": "n"})
    assert err["op"] == "error" and err["error"]["type"] == "ProvenanceError"
    client.send({"op": "get", "target": world["id"], "name": "then"})
    assert client.recv()["value"] == {"$undef": 1}


def test_function_arguments_call_back_to_the_client(client: Client) -> None:
    client.send({"op": "get", "target": 0, "name": "WorldModel"})
    cls = client.recv()["value"]["$ref"]["id"]
    world = client.call(cls, None, {"storage": "memory", "clock": {"$fn": 7}})["value"]["$ref"]["id"]
    src = {"kind": "manual", "at": "2026-01-01T00:00:00Z"}
    for name in ("Jerome Example abc", "Jerome Example x"):
        client.call(world, "upsertEntity", {"type": "p", "name": name, "source": src})
    (proposal,) = client.call(world, "mergeQueue")["value"]
    client.send({"op": "call", "target": world, "name": "approveMerge", "args": [proposal["id"], "op"]})
    callback = client.recv()
    assert callback == {"op": "callback", "fn": 7, "args": []}
    client.send({"op": "return", "value": {"$date": "2031-01-01T00:00:00.000Z"}})
    done = client.recv()
    assert done["op"] == "result" and done["value"]["resolvedAt"] == "2031-01-01T00:00:00.000Z"


def test_reserved_keys_are_escaped(client: Client) -> None:
    client.send({"op": "get", "target": 0, "name": "canonicalJson"})
    fn = client.recv()["value"]["$ref"]["id"]
    out = client.call(fn, None, {"$map": {"$ref": 1}})
    assert out["value"] == '{"$ref":1}'
