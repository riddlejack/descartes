"""The Nightshift client against fakes: an in-process transport, and one loopback server
that speaks the Runtime 0.90.0 contract. Neither touches the network."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from stemma.datalayer.querylog import QueryLog
from stemma.providers.nightshift import (
    HttpAnswer,
    NightshiftClient,
    NightshiftError,
    NightshiftOffline,
    NightshiftUnauthorized,
    detect,
    normalise_base_url,
    read_token,
)

TOKEN = "tok-invented-0001"
MODELS = {"object": "list", "data": [{"id": "meterless/auto"}, {"id": "meterless/deep"},
                                     {"id": "meterless/text.fast"}, {"id": "meterless/new-x"}]}


def completion(model: str = "claude-opus-5-5", **message: Any) -> dict[str, Any]:
    msg = {"role": "assistant", "content": "Forty-two."} | message
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


class Fake:
    def __init__(self, answers: dict[str, HttpAnswer] | None = None,
                 offline: bool = False) -> None:
        self.answers = answers or {}
        self.offline = offline
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str],
                 timeout: float) -> HttpAnswer:
        self.calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout,
                           "body": json.loads(body) if body else None})
        if self.offline:
            raise NightshiftOffline(f"no answer from {url}: refused")
        for suffix, ans in self.answers.items():
            if url.endswith(suffix):
                return ans
        return HttpAnswer(404, {}, b"not found")


def ok(data: Any, headers: dict[str, str] | None = None) -> HttpAnswer:
    return HttpAnswer(200, headers or {}, json.dumps(data).encode())


@pytest.mark.parametrize("pasted", [
    "127.0.0.1:8088", "http://127.0.0.1:8088", "http://127.0.0.1:8088/",
    "http://127.0.0.1:8088/v1", "http://127.0.0.1:8088/v1/", "127.0.0.1:8088/v1/v1",
    "http://127.0.0.1:8088/v1/chat/completions", "http://127.0.0.1:8088/v1/models",
    "http://127.0.0.1:8088/healthz",
])
def test_any_pasted_address_normalises_to_v1(pasted: str) -> None:
    assert normalise_base_url(pasted) == "http://127.0.0.1:8088/v1"


@pytest.mark.parametrize("bad", ["", "   ", "ftp://x/v1", "http://host/some/path"])
def test_bad_addresses_are_refused(bad: str) -> None:
    with pytest.raises(NightshiftError):
        normalise_base_url(bad)


def test_placeholder_token_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NightshiftUnauthorized, match="rt-local"):
        NightshiftClient("127.0.0.1:8088", "rt-local", QueryLog())
    (tmp_path / "proxy-token").write_text("rt-local\n")
    with pytest.raises(NightshiftUnauthorized, match="rt-local"):
        read_token({"METERLESS_STATE_DIR": str(tmp_path)})
    (tmp_path / "proxy-token").write_text(TOKEN + "\n")
    assert read_token({"METERLESS_STATE_DIR": str(tmp_path)}) == TOKEN
    with pytest.raises(NightshiftUnauthorized, match="no runtime token"):
        read_token({})


def test_status_states() -> None:
    log = QueryLog()
    assert NightshiftClient("h:1", TOKEN, log, Fake({"/models": ok(MODELS)})).status().state \
        == "online"
    assert NightshiftClient("h:1", TOKEN, log, Fake({"/models": HttpAnswer(401, {}, b"")})
                            ).status().state == "unauthorized"
    assert NightshiftClient("h:1", TOKEN, log, Fake({"/models": HttpAnswer(403, {}, b"")})
                            ).status().state == "unauthorized"
    assert NightshiftClient("h:1", TOKEN, log, Fake(offline=True)).status().state == "offline"
    no_routes = ok({"data": [{"id": "gpt-x"}]})
    assert NightshiftClient("h:1", TOKEN, log, Fake({"/models": no_routes})).status().state \
        == "offline"
    healthz_only = Fake({"/healthz": ok({"ok": True})})
    assert NightshiftClient("h:1", TOKEN, log, healthz_only).status().state == "offline"


def test_unknown_meterless_routes_are_kept() -> None:
    models = NightshiftClient("h:1", TOKEN, QueryLog(), Fake({"/models": ok(MODELS)})).models()
    assert "meterless/new-x" in models and "meterless/text.fast" in models


def test_chat_request_shape_headers_and_receipt(tmp_path: Path) -> None:
    log = QueryLog(tmp_path / "providers.jsonl")
    fake = Fake({"/chat/completions": ok(completion(), {"meterless-route": "deep/lane-2",
                                                        "server": "x"})})
    client = NightshiftClient("127.0.0.1:8088", TOKEN, log, fake)
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    out = client.chat([{"role": "user", "content": "secret question text"}], tools=tools,
                      max_tokens=50)
    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"] == "http://127.0.0.1:8088/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["meterless-app"] == "stemma"
    assert call["headers"]["meterless-scope"] == "user"
    assert call["timeout"] == 180.0
    assert call["body"]["model"] == "meterless/deep" and call["body"]["tools"] == tools
    assert out.text == "Forty-two." and out.served_model == "claude-opus-5-5"
    line = (tmp_path / "providers.jsonl").read_text()
    rec = json.loads(line)
    assert rec["route"] == "meterless/deep" and rec["served_model"] == "claude-opus-5-5"
    assert rec["runtime_headers"] == {"meterless-route": "deep/lane-2"}
    assert rec["usage"] == {"prompt_tokens": 12, "completion_tokens": 3}
    assert TOKEN not in line and "secret question text" not in line
    assert TOKEN not in repr(client)


def test_models_call_carries_app_header_but_not_scope() -> None:
    fake = Fake({"/models": ok(MODELS)})
    NightshiftClient("h:1", TOKEN, QueryLog(), fake).models()
    h = fake.calls[0]["headers"]
    assert h["meterless-app"] == "stemma" and "meterless-scope" not in h


def test_tool_calls_come_back_whole() -> None:
    tc = [{"id": "call_1", "type": "function",
           "function": {"name": "lookup", "arguments": "{\"q\": \"fleet\"}"}}]
    fake = Fake({"/chat/completions": ok(completion(content=None, tool_calls=tc))})
    out = NightshiftClient("h:1", TOKEN, QueryLog(), fake).chat([{"role": "user", "content": "x"}])
    assert out.text is None and out.tool_calls == tc


@pytest.mark.parametrize("answer,error", [
    (HttpAnswer(401, {}, b""), NightshiftUnauthorized),
    (HttpAnswer(500, {}, b"lane crashed"), NightshiftError),
    (HttpAnswer(200, {}, b"<html>"), NightshiftError),
    (HttpAnswer(200, {}, b'{"choices": []}'), NightshiftError),
])
def test_bad_answers_are_typed_and_logged(answer: HttpAnswer, error: type[Exception]) -> None:
    log = QueryLog()
    client = NightshiftClient("h:1", TOKEN, log, Fake({"/chat/completions": answer}))
    with pytest.raises(error):
        client.chat([{"role": "user", "content": "x"}])
    assert log.entries[-1]["ok"] is False


def test_offline_chat_is_logged_and_raised() -> None:
    log = QueryLog()
    with pytest.raises(NightshiftOffline):
        NightshiftClient("h:1", TOKEN, log, Fake(offline=True)).chat([{"role": "user",
                                                                       "content": "x"}])
    assert log.entries[-1]["ok"] is False


def test_route_must_be_a_meterless_mode() -> None:
    with pytest.raises(NightshiftError, match="routing mode"):
        NightshiftClient("h:1", TOKEN, QueryLog(), Fake()).chat([], route="claude-opus-5-5")


def test_auto_detect_order_and_unauthorized_counts() -> None:
    seen: list[str] = []

    def transport(method: str, url: str, body: bytes | None, headers: dict[str, str],
                  timeout: float) -> HttpAnswer:
        seen.append(url)
        if ":8787/" in url and "localhost" in url:
            return HttpAnswer(401, {}, b"")
        raise NightshiftOffline("refused")

    assert detect(TOKEN, QueryLog(), transport) == "http://localhost:8787/v1"
    assert seen[:3] == ["http://127.0.0.1:8088/v1/models", "http://localhost:8088/v1/models",
                        "http://[::1]:8088/v1/models"]


# A loopback server that speaks the contract, to exercise the real urllib transport.
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _json(self, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("meterless-lane", "deep")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/v1/models" and self._auth():
            self._json(MODELS)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions" or not self._auth():
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers.get("meterless-app") == "stemma"
        self._json(completion(model="served-by-" + req["model"].split("/")[1]))


@pytest.fixture
def server() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_against_a_loopback_fake_runtime(server: str) -> None:
    log = QueryLog()
    good = NightshiftClient(server, TOKEN, log)
    assert good.status().state == "online"
    out = good.chat([{"role": "user", "content": "hi"}], route="meterless/fast")
    assert out.served_model == "served-by-fast" and out.text == "Forty-two."
    assert log.entries[-1]["runtime_headers"] == {"meterless-lane": "deep"}
    assert NightshiftClient(server, "wrong-token", log).status().state == "unauthorized"


def test_no_response_body_in_errors_or_logs(tmp_path: Path) -> None:
    echo = f"lane failed; you sent Bearer {TOKEN} and PRIVATE-PROMPT-TEXT".encode()
    log = QueryLog(tmp_path / "p.jsonl")
    client = NightshiftClient("h:1", TOKEN, log,
                              Fake({"/chat/completions": HttpAnswer(500, {}, echo)}))
    with pytest.raises(NightshiftError) as e:
        client.chat([{"role": "user", "content": "PRIVATE-PROMPT-TEXT"}])
    text = str(e.value) + (tmp_path / "p.jsonl").read_text()
    assert "PRIVATE-PROMPT-TEXT" not in text and TOKEN not in text
    assert "bytes, sha256" in str(e.value)
