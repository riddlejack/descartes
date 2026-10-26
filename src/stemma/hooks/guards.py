"""The guards of CONTRACTS section 6, as pure decisions over a hook's input.

Each function takes the hook input the harness sends (its `agent_type` is the caller's
identity, never a tool argument) and returns a `Decision`. The entry point
(`stemma.hooks.__main__`) turns a decision into the harness protocol: a refusal is
exit code 2 with the refusal on stderr, which blocks the tool however the rest of the
output looks; a rewrite is `updatedInput` JSON on stdout; anything that goes wrong in
a guard is a refusal too, and the hook entry carries `"onFailure": "block"`.

Refusals name what failed and nothing else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from stemma.hooks import paths
from stemma.register import roles


class GuardInputError(Exception):
    """The hook input lacks something every guard needs."""


@dataclass(frozen=True)
class Decision:
    allow: bool
    refusal: str = ""
    updated_input: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


ALLOW = Decision(allow=True)

# Tools that write a file named by `file_path` (or `notebook_path`).
WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
READ_TOOLS = frozenset({"Read"})
AGENT_TOOLS = frozenset({"Agent", "Task"})

# Arguments of an Agent call that would override the role's recorded model or effort.
RUN_SET_AGENT_ARGS = ("model", "effort")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_dir_from_env() -> Path:
    value = os.environ.get("STEMMA_RUN_DIR", "")
    if not value:
        raise GuardInputError("STEMMA_RUN_DIR is not set")
    return Path(value)


def _who(hook: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_type": hook.get("agent_type"),
        "agent_id": hook.get("agent_id"),
        "session_id": hook.get("session_id"),
    }


def _require(hook: dict[str, Any], key: str) -> Any:
    if key not in hook or hook[key] in (None, ""):
        raise GuardInputError(f"hook input has no {key}")
    return hook[key]


def refuse(hook: dict[str, Any], message: str) -> Decision:
    event = {"event": "denied", "at": now(), "tool": hook.get("tool_name"),
             "refusal": message, **_who(hook)}
    return Decision(allow=False, refusal=message, events=[event])


def _target(hook: dict[str, Any]) -> str:
    ti = hook.get("tool_input") or {}
    value = ti.get("file_path") or ti.get("notebook_path") or ""
    if not isinstance(value, str) or not value:
        raise GuardInputError("tool input has no file path")
    return value


# --- Agent calls -------------------------------------------------------------------


def guard_agent(hook: dict[str, Any]) -> Decision:
    """Only plugin workers start as subagents; model and effort are the run's."""
    ti = dict(hook.get("tool_input") or {})
    sub = ti.get("subagent_type")
    if not isinstance(sub, str) or sub not in roles.SUBAGENT_TYPES:
        shown = sub if isinstance(sub, str) else "(none)"
        return refuse(hook, f"Refused: subagent type {shown} is not one this session may start.")
    stripped = [k for k in RUN_SET_AGENT_ARGS if k in ti]
    if not stripped:
        return ALLOW
    for k in stripped:
        del ti[k]
    event = {"event": "agent_args_stripped", "at": now(), "stripped": stripped, **_who(hook)}
    return Decision(allow=True, updated_input=ti, events=[event])


# --- Writes ------------------------------------------------------------------------


def guard_write(hook: dict[str, Any], run_dir: Path) -> Decision:
    rel = paths.run_relative(_target(hook), str(hook.get("cwd") or run_dir), run_dir)
    if rel is not None and paths.is_tool_only(rel):
        return refuse(hook, f"Refused: {rel} is written only by Stemma's tools.")
    return ALLOW


def write_event(hook: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    target = _target(hook)
    rel = paths.run_relative(target, str(hook.get("cwd") or run_dir), run_dir)
    return {"event": "file_write", "at": now(), "tool": hook.get("tool_name"),
            "path": str(rel) if rel is not None else str(paths.resolve(target, str(run_dir))),
            **_who(hook)}


# --- Blind roles' reads ------------------------------------------------------------

_CHECKER_HIDDEN_BRANCH_FILES = frozenset({"record.md", "order.md", "restatement.md"})
_CHECKER_HIDDEN_TOP = frozenset({"frame.md", "history", "draft", "deliverable", "reviews"})
_FRAMER_HIDDEN_TOP = frozenset(
    {"frame.md", "history", "tree.json", "tree.md", "branches", "draft", "deliverable", "reviews"}
)


def _deciders_eye_allowed(rel: PurePosixPath, draft_version: str | None) -> bool:
    if paths.under(rel, "task") or rel == PurePosixPath("intake.md"):
        return True
    return draft_version is not None and rel == PurePosixPath(f"draft/answer.v{draft_version}.md")


def blind_read_refusal(role: str, rel: PurePosixPath, run_dir: Path,
                       draft_version: str | None) -> str | None:
    """Why `role` may not read `rel`, or None. Plugin and store reads stay open."""
    if role == roles.CLAIM_CHECKER:
        if rel.parts and rel.parts[0] in _CHECKER_HIDDEN_TOP:
            return f"Refused: the claim checker does not read {rel}."
        if paths.is_branch_file(rel, _CHECKER_HIDDEN_BRANCH_FILES):
            return f"Refused: the claim checker does not read {rel}."
    elif role == roles.DECIDERS_EYE:
        if not _deciders_eye_allowed(rel, draft_version):
            return f"Refused: the decider's-eye reviewer does not read {rel}."
    elif role == roles.BLIND_FRAMER:
        own_frame = run_dir / "framer" / "own-frame.md"
        if not own_frame.exists() and rel.parts and rel.parts[0] in _FRAMER_HIDDEN_TOP:
            return f"Refused: the blind framer does not read {rel} before its own frame exists."
    return None


def guard_read(hook: dict[str, Any], run_dir: Path, draft_version: str | None) -> Decision:
    role = _require(hook, "agent_type")
    rel = paths.run_relative(_target(hook), str(hook.get("cwd") or run_dir), run_dir)
    if rel is None:
        if role == roles.DECIDERS_EYE:
            return refuse(hook, "Refused: the decider's-eye reviewer reads only its run's files.")
        return ALLOW
    why = blind_read_refusal(role, rel, run_dir, draft_version)
    return refuse(hook, why) if why else ALLOW


# --- Identity stamps for the MCP server --------------------------------------------


def stamp_path(run_dir: Path, tool_use_id: str) -> Path:
    if not tool_use_id or "/" in tool_use_id or tool_use_id.startswith("."):
        raise GuardInputError("tool_use_id is not a plain name")
    return run_dir / "register" / "calls" / f"{tool_use_id}.json"


def write_stamp(hook: dict[str, Any], run_dir: Path) -> None:
    """Record who is calling, keyed by the tool use id the MCP request also carries."""
    tool_use_id = _require(hook, "tool_use_id")
    stamp = {
        "agent_type": _require(hook, "agent_type"),
        "agent_id": hook.get("agent_id"),
        "session_id": _require(hook, "session_id"),
        "tool": _require(hook, "tool_name"),
        "at": now(),
    }
    path = stamp_path(run_dir, tool_use_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(stamp, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def guard_mcp(hook: dict[str, Any], run_dir: Path) -> Decision:
    write_stamp(hook, run_dir)
    return ALLOW
