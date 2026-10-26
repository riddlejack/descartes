"""The canary: the stream-json init message must show exactly the run plugin and nothing else.

Exact facts only: plugin list, agent list, MCP servers, skills, tools, working folder,
harness version, session id. Any difference stops that session (CONTRACTS section 2.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.register import roles

MCP_SERVER: Final = "plugin:stemma:stemma"  # invariant: plugin "stemma", server "stemma"
# The init message lists the Agent tool under its older name (probe p1, p6).
INIT_TOOL_ALIASES: Final = {"Task": "Agent"}


@dataclass(frozen=True)
class Expectation:
    cwd: Path
    plugin_dir: Path
    agents: frozenset[str]  # short names
    skills: frozenset[str]
    builtin_tools: frozenset[str]
    harness_version: str  # the bare version, e.g. "2.1.308"
    session_id: str | None  # None when resuming (the harness may report its own)


def _names(xs: Any) -> list[str] | None:
    if not isinstance(xs, list):
        return None
    out = []
    for x in xs:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and isinstance(x.get("name"), str):
            out.append(x["name"])
        else:
            return None
    return out


def _skill_name(s: str) -> str:
    return s[len(roles.PLUGIN) + 1:] if s.startswith(roles.PLUGIN + ":") else s


def check(init: dict[str, Any], exp: Expectation) -> list[str]:
    """Return every difference between the init message and the expectation; [] passes."""
    problems: list[str] = []
    if init.get("type") != "system" or init.get("subtype") != "init":
        return ["first stream message is not the system init message"]

    plugins = init.get("plugins")
    if not isinstance(plugins, list) or [p.get("name") for p in plugins
                                         if isinstance(p, dict)] != [roles.PLUGIN] \
            or len(plugins) != 1:
        problems.append(f"plugins are {_names(plugins)}, expected exactly ['stemma']")
    elif plugins[0].get("path") != str(exp.plugin_dir):
        problems.append(f"plugin path is {plugins[0].get('path')!r}, expected the run plugin "
                        f"{str(exp.plugin_dir)!r}")

    agents = _names(init.get("agents"))
    want_agents = sorted(f"{roles.PLUGIN}:{a}" for a in exp.agents)
    if agents is None:
        problems.append("init has no agent list")
    else:
        foreign = sorted(a for a in agents if not a.startswith(roles.PLUGIN + ":"))
        if foreign:
            problems.append(f"agents outside the plugin loaded: {foreign}")
        if sorted(agents) != want_agents:
            problems.append(f"agents are {sorted(agents)}, expected {want_agents}")

    servers = init.get("mcp_servers")
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict) \
            or servers[0].get("name") != MCP_SERVER:
        problems.append(f"MCP servers are {_names(servers)}, expected exactly [{MCP_SERVER!r}]")
    elif servers[0].get("status") != "connected":
        problems.append(f"MCP server {MCP_SERVER} status is {servers[0].get('status')!r}")

    skills = _names(init.get("skills"))
    if skills is None:
        problems.append("init has no skill list")
    else:
        if any(":" in s and not s.startswith(roles.PLUGIN + ":") for s in skills):
            problems.append(f"skills outside the plugin loaded: {sorted(skills)}")
        got = sorted(_skill_name(s) for s in skills)
        if got != sorted(exp.skills):
            problems.append(f"skills are {sorted(skills)}, expected {sorted(exp.skills)}")

    tools = _names(init.get("tools"))
    if tools is None:
        problems.append("init has no tool list")
    else:
        builtin = sorted(INIT_TOOL_ALIASES.get(t, t) for t in tools if not t.startswith("mcp__"))
        mcp = sorted(t for t in tools if t.startswith("mcp__"))
        if builtin != sorted(exp.builtin_tools):
            problems.append(f"built-in tools are {builtin}, expected {sorted(exp.builtin_tools)}")
        foreign_mcp = [t for t in mcp if not t.startswith(roles.MCP_PREFIX)]
        if foreign_mcp:
            problems.append(f"MCP tools outside the stemma server: {foreign_mcp}")

    if init.get("cwd") != str(exp.cwd):
        problems.append(f"working folder is {init.get('cwd')!r}, expected {str(exp.cwd)!r}")
    if init.get("claude_code_version") != exp.harness_version:
        problems.append(f"harness version is {init.get('claude_code_version')!r}, "
                        f"expected {exp.harness_version!r}")
    if exp.session_id is not None and init.get("session_id") != exp.session_id:
        problems.append(f"session id is {init.get('session_id')!r}, expected {exp.session_id!r}")
    return problems
