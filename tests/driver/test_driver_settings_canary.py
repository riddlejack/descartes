"""Per-session settings content, tool split, read denials, and the canary's checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stemma.driver import canary
from stemma.driver.errors import AgentFileInvalid
from stemma.driver.settings import build_settings, split_tools, tools_for_role

RUN = Path("/private/tmp/run-x")
CONFIG = Path("/cfg")


def _settings(role: str, prompt: str = "claim:abc", run: Path = RUN) -> dict[str, Any]:
    tools = split_tools([["Read", "Bash", "mcp__plugin_stemma_stemma__get_claim"]])
    return build_settings(run, role, prompt, tools, CONFIG)


def test_canary_settings_and_denials() -> None:
    s = _settings("claim-checker")
    assert s["syncClaudeAiPlugins"] is False and s["syncClaudeAiSkills"] is False
    assert s["disableClaudeAiConnectors"] is True and s["disableBundledSkills"] is True
    assert s["skillOverrides"] == {"design": "off", "doctor": "off"}
    assert set(s["enabledPlugins"]) == {"cc-plugin-agents-md@builtin",
                                        "cc-plugin-telemetry@builtin",
                                        "cc-plugin-plugin-authoring@builtin"}
    assert not any(s["enabledPlugins"].values())
    assert s["availableModels"] == ["claude-opus-5-5", "claude-fable-5-1"]
    deny = s["permissions"]["deny"]
    for d in ("WebFetch", "Agent(fork)", "Agent(general-purpose)", "Agent(Explore)",
              "Agent(Plan)", "Agent(claude)", "Agent(statusline-setup)",
              "Agent(claude-code-guide)"):
        assert d in deny
    for tool in ("Write", "Edit"):
        for p in ("store/**", "register/**", "tree.json", "events.jsonl", "run.json",
                  "status.json", "status.html", "task/**", "CLAUDE.md", ".claude/**",
                  ".stemma/**", "branches/**/verdicts/**", "deliverable/answer.reader.md"):
            assert f"{tool}(//private/tmp/run-x/{p})" in deny, (tool, p)
    sb = s["sandbox"]
    assert sb["enabled"] and sb["failIfUnavailable"] and sb["allowUnsandboxedCommands"] is False
    assert sb["network"] == {"allowedDomains": []}
    assert "/private/tmp/run-x/register" in sb["filesystem"]["denyWrite"]
    assert "/private/tmp/run-x/branches/**/verdicts" in sb["filesystem"]["denyWrite"]
    assert s["permissions"]["allow"] == ["Bash", "Read", "ToolSearch",
                                         "mcp__plugin_stemma_stemma__get_claim"]


def test_claim_checker_is_blind() -> None:
    s = _settings("claim-checker")
    deny, dr = s["permissions"]["deny"], s["sandbox"]["filesystem"]["denyRead"]
    for p, rule in (("frame.md", "frame.md"), ("draft", "draft/**"),
                    ("branches/**/record.md", "branches/**/record.md"),
                    ("branches/**/order.md", "branches/**/order.md")):
        assert f"/private/tmp/run-x/{p}" in dr
        assert f"Read(//private/tmp/run-x/{rule})" in deny
    # Nobody reads another session's transcript or raw stream.
    assert "Read(//cfg/projects/**)" in deny and "/cfg/projects" in dr
    assert "/private/tmp/run-x/.stemma/sessions" in dr


def test_framer_blind_only_for_own_frame() -> None:
    own = _settings("blind-framer", "frame:own")["sandbox"]["filesystem"]["denyRead"]
    crit = _settings("blind-framer", "frame:critique")["sandbox"]["filesystem"]["denyRead"]
    for p in ("frame.md", "tree.json", "branches"):
        assert f"/private/tmp/run-x/{p}" in own
        assert f"/private/tmp/run-x/{p}" not in crit


def test_lead_reads_its_run(tmp: Path) -> None:
    dr = _settings("lead", "run:x")["sandbox"]["filesystem"]["denyRead"]
    assert dr == ["/private/tmp/run-x/.stemma/sessions", "/cfg/projects"]


def test_deciders_eye_reads_only_task_intake_and_its_draft(tmp: Path) -> None:
    run = tmp / "run"
    for rel in ("task/question.md", "intake.md", "frame.md", "draft/answer.v1.md",
                "draft/answer.v2.md", "draft/notes.md", "reviews/v1/deciders-eye.md",
                "reviews/v2/mechanical.md", "odd-new-file.md", "branches/b1/record.md"):
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        (run / rel).write_text("x")
    s = build_settings(run, "deciders-eye-reviewer", "draft:2",
                       split_tools([["Read", "Write"]]), CONFIG)
    deny = s["permissions"]["deny"]
    denied = {r[len("Read(/"):-1] for r in deny if r.startswith("Read(")}
    r = str(run)
    for must in ("frame.md", "branches/**", "odd-new-file.md", "draft/answer.v1.md",
                 "draft/notes.md", "reviews/v1/**", "reviews/v2/mechanical.md",
                 "reviews/v2/evidence-audit.md", "tree.json"):
        assert f"{r}/{must}" in denied, must
    for allowed in ("task/**", "intake.md", "draft/answer.v2.md", "draft/**"):
        assert f"{r}/{allowed}" not in denied, allowed
    fs = s["sandbox"]["filesystem"]
    assert fs["denyRead"] == [r, "/cfg/projects"]
    assert fs["allowRead"] == [f"{r}/task", f"{r}/intake.md", f"{r}/draft/answer.v2.md"]
    with pytest.raises(AgentFileInvalid):
        build_settings(run, "deciders-eye-reviewer", "draft:x", split_tools([]), CONFIG)


def test_only_deciders_eye_gets_allow_read() -> None:
    for role, prompt in (("claim-checker", "claim:a"), ("lead", "run:x"),
                         ("blind-framer", "frame:own")):
        fs = _settings(role, prompt)["sandbox"]["filesystem"]
        assert "allowRead" not in fs and str(RUN) not in fs["denyRead"]


def test_tool_split() -> None:
    t = split_tools([["Read", "Agent(stemma:worker)", "Bash(git log:*)",
                      "mcp__plugin_stemma_stemma__file_claim"]])
    assert t.builtin == ("Agent", "Bash", "Read", "ToolSearch")
    assert "Agent(stemma:worker)" in t.allow and "Bash(git log:*)" in t.allow
    assert "mcp__plugin_stemma_stemma__file_claim" in t.allow
    assert "mcp__plugin_stemma_stemma__file_claim" not in t.builtin
    with pytest.raises(AgentFileInvalid, match="outside the stemma server"):
        split_tools([["mcp__claude_ai_Docs__read"]])
    with pytest.raises(AgentFileInvalid):
        split_tools([["Read; rm -rf"]])


def test_lead_session_covers_worker_tools() -> None:
    tools = {"lead": ["Read", "Agent(stemma:worker)"],
             "worker": ["WebSearch", "mcp__plugin_stemma_stemma__file_claim"],
             "claim-checker": ["Bash"]}
    t = tools_for_role("lead", tools)
    assert "WebSearch" in t.builtin and "mcp__plugin_stemma_stemma__file_claim" in t.allow
    assert "Bash" not in t.builtin
    assert t.main == ("Agent", "Read")  # what init lists for the lead's own thread
    assert tools_for_role("claim-checker", tools).builtin == ("Bash", "ToolSearch")
    with pytest.raises(AgentFileInvalid):
        tools_for_role("evidence-auditor", tools)


# ----- canary -------------------------------------------------------------------------

EXP = canary.Expectation(
    cwd=RUN, plugin_dir=RUN / ".stemma/plugin", agents=frozenset({"lead", "worker"}),
    skills=frozenset({"decomposition-method"}),
    builtin_tools=frozenset({"Agent", "Read", "ToolSearch"}), harness_version="2.1.308",
    session_id="s1")


def good_init() -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "cwd": str(RUN), "session_id": "s1",
            "tools": ["Task", "Read", "ToolSearch", "mcp__plugin_stemma_stemma__ping"],
            "mcp_servers": [{"name": "plugin:stemma:stemma", "status": "connected"}],
            "model": "claude-opus-5-5", "claude_code_version": "2.1.308",
            "agents": ["stemma:lead", "stemma:worker"], "skills": ["decomposition-method"],
            "plugins": [{"name": "stemma", "path": str(RUN / ".stemma/plugin")}]}


def test_canary_passes() -> None:
    assert canary.check(good_init(), EXP) == []
    g = good_init()
    g["skills"] = ["stemma:decomposition-method"]
    assert canary.check(g, EXP) == []


@pytest.mark.parametrize("change,needle", [
    ({"plugins": [{"name": "stemma", "path": str(RUN / ".stemma/plugin")},
                  {"name": "engineering", "path": "/x"}]}, "plugins"),
    ({"plugins": [{"name": "stemma", "path": "/repo/plugin"}]}, "plugin path"),
    ({"agents": ["stemma:lead", "stemma:worker", "general-purpose"]}, "outside the plugin"),
    ({"agents": ["stemma:lead"]}, "agents are"),
    ({"mcp_servers": [{"name": "plugin:stemma:stemma", "status": "connected"},
                      {"name": "claude.ai Docs", "status": "connected"}]}, "MCP servers"),
    ({"mcp_servers": [{"name": "plugin:stemma:stemma", "status": "failed"}]}, "status"),
    ({"skills": ["decomposition-method", "placebo-method"]}, "skills are"),
    ({"skills": ["decomposition-method", "anthropic-skills:pdf"]}, "outside the plugin"),
    ({"skills": []}, "skills are"),
    ({"tools": ["Task", "Read", "ToolSearch", "WebFetch"]}, "built-in tools"),
    ({"tools": ["Task", "Read", "ToolSearch", "mcp__claude_ai_Docs__read"]}, "outside the stemma"),
    ({"cwd": "/elsewhere"}, "working folder"),
    ({"claude_code_version": "2.2.0"}, "harness version"),
    ({"session_id": "other"}, "session id"),
    ({"subtype": "hook"}, "not the system init"),
])
def test_canary_fails(change: dict[str, Any], needle: str) -> None:
    g = good_init()
    g.update(change)
    problems = canary.check(g, EXP)
    assert problems and any(needle in p for p in problems), problems
