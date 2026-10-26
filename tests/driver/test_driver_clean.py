"""The one clean launch (`stemma.driver.clean`) for the driver and every other runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from driverkit import Fake

from stemma.driver import clean
from stemma.driver.assemble import assemble
from stemma.driver.errors import HarnessError
from stemma.driver.task import fill_switches

OS_ADDED_ENV = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}  # macOS adds these in every process


def _launch(fake: Fake, tmp: Path, **kw: Any) -> clean.CleanLaunch:
    args: dict[str, Any] = {"cwd": tmp, "model": "claude-opus-5-5", "effort": "high",
                            "tools": ["Read", "Bash"], "settings_path": tmp / "s" / "x.json"}
    args.update(kw)
    return clean.clean_launch(fake.path, tmp / "config", "claim:c1", **args)


def test_plugin_launch_shape(fake: Fake, tmp: Path) -> None:
    cl = _launch(fake, tmp, plugin_dir=tmp / "plug", agent="stemma:claim-checker",
                 session_id="sid-1", env_extra={"STEMMA_RUN_DIR": "/r"},
                 settings_extra={"permissions": {"deny": ["WebFetch"]}})
    assert cl.argv == [
        str(fake.path), "-p", "claim:c1", "--output-format", "stream-json", "--verbose",
        "--agent", "stemma:claim-checker", "--model", "claude-opus-5-5", "--effort", "high",
        "--plugin-dir", str(tmp / "plug"), "--settings", str(tmp / "s" / "x.json"),
        "--tools", "Read,Bash", "--session-id=sid-1"]
    assert set(cl.env) == {"HOME", "USER", "PATH", "LANG", "CLAUDE_CONFIG_DIR",
                           "STEMMA_RUN_DIR"}
    written = json.loads((tmp / "s" / "x.json").read_text())
    assert written == cl.settings
    for k, v in clean.CANARY_SETTINGS.items():
        assert written[k] == v
    assert written["availableModels"] == ["claude-opus-5-5", "claude-fable-5-1"]
    assert written["permissions"] == {"deny": ["WebFetch"]}
    r = _launch(fake, tmp, plugin_dir=tmp / "plug", session_id="sid-1", resume=True)
    assert r.argv[-1] == "--resume=sid-1"


@pytest.mark.parametrize("extra,needle", [
    ({"CLAUDE_CODE_EFFORT_LEVEL": "low"}, "never set"),
    ({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, "the probes proved"),
    ({"CLAUDECODE": "1"}, "the probes proved"),
    ({"ANTHROPIC_API_KEY": "x"}, "redirect"),
    ({"ANTHROPIC_BASE_URL": "http://x"}, "redirect"),
    ({"HOME": "/tmp"}, "may not change HOME"),
    ({"CLAUDE_CONFIG_DIR": "/other"}, "may not change"),
])
def test_env_extra_refusals(fake: Fake, tmp: Path, extra: dict[str, str], needle: str) -> None:
    with pytest.raises(HarnessError, match=needle):
        _launch(fake, tmp, env_extra=extra)


@pytest.mark.parametrize("kw,needle", [
    ({"settings_extra": {"syncClaudeAiPlugins": True}}, "canary setting"),
    ({"settings_extra": {"availableModels": ["claude-haiku-4-5"]}}, "canary setting"),
    ({"plugin_dir": Path("/p"), "strict_mcp_config": True}, "not allowed with a plugin"),
    ({"plugin_dir": Path("/p"), "disable_slash_commands": True}, "not allowed with a plugin"),
    ({"agent": "stemma:lead"}, "needs the plugin"),
    ({"model": "claude-haiku-4-5"}, "model must be"),
    ({"effort": "ultra"}, "effort must be"),
    ({"tools": ["mcp__plugin_stemma_stemma__ping"]}, "built-in tool names"),
    ({"resume": True}, "session id"),
])
def test_launch_refusals(fake: Fake, tmp: Path, kw: dict[str, Any], needle: str) -> None:
    with pytest.raises(HarnessError, match=needle):
        _launch(fake, tmp, **kw)


def test_never_bare_and_bad_prompt(fake: Fake, tmp: Path) -> None:
    for kw in ({}, {"plugin_dir": tmp}, {"strict_mcp_config": True}):
        assert "--bare" not in _launch(fake, tmp, **kw).argv
    with pytest.raises(HarnessError):
        clean.clean_launch(fake.path, tmp, "--bare", cwd=tmp, model="claude-opus-5-5",
                           effort="high", tools=[], settings_path=tmp / "y.json")


def _run(cl: clean.CleanLaunch, cwd: Path) -> dict[str, Any]:
    out = subprocess.run(cl.argv, env=cl.env, cwd=cwd, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, check=True,
                         timeout=60)  # resource limit: the fake answers at once
    init = json.loads(out.stdout.splitlines()[0])
    assert isinstance(init, dict)
    return init


def test_bare_baseline_launch_and_canary(fake: Fake, tmp: Path) -> None:
    (tmp / "config").mkdir()
    fake.configure({})
    cl = _launch(fake, tmp, tools=["Read", "Bash", "WebFetch"], session_id="sid-b",
                 strict_mcp_config=True, disable_slash_commands=True,
                 env_extra={"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1"})
    assert "--strict-mcp-config" in cl.argv and "--plugin-dir" not in cl.argv
    assert "--agent" not in cl.argv
    init = _run(cl, tmp)
    env = {k for k in fake.calls()[0]["env"] if k not in OS_ADDED_ENV}
    assert env == {"HOME", "USER", "PATH", "LANG", "CLAUDE_CONFIG_DIR",
                   "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"}
    exp = clean.bare_expectation(cwd=tmp, tools=["Read", "Bash", "WebFetch"],
                                 harness_version="2.1.308 (Claude Code)", session_id="sid-b")
    assert clean.check_canary(init, exp) == []
    for change, needle in (
        ({"plugins": [{"name": "engineering", "path": "/x"}]}, "plugins are"),
        ({"mcp_servers": [{"name": "claude.ai Docs", "status": "connected"}]}, "MCP servers"),
        ({"tools": ["Read", "Bash", "WebFetch", "mcp__claude_ai_Docs__read"]}, "no MCP server"),
        ({"agents": ["general-purpose"]}, "agents are"),
        ({"skills": ["pdf"]}, "skills are"),
    ):
        bad = {**init, **change}
        assert any(needle in p for p in clean.check_canary(bad, exp)), change


def test_stemma_expectation_matches_plugin_launch(fake: Fake, tmp: Path,
                                                  plugin_src: Path) -> None:
    (tmp / "config").mkdir()
    fake.configure({})
    asm = assemble(plugin_src, tmp / "plug", fill_switches(None))
    cl = _launch(fake, tmp, plugin_dir=asm.plugin_dir, agent="stemma:claim-checker",
                 tools=["Bash", "Read", "ToolSearch"], session_id="sid-s")
    init = _run(cl, tmp)
    exp = clean.stemma_expectation(
        cwd=tmp, plugin_dir=asm.plugin_dir, agents=asm.agents, skills=asm.skills,
        main_tools=["Bash", "Read"], harness_version="2.1.308", session_id="sid-s")
    assert clean.check_canary(init, exp) == []
    assert any("plugins are" in p for p in clean.check_canary(
        {**init, "plugins": []}, exp))
