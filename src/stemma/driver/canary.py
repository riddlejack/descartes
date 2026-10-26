"""The canary: the stream-json init message must show exactly what was meant to load.

Exact facts only: plugin list, agent list, MCP servers and tools, skills, built-in tools,
working folder, harness version, session id. Any difference stops that session
(CONTRACTS section 2.1). Runners use it through `stemma.driver.clean.check_canary`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.register import roles

PLUGIN: Final = roles.PLUGIN
MCP_SERVER: Final = "plugin:stemma:stemma"  # invariant: plugin "stemma", server "stemma"
# The init message lists the Agent tool under its older name (probe p1, p6).
INIT_TOOL_ALIASES: Final = {"Task": "Agent"}


@dataclass(frozen=True)
class Expectation:
    cwd: Path
    plugin_dir: Path | None  # the stemma plugin; None for a bare session (no plugin)
    agents: frozenset[str]  # full names as init lists them, e.g. "stemma:lead"
    skills: frozenset[str]  # names; a plugin skill may be listed as "stemma:<name>"
    builtin_tools: frozenset[str]
    harness_version: str  # the bare version, e.g. "2.1.308"
    session_id: str | None  # None when resuming (the harness may report its own)
    mcp_servers: tuple[str, ...] = (MCP_SERVER,)


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
    return s[len(PLUGIN) + 1:] if s.startswith(PLUGIN + ":") else s


def check(init: dict[str, Any], exp: Expectation) -> list[str]:
    """Return every difference between the init message and the expectation; [] passes."""
    problems: list[str] = []
    if init.get("type") != "system" or init.get("subtype") != "init":
        return ["first stream message is not the system init message"]

    plugins = init.get("plugins")
    want_plugins = [PLUGIN] if exp.plugin_dir is not None else []
    if not isinstance(plugins, list) or _names(plugins) != want_plugins:
        problems.append(f"plugins are {_names(plugins)}, expected exactly {want_plugins}")
    elif exp.plugin_dir is not None and plugins[0].get("path") != str(exp.plugin_dir):
        problems.append(f"plugin path is {plugins[0].get('path')!r}, expected the run plugin "
                        f"{str(exp.plugin_dir)!r}")

    agents = _names(init.get("agents"))
    if agents is None:
        problems.append("init has no agent list")
    else:
        if exp.plugin_dir is not None:
            foreign = sorted(a for a in agents if not a.startswith(PLUGIN + ":"))
            if foreign:
                problems.append(f"agents outside the plugin loaded: {foreign}")
        if sorted(agents) != sorted(exp.agents):
            problems.append(f"agents are {sorted(agents)}, expected {sorted(exp.agents)}")

    servers = init.get("mcp_servers")
    names = _names(servers)
    if names is None or sorted(names) != sorted(exp.mcp_servers):
        problems.append(f"MCP servers are {names}, expected exactly {list(exp.mcp_servers)}")
    else:
        for srv in servers if isinstance(servers, list) else []:
            if isinstance(srv, dict) and srv.get("status") != "connected":
                problems.append(f"MCP server {srv.get('name')} status is {srv.get('status')!r}")

    skills = _names(init.get("skills"))
    if skills is None:
        problems.append("init has no skill list")
    else:
        if any(":" in s and not s.startswith(PLUGIN + ":") for s in skills):
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
        if exp.mcp_servers:
            foreign_mcp = [t for t in mcp if not t.startswith(roles.MCP_PREFIX)]
            if foreign_mcp:
                problems.append(f"MCP tools outside the stemma server: {foreign_mcp}")
        elif mcp:
            problems.append(f"MCP tools loaded in a session with no MCP server: {mcp}")

    if init.get("cwd") != str(exp.cwd):
        problems.append(f"working folder is {init.get('cwd')!r}, expected {str(exp.cwd)!r}")
    if init.get("claude_code_version") != exp.harness_version:
        problems.append(f"harness version is {init.get('claude_code_version')!r}, "
                        f"expected {exp.harness_version!r}")
    if exp.session_id is not None and init.get("session_id") != exp.session_id:
        problems.append(f"session id is {init.get('session_id')!r}, expected {exp.session_id!r}")
    return problems
