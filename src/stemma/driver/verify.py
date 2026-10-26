"""Resolved model and effort, read from the transcripts of sessions the driver launched.

The main thread's transcript is `<config>/projects/<cwd slug>/<session_id>.jsonl`; each
assistant line carries `effort` and `message.model`. Subagents are under
`<session_id>/subagents/agent-<id>.jsonl` with `agent-<id>.meta.json` (`agentType`,
`spawnDepth`). Only files named by a session id the driver itself launched are opened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

SYNTHETIC_MODEL: Final = "<synthetic>"  # harness-made messages that no model produced


def slug(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_dir(claude_config: Path, cwd: Path, init: dict[str, Any] | None) -> Path:
    """The transcript folder. The init message's auto-memory path names it exactly."""
    if init is not None:
        mp = init.get("memory_paths")
        auto = mp.get("auto") if isinstance(mp, dict) else None
        if isinstance(auto, str) and auto:
            p = Path(auto.rstrip("/"))
            if p.name == "memory" and p.parent.parent == claude_config / "projects":
                return p.parent
    return claude_config / "projects" / slug(cwd)


def find_transcript(claude_config: Path, cwd: Path, init: dict[str, Any] | None,
                    session_id: str) -> Path | None:
    p = project_dir(claude_config, cwd, init) / f"{session_id}.jsonl"
    if p.is_file():
        return p
    # Long paths are shortened by the harness; look for this one session id only.
    hits = sorted((claude_config / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if len(hits) == 1 else None


@dataclass
class Tail:
    """Incremental reader of one transcript file."""

    path: Path
    offset: int = 0
    pairs: set[tuple[str, str]] = field(default_factory=set)  # (model, effort) seen

    def read(self) -> set[tuple[str, str]]:
        """New (model, effort) pairs since the last read."""
        if not self.path.is_file():
            return set()
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return set()
        self.offset += end + 1
        new: set[tuple[str, str]] = set()
        for raw in data[: end + 1].splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            model = msg.get("model") if isinstance(msg, dict) else None
            if not isinstance(model, str) or model == SYNTHETIC_MODEL:
                continue
            effort = rec.get("effort")
            pair = (model, effort if isinstance(effort, str) else "<none>")
            if pair not in self.pairs:
                self.pairs.add(pair)
                new.add(pair)
        return new


@dataclass
class Subagent:
    agent_id: str
    agent_type: str | None
    spawn_depth: int | None
    tail: Tail


def subagents(transcript: Path, known: dict[str, Subagent]) -> list[Subagent]:
    """Subagents of the session that appeared since the last call (added to `known`)."""
    folder = transcript.with_suffix("") / "subagents"
    found: list[Subagent] = []
    if not folder.is_dir():
        return found
    for meta in sorted(folder.glob("agent-*.meta.json")):
        aid = meta.name[len("agent-"): -len(".meta.json")]
        if aid in known:
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # being written; seen next pass
        at = m.get("agentType") if isinstance(m, dict) else None
        depth = m.get("spawnDepth") if isinstance(m, dict) else None
        sub = Subagent(aid, at if isinstance(at, str) else None,
                       depth if isinstance(depth, int) else None,
                       Tail(folder / f"agent-{aid}.jsonl"))
        known[aid] = sub
        found.append(sub)
    return found
