"""The one clean launch of the pinned harness, for the driver and every other runner.

`clean_launch` is the only way to build a session's argv, environment and settings, so no
runner (the driver or any other) can be clean in name only. It always:
- builds the environment from nothing: HOME, USER, PATH, LANG and CLAUDE_CONFIG_DIR, plus
  only `env_extra`, which may not set CLAUDE_CODE_EFFORT_LEVEL, a CLAUDE_* key the probes
  did not prove, an ANTHROPIC_* key (they redirect the harness's login, endpoint or
  model), or any base key;
- applies the canary settings (nothing synced from claude.ai, no connectors, no bundled
  skills, no built-in plugins, approved models only), which `settings_extra` may add to
  but never change;
- passes `-p <prompt>`, stream-json, `--model`, `--effort`, `--settings`, `--tools`;
- never passes `--bare` (it skips plugin hooks). `--strict-mcp-config` drops the plugin's
  own MCP server (probe p13) and `--disable-slash-commands` disables plugin skills, so
  both are refused when a plugin is loaded; a bare baseline may use them.

`check_canary` compares a session's stream-json init message with an `Expectation`;
`stemma_expectation` and `bare_expectation` build the two kinds a runner needs.
"""

from __future__ import annotations

import json
import os
import pwd
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.driver import canary
from stemma.driver.canary import Expectation
from stemma.driver.config import AVAILABLE_MODELS, EFFORTS, SCRUBBED_LANG, SCRUBBED_PATH
from stemma.driver.errors import HarnessError

__all__ = ["CANARY_SETTINGS", "CleanLaunch", "Expectation", "bare_expectation",
           "base_settings", "check_canary", "clean_launch", "merge_settings", "scrubbed_env",
           "stemma_expectation"]

# The probe-proven isolation settings (probes p1, p5, p6). Invariant.
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
BASE_ENV_KEYS: Final = frozenset({"HOME", "USER", "PATH", "LANG", "CLAUDE_CONFIG_DIR"})
# CLAUDE_* keys a run may set, each shown by a probe (p5: removes built-in agent types;
# p18, p19: CLAUDE_CODE_TMPDIR moves the harness's temp files into the session's folder).
PROVEN_CLAUDE_ENV: Final = frozenset({"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
                                      "CLAUDE_CODE_TMPDIR"})
FORBIDDEN_ENV: Final = frozenset({"CLAUDE_CODE_EFFORT_LEVEL"})  # overrides --effort
FORBIDDEN_FLAGS: Final = frozenset({"--bare"})


@dataclass(frozen=True)
class CleanLaunch:
    argv: list[str]
    env: dict[str, str]
    settings: dict[str, Any]
    settings_path: Path


def scrubbed_env(config_dir: Path, env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """A session's whole environment, built from nothing (no inherited variable)."""
    pw = pwd.getpwuid(os.getuid())
    env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "PATH": SCRUBBED_PATH, "LANG": SCRUBBED_LANG,
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    for k, v in (env_extra or {}).items():
        if k in BASE_ENV_KEYS:
            raise HarnessError(f"env_extra may not change {k}")
        if k in FORBIDDEN_ENV:
            raise HarnessError(f"{k} is never set in a run's environment")
        if k.startswith("CLAUDE") and k not in PROVEN_CLAUDE_ENV:
            raise HarnessError(f"{k} is not a CLAUDE_* variable the probes proved")
        if k.startswith("ANTHROPIC"):
            raise HarnessError(f"{k} would redirect the harness's login, endpoint or model")
        if not isinstance(v, str):
            raise HarnessError(f"env_extra[{k!r}] must be a string")
        env[k] = v
    return env


def base_settings() -> dict[str, Any]:
    out = json.loads(json.dumps(CANARY_SETTINGS))
    out["availableModels"] = list(AVAILABLE_MODELS)
    assert isinstance(out, dict)  # invariant
    return out


def merge_settings(extra: dict[str, Any] | None) -> dict[str, Any]:
    """The base settings plus `extra`, which may add keys but never change a base one."""
    out = base_settings()
    for k, v in (extra or {}).items():
        if k in out and out[k] != v:
            raise HarnessError(f"settings_extra may not change the canary setting {k!r}")
        out[k] = v
    return out


# Memory files the harness loads from a folder (probed on 2.1.308: it reads them in every
# folder above the working folder, the home folder included, whatever CLAUDE_CONFIG_DIR
# says). Invariant: the harness's own names.
MEMORY_NAMES: Final = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md",
                       ".claude/CLAUDE.local.md")
MEMORY_GLOBS: Final = (".claude/rules/**",)


def _both_forms(p: Path) -> list[Path]:
    """The path as given and fully resolved (macOS reaches /var through /private/var)."""
    out = [Path(os.path.abspath(p))]
    real = Path(os.path.realpath(p))
    if real not in out:
        out.append(real)
    return out


def memory_excludes(cwd: Path, config_dir: Path) -> list[str]:
    """`claudeMdExcludes` for a clean session: every memory file the harness could load
    from a folder above the working folder, from the home folder and from the config
    folder. Only the working folder's own CLAUDE.md (a run's standing rules) stays."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    out: list[str] = []
    for base in _both_forms(cwd):
        for folder in base.parents:
            out += [str(folder / n) for n in MEMORY_NAMES]
            out += [str(folder / g) for g in MEMORY_GLOBS]
        out += [str(base / n) for n in MEMORY_NAMES if n != "CLAUDE.md"]
        out += [str(base / g) for g in MEMORY_GLOBS]
    # The user's own memory: in the home folder, its .claude/, and the config folder.
    for folder in (*_both_forms(home), *_both_forms(home / ".claude"), *_both_forms(config_dir)):
        out += [str(folder / n) for n in MEMORY_NAMES]
        out += [str(folder / "rules/**")] + [str(folder / g) for g in MEMORY_GLOBS]
    return list(dict.fromkeys(out))


def memory_leaks(cwd: Path, config_dir: Path, excludes: list[str]) -> list[str]:
    """Memory files that exist where the harness would load them and that `excludes`
    does not name: each would load into a session meant to be clean."""
    names = set(excludes)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    found: list[str] = []
    for base in _both_forms(cwd):
        folders = [*base.parents, *_both_forms(home), *_both_forms(home / ".claude"),
                   *_both_forms(config_dir)]
        for folder in folders:
            for n in MEMORY_NAMES:
                f = folder / n
                if os.path.lexists(f) and str(f) not in names:
                    found.append(str(f))
    return sorted(set(found))


def clean_launch(binary: Path, config_dir: Path, prompt: str, *, cwd: Path, model: str,
                 effort: str, tools: list[str] | tuple[str, ...], settings_path: Path,
                 plugin_dir: Path | None = None, agent: str | None = None,
                 settings_extra: dict[str, Any] | None = None,
                 env_extra: dict[str, str] | None = None, session_id: str | None = None,
                 resume: bool = False, strict_mcp_config: bool = False,
                 disable_slash_commands: bool = False,
                 settings_writer: Callable[[str], None] | None = None) -> CleanLaunch:
    """Build a clean launch and write its settings file; the caller starts `argv` with
    `env` in `cwd`, stdin closed."""
    if not prompt or prompt.startswith("-"):
        raise HarnessError(f"prompt must not be empty or start with '-': {prompt!r}")
    if model not in AVAILABLE_MODELS:
        raise HarnessError(f"model must be one of {list(AVAILABLE_MODELS)}, got {model!r}")
    if effort not in EFFORTS:
        raise HarnessError(f"effort must be one of {list(EFFORTS)}, got {effort!r}")
    if plugin_dir is not None and (strict_mcp_config or disable_slash_commands):
        raise HarnessError("--strict-mcp-config and --disable-slash-commands drop the "
                           "plugin's MCP server and skills; not allowed with a plugin")
    if agent is not None and plugin_dir is None:
        raise HarnessError("an agent needs the plugin that defines it")
    if resume and not session_id:
        raise HarnessError("resume needs the session id to resume")
    for t in tools:
        if not t or "," in t or t.startswith("mcp__"):
            raise HarnessError(f"--tools takes built-in tool names, got {t!r}")
    settings = merge_settings(settings_extra)
    # No memory file from above the working folder, the home folder or the config folder
    # loads (the tree check found the owner's preferences in every clean session).
    excludes = list(dict.fromkeys([*memory_excludes(cwd, config_dir),
                                   *settings.get("claudeMdExcludes", [])]))
    settings["claudeMdExcludes"] = excludes
    leaks = memory_leaks(cwd, config_dir, excludes)
    if leaks:
        raise HarnessError(f"memory files outside the run would load: {leaks}")
    argv = [str(binary), "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if agent is not None:
        argv += ["--agent", agent]
    argv += ["--model", model, "--effort", effort]
    if plugin_dir is not None:
        argv += ["--plugin-dir", str(plugin_dir)]
    argv += ["--settings", str(settings_path), "--tools", ",".join(tools)]
    if strict_mcp_config:
        argv.append("--strict-mcp-config")
    if disable_slash_commands:
        argv.append("--disable-slash-commands")
    if session_id is not None:
        # Equals form: the value can never be read as a separate flag.
        argv.append(f"--resume={session_id}" if resume else f"--session-id={session_id}")
    assert not FORBIDDEN_FLAGS & set(argv)  # invariant
    env = scrubbed_env(config_dir, env_extra)
    text = json.dumps(settings, indent=1, sort_keys=True)
    if settings_writer is not None:  # e.g. a write that follows no symlink
        settings_writer(text)
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(text, encoding="utf-8")
    return CleanLaunch(argv, env, settings, settings_path)


def check_canary(init: dict[str, Any], expected: Expectation) -> list[str]:
    """Every difference between a session's init message and what it must show; [] passes."""
    return canary.check(init, expected)


def stemma_expectation(*, cwd: Path, plugin_dir: Path, agents: list[str] | frozenset[str],
                       skills: list[str] | frozenset[str], main_tools: list[str] | tuple[str, ...],
                       harness_version: str, session_id: str | None) -> Expectation:
    """A Stemma session: exactly the stemma plugin, its agents, its MCP server, the skills
    the switches selected, and the agent's own built-in tools (under --agent the init lists
    the agent's frontmatter tools within --tools)."""
    return Expectation(cwd=cwd, plugin_dir=plugin_dir,
                       agents=frozenset(f"{canary.PLUGIN}:{a}" for a in agents),
                       skills=frozenset(skills), builtin_tools=frozenset(main_tools),
                       harness_version=harness_version.split()[0], session_id=session_id,
                       mcp_servers=(canary.MCP_SERVER,))


def bare_expectation(*, cwd: Path, tools: list[str] | tuple[str, ...], harness_version: str,
                     session_id: str | None,
                     builtin_agents: frozenset[str] = frozenset()) -> Expectation:
    """A bare baseline (no plugin, no agent): no plugins, no MCP servers or MCP tools, no
    skills, exactly the --tools list, and the agents in `builtin_agents` (none when the
    session's env sets CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1)."""
    return Expectation(cwd=cwd, plugin_dir=None, agents=builtin_agents, skills=frozenset(),
                       builtin_tools=frozenset(tools), harness_version=harness_version.split()[0],
                       session_id=session_id, mcp_servers=())
