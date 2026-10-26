"""Per-session settings (`--settings`), built-in tool allowlist (`--tools`) and read denials.

The base is the canary settings of the harness probes (p5, p6): nothing synced from
claude.ai, no connectors, no bundled skills, no built-in plugins. On top: approved models
only, WebFetch, forks and every built-in agent type denied, tool-only paths unwritable
by file tools and by sandboxed Bash, and each blind role's read denials (CONTRACTS
section 6). Absolute paths in permission rules take a leading extra slash
(`Read(//abs/path/**)`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.driver.clean import merge_settings
from stemma.driver.config import SESSIONS_DIR, TOOL_ONLY
from stemma.driver.errors import AgentFileInvalid
from stemma.register import roles

MCP_PREFIX: Final = roles.MCP_PREFIX
# Kept in --tools and allowed for every session: harmless now that the plugin's MCP server
# sets alwaysLoad (probe p11), and the canary expects only the agent's own tools.
ALWAYS_TOOLS: Final = ("ToolSearch",)
# Plugin roles no session may start as a subagent (invariant: section 6, v0.2.4).
HOST_STARTED_ROLES: Final = tuple(sorted(roles.CHECKING_ROLES | {roles.LEAD}))
# Built-in agent types that must never start (invariant: section 6).
DENIED_AGENT_TYPES: Final = (
    "fork", "general-purpose", "Explore", "Plan", "claude", "statusline-setup",
    "claude-code-guide",
)
_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.*\))?$")

# What the blind roles may not read (section 6), as run-relative patterns. A trailing "/"
# is a folder, "**" any depth. Beyond the contract's list: `history/` holds earlier frames,
# `tree.md` renders `tree.json`, and the final deliverable is a draft in final form.
CLAIM_CHECKER_DENY: Final = (
    "frame.md", "history/", "framer/", "draft/", "deliverable/", "reviews/", "register/",
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
    "analysis/", "deliverable/", "events.jsonl", "status.json", "status.html",
    "retro.md", "run.json", "draft/",
    # register/ holds the frozen draft the reviewer reads; everything else in it is denied.
    "register/log.jsonl", "register/log.jsonl.lock", "register/checks.jsonl",
    "register/checks.jsonl.lock", "register/index.sqlite", "register/register.lock",
    "register/calls/", "register/frames/", "register/tmp/",
)
DECIDERS_EYE_KEEP: Final = frozenset({"task", "intake.md", "CLAUDE.md", "register",
                                      ".stemma", ".claude"})


@dataclass(frozen=True)
class ToolSets:
    builtin: tuple[str, ...]  # for --tools, sorted
    allow: tuple[str, ...]  # for permissions.allow, sorted
    # The built-in tools the session's own agent has: with `--agent`, the init message
    # lists the agent's frontmatter tools within --tools, not all of --tools (real launch).
    main: tuple[str, ...] = ()


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
    own = agent_tools[role]
    lists = [own]
    if role == roles.short(roles.LEAD):
        lists += [agent_tools[roles.short(s)] for s in sorted(roles.SUBAGENT_TYPES)
                  if roles.short(s) in agent_tools]
    sets = split_tools(lists)
    main = tuple(sorted({t.split("(", 1)[0] for t in own if not t.startswith("mcp__")}))
    return ToolSets(sets.builtin, sets.allow, main)


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


def _draft_version(prompt: str) -> str:
    m = re.fullmatch(r"draft:(\d+)", prompt)
    if not m:
        raise AgentFileInvalid(f"decider's-eye prompt must be draft:<version>, got {prompt!r}")
    return m.group(1)


def _deciders_eye_reads(prompt: str) -> list[str]:
    """The only run-folder paths the decider's-eye reviewer reads (section 6)."""
    return ["task", "intake.md", f"register/drafts/answer.v{_draft_version(prompt)}.md"]


def _deciders_eye_denials(run_dir: Path, prompt: str) -> list[str]:
    v = _draft_version(prompt)
    out: list[str] = list(DECIDERS_EYE_KNOWN)
    if run_dir.is_dir():
        for p in sorted(run_dir.iterdir()):
            if p.name not in DECIDERS_EYE_KEEP:
                out.append(p.name + ("/" if p.is_dir() else ""))
    register = run_dir / "register"
    if register.is_dir():
        for p in sorted(register.iterdir()):
            if p.name != "drafts":
                out.append(f"register/{p.name}" + ("/" if p.is_dir() else ""))
    drafts = register / "drafts"
    if drafts.is_dir():
        for p in sorted(drafts.iterdir()):
            if p.name != f"answer.v{v}.md":
                out.append(f"register/drafts/{p.name}" + ("/" if p.is_dir() else ""))
    out.append("reviews/")  # reviews are tool-written; the reviewer records through a tool
    return list(dict.fromkeys(out))


def build_settings(run_dir: Path, role: str, prompt: str, tools: ToolSets,
                   claude_config: Path, *, cache_dir: Path | None = None,
                   deny_agent: bool = False, scope: ReadScope | None = None) -> dict[str, Any]:
    """The whole settings JSON of one session: the clean base plus Stemma's rules."""
    return merge_settings(stemma_settings_extra(run_dir, role, prompt, tools, claude_config,
                                                cache_dir=cache_dir, deny_agent=deny_agent,
                                                scope=scope))


@dataclass(frozen=True)
class ReadScope:
    """What a session's Bash may read outside its run (CONTRACTS section 6, v0.2.8).

    `deny` are folders no session reads (the home folder, the runs folder, the
    repository, the plugin source, the fetch cache); `allow` re-opens what the
    interpreter needs (its venv and base install, the stemma package source). The run
    folder itself is re-opened too. On 2.1.308 the most specific rule wins: an
    `allowRead` re-opens a path inside a `denyRead`, and a `denyRead` inside an
    `allowRead` still denies (probes p14, p16), so the blind roles' own denials hold."""

    deny: tuple[str, ...]
    allow: tuple[str, ...]


def read_scope(python: Path, runs_dir: Path, repo_dir: Path, plugin_src: Path,
               cache_dir: Path | None) -> ReadScope:
    home = Path(os.path.expanduser("~")).resolve()
    deny = [home, runs_dir, repo_dir, plugin_src] + ([cache_dir] if cache_dir else [])
    allow: list[Path] = []
    py = Path(os.path.abspath(python))
    venv = py.parent.parent
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        allow.append(venv)
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                allow.append(Path(value.strip()).resolve().parent)  # the base install
    else:
        allow.append(py.resolve().parent.parent)
    import stemma  # where the stemma package's source lives (editable or installed)
    allow.append(Path(stemma.__file__).resolve().parent.parent)
    def norm(ps: list[Path]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(Path(os.path.realpath(p))) for p in ps))
    return ReadScope(norm(deny), norm(allow))


def stemma_settings_extra(run_dir: Path, role: str, prompt: str, tools: ToolSets,
                          claude_config: Path, *, cache_dir: Path | None = None,
                          deny_agent: bool = False,
                          scope: ReadScope | None = None) -> dict[str, Any]:
    """Stemma's rules on top of the clean base: permissions and the sandbox.
    `run_dir` must be the resolved real path. `deny_agent` denies the Agent tool outright
    (the lead under s2 single_loop)."""
    deny: list[str] = ["WebFetch"] + [f"Agent({a})" for a in DENIED_AGENT_TYPES]
    # Only the host starts checking roles and the lead (section 6, v0.2.4).
    deny += [f"Agent({r})" for r in HOST_STARTED_ROLES]
    if deny_agent:
        deny.append("Agent")
    deny_write_abs: list[str] = []
    for pat in TOOL_ONLY:
        deny += [_rule("Write", run_dir, pat), _rule("Edit", run_dir, pat)]
        deny_write_abs.append(_abs(run_dir, pat))
    if cache_dir is not None:
        # A later run is served the cache's entries, so no agent may write them.
        deny += [_rule_abs("Write", cache_dir, folder=True),
                 _rule_abs("Edit", cache_dir, folder=True)]
        deny_write_abs.append(str(cache_dir))
    reads = read_denials(run_dir, role, prompt)
    for pat in reads:
        deny.append(_rule("Read", run_dir, pat))
    # Session transcripts of every session live in the shared config folder; no agent
    # reads them (a blind role would see its author's whole conversation).
    transcripts = claude_config / "projects"
    deny.append(_rule_abs("Read", transcripts, folder=True))
    deny_read_abs = [_abs(run_dir, p) for p in reads] + [str(transcripts)]
    filesystem: dict[str, Any] = {"denyWrite": deny_write_abs, "denyRead": deny_read_abs}
    allow_read: list[str] = []
    if role == "deciders-eye-reviewer":
        # Bash: the whole run folder is unreadable except the three things the reviewer
        # reads (allowRead takes precedence over denyRead: probe p12). The Read tool's
        # deny rules cannot be overridden by an allow, so they enumerate (above).
        filesystem["denyRead"] = [str(run_dir), str(transcripts)]
        allow_read = [_abs(run_dir, p) for p in _deciders_eye_reads(prompt)]
    elif scope is not None:
        allow_read = [str(run_dir)]  # re-opened inside the denied folders (probe p14)
    if scope is not None:
        # Every session reads only its own run (v0.2.8). The Read tool is confined by the
        # Read guard hook (deny by default outside the run folder); this confines Bash.
        filesystem["denyRead"] = list(dict.fromkeys(filesystem["denyRead"] + list(scope.deny)))
        allow_read += list(scope.allow)
    if allow_read:
        filesystem["allowRead"] = list(dict.fromkeys(allow_read))
    return {
        "permissions": {"deny": deny, "allow": list(tools.allow)},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "network": {"allowedDomains": []},
            "filesystem": filesystem,
        },
    }
