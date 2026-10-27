"""A bare arm given an as-of date gets exactly Stemma's as_of tools and refusals
(CONTRACTS section 0d): no live web tool, Bash without network, Stemma's guard as a
hook, and Stemma's broker with only the evidence tools Stemma's worker holds. Hostile
cases try to get the live web back or to move the date."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hackathon.headtohead import bare
from hackathon.tests.test_headtohead import _env, _launch, _Sessions
from stemma.driver import canary
from stemma.driver.clean import HarnessError, bare_expectation, clean_launch

AS_OF = "2026-09-30"
EVIDENCE = {"capture", "capture_file", "archived_links", "sec_search", "market_series",
            "sec_filings",
            "sec_capture", "sec_facts", "outline", "read", "read_section", "search_store",
            "check_quote", "tables"}


def _settings(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    folder = tmp_path / "stemma2-run3" / "bare" / "20300101-000000"
    folder.mkdir(parents=True)
    return folder, bare.settings_extra(folder, tmp_path / "config", bare.SCOPE_PYTHON, AS_OF,
                                       Path(sys.executable))


def test_the_tools_are_stemmas_as_of_tools(tmp_path: Path) -> None:
    assert bare.tools_for({"as_of": AS_OF}) == ("Bash", "Read", "Write")
    assert bare.tools_for({"as_of": None}) == bare.TOOLS
    assert set(bare.broker_tools()) == EVIDENCE  # the worker's own evidence tools
    _folder, st = _settings(tmp_path)
    allow, deny = st["permissions"]["allow"], st["permissions"]["deny"]
    assert not any(a.startswith("Web") for a in allow)
    assert {"WebSearch", "WebFetch"} <= set(deny)
    assert {a for a in allow if a.startswith("mcp__")} == {f"mcp__stemma__{n}" for n in EVIDENCE}
    for other in ("file_claim", "record_claim_verdict", "finish_run", "tree_upsert",
                  "query_data", "submit_draft"):
        assert f"mcp__stemma__{other}" in deny, other
    sb = st["sandbox"]
    assert sb["network"] == {"allowedDomains": []} and sb["allowUnsandboxedCommands"] is False
    hook = st["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "WebSearch|WebFetch"
    assert hook["hooks"][0]["command"].endswith("-I -m stemma.hooks pre")
    assert hook["hooks"][0]["onFailure"] == "block"


def test_the_date_and_the_record_are_unwritable(tmp_path: Path) -> None:
    # Hostile: rewriting run.json would drop the date from the broker and the guard.
    folder, st = _settings(tmp_path)
    deny = st["permissions"]["deny"]
    for p in ("run.json", "store", "events.jsonl", ".mcp.json", ".settings.json"):
        assert f"Write(/{folder / p})" in deny and f"Write(/{folder / p}/**)" in deny, p
        assert str(folder / p) in st["sandbox"]["filesystem"]["denyWrite"], p


def test_the_guard_hook_refuses_the_live_web_in_the_folder(tmp_path: Path) -> None:
    folder, _st = _settings(tmp_path)
    (folder / "run.json").write_text(json.dumps({"run_id": "bare-x", "as_of": AS_OF}))
    hook = {"hook_event_name": "PreToolUse", "tool_name": "WebFetch",
            "tool_input": {"url": "https://www.sec.gov/"}, "session_id": "s1",
            "tool_use_id": "t1", "cwd": str(folder)}  # a bare session has no agent_type
    r = subprocess.run([sys.executable, "-I", "-m", "stemma.hooks", "pre"], input=json.dumps(hook),
                       capture_output=True, text=True, timeout=60, check=False,
                       env={"PATH": "/usr/bin:/bin", "STEMMA_RUN_DIR": str(folder)})
    assert r.returncode == 2 and f"this run is as of {AS_OF}" in r.stderr


def test_an_as_of_launch_serves_the_broker_over_the_folder(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch
                                                          ) -> None:
    from stemma.driver import clean

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(clean, "check_canary", lambda init, exp: [])
    monkeypatch.setenv("STEMMA_SEC_IDENTITY", "Test Builder test@example.org")
    sessions = _Sessions()
    folder = _launch(tmp_path, sessions, as_of=AS_OF)
    assert json.loads((folder / "run.json").read_text())["as_of"] == AS_OF
    cfg = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["stemma"]
    assert cfg["args"] == ["-I", "-m", "stemma.mcp_server"]
    assert cfg["env"]["STEMMA_RUN_DIR"] == str(folder)
    assert cfg["env"]["STEMMA_SEC_IDENTITY"] == "Test Builder test@example.org"
    for argv in sessions.calls:
        assert argv[argv.index("--mcp-config") + 1] == str(folder / ".mcp.json")
        assert "--strict-mcp-config" in argv
        assert argv[argv.index("--tools") + 1] == "Bash,Read,Write"
    launch = json.loads((folder / "launch.json").read_text())
    assert launch["as_of"] == AS_OF and set(launch["broker_tools"]) == EVIDENCE


def test_a_broker_control_that_is_not_archived_refuses_the_arm(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver import clean

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(clean, "check_canary", lambda init, exp: [])
    sessions = _Sessions(controls=False)
    with pytest.raises(bare.BareError, match="did not return an archived copy"):
        _launch(tmp_path, sessions, as_of=AS_OF)
    assert len(sessions.calls) == 1


def test_an_arm_without_a_date_is_unchanged(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver import clean

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(clean, "check_canary", lambda init, exp: [])
    sessions = _Sessions()
    folder = _launch(tmp_path, sessions, as_of=None)
    assert json.loads((folder / "launch.json").read_text())["tools"] == list(bare.TOOLS)
    assert not (folder / "run.json").exists() and not (folder / ".mcp.json").exists()
    assert all("--mcp-config" not in argv for argv in sessions.calls)
    assert "hooks" not in json.loads((folder / "launch.json").read_text())["settings_extra"]


def test_an_mcp_configuration_is_the_only_one_or_none(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="only with --strict-mcp-config"):
        clean_launch(tmp_path / "claude", tmp_path, "go", cwd=tmp_path,
                     model="claude-opus-5-5", effort="high", tools=["Read"],
                     settings_path=tmp_path / "s.json", mcp_config=tmp_path / "m.json")


def test_the_init_canary_holds_the_broker_to_its_own_tools(tmp_path: Path) -> None:
    exp = bare_expectation(cwd=tmp_path, tools=["Bash", "Read", "Write"],
                           harness_version="2.1.308", session_id="s1", mcp_server="stemma")
    init = {"type": "system", "subtype": "init", "cwd": str(tmp_path), "session_id": "s1",
            "tools": ["Bash", "Read", "Write", "mcp__stemma__capture"],
            "mcp_servers": [{"name": "stemma", "status": "connected"}],
            "model": "claude-opus-5-5", "claude_code_version": "2.1.308", "agents": [],
            "skills": [], "plugins": []}
    assert canary.check(init, exp) == []
    for tools, servers, needle in (
            (["Bash", "Read", "Write", "WebSearch", "mcp__stemma__capture"], ["stemma"],
             "built-in tools"),
            (["Bash", "Read", "Write", "mcp__other__fetch"], ["stemma"], "outside"),
            (["Bash", "Read", "Write"], ["stemma", "other"], "MCP servers")):
        bad = {**init, "tools": tools,
               "mcp_servers": [{"name": n, "status": "connected"} for n in servers]}
        assert any(needle in p for p in canary.check(bad, exp)), needle
