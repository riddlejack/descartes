"""Per-session settings (`--settings`), built-in tool allowlist (`--tools`) and read denials.

The base is the canary settings of the harness probes (p5, p6): nothing synced from
claude.ai, no connectors, no bundled skills, no built-in plugins. On top: approved models
only, WebFetch, forks and every built-in agent type denied, tool-only paths unwritable
by file tools and by sandboxed Bash, and each blind role's read denials (CONTRACTS
section 6). Absolute paths in permission rules take a leading extra slash
(`Read(//abs/path/**)`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.driver.config import AVAILABLE_MODELS, SESSIONS_DIR, TOOL_ONLY
from stemma.driver.errors import AgentFileInvalid
from stemma.register import roles

MCP_PREFIX: Final = roles.MCP_PREFIX
# MCP tools load deferred, so every session needs ToolSearch (probe p1).
ALWAYS_TOOLS: Final = ("ToolSearch",)
# Built-in agent types that must never start (invariant: section 6).
DENIED_AGENT_TYPES: Final = (
    "fork", "general-purpose", "Explore", "Plan", "claude", "statusline-setup",
    "claude-code-guide",
)
CANARY_SETTINGS: Final[dict[str, Any]] = {
    "syncClaudeAiPlugins": False,
    "syncClaudeAiSkills": False,
    "disableClaudeAiConnectors": True,
    "disableBundledSkills": True,
    # Two bundled skills survive disableBundledSkills (probe p5).
    "skillOverrides": {"design": "off", "doctor": "off"},
    "enabledPlugins": {
        "cc-plugin-agents-md@builtin": False,
        "cc-plugin-telemetry@builtin": False,
        "cc-plugin-plugin-authoring@builtin": False,
    },
}

_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.*\))?$")

# What the blind roles may not read (section 6), as run-relative patterns. A trailing "/"
# is a folder, "**" any depth. Beyond the contract's list: `history/` holds earlier frames,
# `tree.md` renders `tree.json`, and the final deliverable is a draft in final form.
CLAIM_CHECKER_DENY: Final = (
    "frame.md", "history/", "framer/", "draft/", "deliverable/", "reviews/",
    "branches/**/record.md", "branches/**/order.md", "branches/**/restatement.md",
)
FRAMER_OWN_DENY: Final = (
    "frame.md", "history/", "tree.json", "tree.md", "branches/", "draft/", "deliverable/",
    "reviews/", "analysis/", "register/",
)
# The decider's-eye reviewer reads only task/, intake.md and the draft under review: every
# other name the run folder holds or will hold (section 3) is denied.
DECIDERS_EYE_KNOWN: Final = (
    "frame.md", "history/", "tree.json", "tree.md", "framer/", "branches/", "store/",
    "register/", "analysis/", "deliverable/", "events.jsonl", "status.json", "status.html",
    "retro.md", "run.json",
)
DECIDERS_EYE_KEEP: Final = frozenset({"task", "intake.md", "CLAUDE.md", "draft", "reviews",
                                      ".stemma", ".claude"})


@dataclass(frozen=True)
class ToolSets:
    builtin: tuple[str, ...]  # for --tools, sorted
    allow: tuple[str, ...]  # for permissions.allow, sorted


def split_tools(tool_lists: list[list[str]]) -> ToolSets:
    """Built-in names feed --tools; `mcp__` names and full rules feed permissions.allow."""
    builtin: set[str] = set(ALWAYS_TOOLS)
    allow: set[str] = set(ALWAYS_TOOLS)
    for tools in tool_lists:
        for t in tools:
            if not _TOOL.match(t):
                raise AgentFileInvalid(f"not a tool name or rule: {t!r}")
            if t.startswith("mcp__"):
                if not t.startswith(MCP_PREFIX):
                    raise AgentFileInvalid(f"MCP tool outside the stemma server: {t!r}")
                allow.add(t)
                continue
            builtin.add(t.split("(", 1)[0])
            allow.add(t)
    return ToolSets(tuple(sorted(builtin)), tuple(sorted(allow)))


def tools_for_role(role: str, agent_tools: dict[str, list[str]]) -> ToolSets:
    """A session's tools. The lead's session also covers the subagents it may start, since
    their calls run under the lead session's permissions and built-in tool set."""
    if role not in agent_tools:
        raise AgentFileInvalid(f"run plugin has no agent file for role {role!r}")
    lists = [agent_tools[role]]
    if role == roles.short(roles.LEAD):
        lists += [agent_tools[roles.short(s)] for s in sorted(roles.SUBAGENT_TYPES)
                  if roles.short(s) in agent_tools]
    return split_tools(lists)


def _abs(run_dir: Path, pattern: str) -> str:
    return str(run_dir / pattern.rstrip("/"))


def _rule(tool: str, run_dir: Path, pattern: str) -> str:
    p = _abs(run_dir, pattern)
    suffix = "/**" if pattern.endswith("/") else ""
    return f"{tool}(/{p}{suffix})"


def _rule_abs(tool: str, path: Path, folder: bool) -> str:
    return f"{tool}(/{path}{'/**' if folder else ''})"


def read_denials(run_dir: Path, role: str, prompt: str) -> list[str]:
    """Run-relative patterns this session may not read."""
    deny: list[str] = [SESSIONS_DIR + "/"]  # every session's raw stream: nobody reads it
    if role == "claim-checker":
        deny += CLAIM_CHECKER_DENY
    elif role == "blind-framer" and prompt == "frame:own":
        deny += FRAMER_OWN_DENY
    elif role == "deciders-eye-reviewer":
        deny += _deciders_eye_denials(run_dir, prompt)
    return list(dict.fromkeys(deny))


def _deciders_eye_denials(run_dir: Path, prompt: str) -> list[str]:
    m = re.fullmatch(r"draft:(\d+)", prompt)
    if not m:
        raise AgentFileInvalid(f"decider's-eye prompt must be draft:<version>, got {prompt!r}")
    v = m.group(1)
    out: list[str] = list(DECIDERS_EYE_KNOWN)
    if run_dir.is_dir():
        for p in sorted(run_dir.iterdir()):
            if p.name not in DECIDERS_EYE_KEEP:
                out.append(p.name + ("/" if p.is_dir() else ""))
    draft = run_dir / "draft"
    if draft.is_dir():
        for p in sorted(draft.iterdir()):
            if p.name != f"answer.v{v}.md":
                out.append(f"draft/{p.name}" + ("/" if p.is_dir() else ""))
    reviews = run_dir / "reviews"
    if reviews.is_dir():
        for p in sorted(reviews.iterdir()):
            if p.name != f"v{v}":
                out.append(f"reviews/{p.name}" + ("/" if p.is_dir() else ""))
    out += [f"reviews/v{v}/evidence-audit.md", f"reviews/v{v}/mechanical.md"]
    return out


def build_settings(run_dir: Path, role: str, prompt: str, tools: ToolSets,
                   claude_config: Path) -> dict[str, Any]:
    """The settings JSON for one session. `run_dir` must be the resolved real path."""
    deny: list[str] = ["WebFetch"] + [f"Agent({a})" for a in DENIED_AGENT_TYPES]
    deny_write_abs: list[str] = []
    for pat in TOOL_ONLY:
        deny += [_rule("Write", run_dir, pat), _rule("Edit", run_dir, pat)]
        deny_write_abs.append(_abs(run_dir, pat))
    reads = read_denials(run_dir, role, prompt)
    for pat in reads:
        deny.append(_rule("Read", run_dir, pat))
    # Session transcripts of every session live in the shared config folder; no agent
    # reads them (a blind role would see its author's whole conversation).
    transcripts = claude_config / "projects"
    deny.append(_rule_abs("Read", transcripts, folder=True))
    deny_read_abs = [_abs(run_dir, p) for p in reads] + [str(transcripts)]
    settings: dict[str, Any] = dict(CANARY_SETTINGS)
    settings.update({
        "availableModels": list(AVAILABLE_MODELS),
        "permissions": {"deny": deny, "allow": list(tools.allow)},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "network": {"allowedDomains": []},
            "filesystem": {"denyWrite": deny_write_abs, "denyRead": deny_read_abs},
        },
    })
    return settings
