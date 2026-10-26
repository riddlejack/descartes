"""The browser fallback, the Docling table view and the MCP registration."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeTransport, Reply, html

from stemma.evidence import tables as tables_mod
from stemma.evidence import tools
from stemma.evidence.render import Rendered
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
SPA = b"<html><head><script src='/app.js'></script></head><body><div id='root'></div></body></html>"
FULL = html("<h1>Invented dashboard</h1>" + "<p>Orders rose 12% to 4,410.</p>" * 10)


class FakeRenderer:
    name, version = "fake-browser", "0"

    def __init__(self, result: Rendered | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    def render(self, url: str) -> Rendered:
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_fallback_runs_only_on_loud_text(make_service: Factory) -> None:
    renderer = FakeRenderer(Rendered(FULL, "https://app.example.org/", ["http://10.0.0.1/x"]))
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], SPA),
                       "https://ok.example.org/": Reply(200, [("Content-Type", "text/html")], FULL)})
    svc = make_service(t, renderer=renderer)
    r = svc.capture("https://app.example.org/", "dashboard")
    assert r["kind"] == "rendered" and r["status"] == "ok"
    plain = svc.store.get_receipt(r["fallback_of"])
    assert plain["status"] == "empty_text"  # the plain capture is kept, loud
    assert r["renderer"]["name"] == "fake-browser"
    assert r["warnings"] == ["browser_requests_refused: 1"]
    assert svc.check_quote(r["receipt_id"], "Orders rose 12% to 4,410.")["found"]
    assert svc.capture("https://ok.example.org/", "fine page")["kind"] == "web"
    assert renderer.calls == ["https://app.example.org/"]


def test_fallback_failure_is_recorded(make_service: Factory) -> None:
    renderer = FakeRenderer(RuntimeError("browser crashed"))
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], SPA)})
    r = make_service(t, renderer=renderer).capture("https://app.example.org/", "dashboard")
    assert r["status"] == "empty_text"
    assert any(w.startswith("browser_fallback_failed: receipt r_") for w in r["warnings"])


def test_fallback_absent_is_warned(make_service: Factory) -> None:
    t = FakeTransport({"https://app.example.org/": Reply(200, [("Content-Type", "text/html")], SPA)})
    r = make_service(t, renderer=None).capture("https://app.example.org/", "dashboard")
    assert r["status"] == "empty_text" and "browser_fallback_not_installed" in r["warnings"]


def test_tables_not_installed(make_service: Factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tables_mod, "docling_version", lambda: None)
    t = FakeTransport({"https://ok.example.org/": Reply(200, [("Content-Type", "text/html")], FULL)})
    svc = make_service(t)
    r = svc.capture("https://ok.example.org/", "page")
    assert svc.tables(r["sha256"])["status"] == "not_installed"


def test_tables_with_fake_docling(make_service: Factory, monkeypatch: pytest.MonkeyPatch,
                                  run_dir: Path) -> None:
    monkeypatch.setattr(tables_mod, "docling_version", lambda: "0-fake")
    calls: list[int] = []

    def fake_convert(body: bytes, content_type: str) -> list[dict[str, Any]]:
        calls.append(len(body))
        return [{"index": 0, "page": None, "markdown": "| a | b |\n|---|---|\n| 1 | 2 |"}]

    monkeypatch.setattr(tables_mod, "convert_tables", fake_convert)
    t = FakeTransport({"https://ok.example.org/": Reply(200, [("Content-Type", "text/html")], FULL)})
    svc = make_service(t)
    sha = svc.capture("https://ok.example.org/", "page")["sha256"]
    first = svc.tables(sha)
    second = svc.tables(sha)
    assert first["status"] == "ok" and first["tables"][0]["markdown"].startswith("| a")
    assert "never quote" in first["note"] and second["tables"] == first["tables"]
    assert len(calls) == 1  # converted once, then read from store/derived


def test_register_tools_on_real_server(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("stemma-test")
    tools.register_tools(server)
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == {"capture", "capture_file", "sec_filings", "sec_capture", "sec_facts",
                     "outline", "read", "read_section", "search_store", "check_quote", "tables"}


def test_tool_wrappers_return_typed_errors(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEMMA_RUN_DIR", raising=False)
    assert tools.read("0" * 64)["error"] == "no_run_dir"
    monkeypatch.setenv("STEMMA_RUN_DIR", str(run_dir))
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))  # no browser
    tools._SERVICES.clear()
    assert tools.read("nothex")["error"] == "bad_sha256"
    assert tools.check_quote("r_20990101T000000_000000000000", "x")["error"] == "unknown_receipt"
    r = tools.capture_file("task/attachments/none.txt", "probe")
    assert r["status"] == "file_not_found"
