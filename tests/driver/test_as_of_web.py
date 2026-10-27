"""An as_of run's sessions have no live web tool (CONTRACTS section 0d): WebSearch and
WebFetch are left out of every session's --tools and denied in its settings, for every
role of the real plugin, while the shell stays without network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from driverkit import Fake, write_task

from stemma.driver import frontmatter
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.driver.settings import (
    build_settings,
    split_tools,
    stemma_settings_extra,
    tools_for_role,
    without_live_web,
)
from stemma.register import checks_api

AS_OF = "2026-10-19"
REPO_PLUGIN = Path(__file__).resolve().parents[2] / "plugin"


def _real_agent_tools() -> dict[str, list[str]]:
    out = {}
    for p in sorted((REPO_PLUGIN / "agents").glob("*.md")):
        out[p.stem] = frontmatter.parse(p.read_text(encoding="utf-8"), str(p)).tools()
    return out


def test_no_role_of_the_real_plugin_keeps_a_live_web_tool() -> None:
    agents = _real_agent_tools()
    assert any("WebSearch" in t for t in agents.values())  # the plugin does use it live
    for role in agents:
        t = without_live_web(tools_for_role(role, agents))
        for names in (t.builtin, t.allow, t.main):
            assert not any(n.split("(", 1)[0] in ("WebSearch", "WebFetch") for n in names), (
                role, names)


def test_a_rule_form_of_a_web_tool_is_dropped_too() -> None:
    # Hostile: an agent file naming the tool as a rule must not keep it.
    t = without_live_web(split_tools([["Read", "WebSearch(domain:example.org)",
                                       "WebFetch(domain:example.org)"]]))
    assert t.builtin == ("Read", "ToolSearch") and t.allow == ("Read", "ToolSearch")


def test_as_of_settings_deny_both_and_keep_the_shell_offline(tmp_path: Path) -> None:
    tools = split_tools([["Read", "Bash", "WebSearch"]])
    s = stemma_settings_extra(tmp_path, "worker", "run:x", tools, tmp_path, as_of=AS_OF)
    assert {"WebSearch", "WebFetch"} <= set(s["permissions"]["deny"])
    sb = s["sandbox"]
    assert sb["network"] == {"allowedDomains": []}
    assert sb["allowUnsandboxedCommands"] is False and sb["failIfUnavailable"] is True
    live = stemma_settings_extra(tmp_path, "worker", "run:x", tools, tmp_path)
    assert "WebSearch" not in live["permissions"]["deny"]  # a live run keeps it
    merged = build_settings(tmp_path, "worker", "run:x", tools, tmp_path)
    assert merged["sandbox"]["network"] == {"allowedDomains": []}


def _settings_of(argv: list[str]) -> dict[str, Any]:
    return dict(json.loads(Path(argv[argv.index("--settings") + 1]).read_text()))


def test_an_as_of_runs_sessions_get_no_live_web_tool(cfg: DriverConfig, tmp: Path,
                                                     fake: Fake, limits: Limits) -> None:
    fake.configure({"lead": {"subagents": [{"agentType": "stemma:worker",
                                            "model": "claude-opus-5-5", "effort": "high",
                                            "depth": 1}],
                             "write_files": {"intake.md": "job"}}})
    run = create_run(cfg, write_task(tmp, as_of=AS_OF), limits=limits)
    assert json.loads((run / "run.json").read_text())["as_of"] == AS_OF
    assert start(cfg, run, checks_api, limits, released=lambda _r: True) == 0
    calls = fake.calls()
    assert {c["role"] for c in calls} >= {"lead", "blind-framer"}
    for c in calls:
        argv = c["argv"]
        tools = argv[argv.index("--tools") + 1].split(",")
        assert "WebSearch" not in tools and "WebFetch" not in tools, (c["role"], tools)
        s = _settings_of(argv)
        assert {"WebSearch", "WebFetch"} <= set(s["permissions"]["deny"]), c["role"]
        assert not any(a.startswith("Web") for a in s["permissions"]["allow"]), c["role"]
        assert s["sandbox"]["network"] == {"allowedDomains": []}


def test_a_live_run_keeps_websearch(cfg: DriverConfig, tmp: Path, fake: Fake,
                                    limits: Limits) -> None:
    fake.configure({"lead": {"write_files": {"intake.md": "job"}}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    assert start(cfg, run, checks_api, limits, released=lambda _r: True) == 0
    lead = next(c for c in fake.calls() if c["role"] == "lead")
    assert "WebSearch" in lead["argv"][lead["argv"].index("--tools") + 1].split(",")
