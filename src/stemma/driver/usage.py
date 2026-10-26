"""Token accounting for every session the driver launches and every subagent inside it.

Source: the harness's own records. Each assistant message in a session's transcript
(`<config>/projects/<slug>/<session_id>.jsonl`, subagents under `<session_id>/subagents/`)
carries `message.usage`: input, output, cache-creation and cache-read tokens. A message
written in several entries (one per content block) carries the same usage on each, so a
message counts once, by its id. Summed this way, a session's main thread reproduces the
`usage` of its stream-json `result` exactly (checked on the walking-skeleton run). The
transcript is the source because a resumed lead appends to one transcript, so each
launch counts only the messages written while it ran, and because a session stopped
before its result still has its transcript.

Cost: the harness's own estimate, `total_cost_usd` and `modelUsage[*].costUSD` from the
session's result, at the harness's `costBasis` (list prices). It is an estimate, labelled
so; a subagent's cost is inside its parent session's and is never apportioned here.
Phases are cut at milestone times in `events.jsonl` (see PHASES).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from stemma.driver import hostio
from stemma.driver.verify import SYNTHETIC_MODEL

USAGE_FILE: Final = "usage.jsonl"
USAGE_MD: Final = "usage.md"
TOKEN_KEYS: Final = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                     "cache_read_input_tokens")
# Phases of a run, each ending at the first time its milestone tool was recorded (a
# logical order of the run, not tuning): the lead asks for the frame critique once it has
# framed the question and built the tree; research runs until the first draft; drafting
# and review until the release.
PHASES: Final = (("framing", "request_frame_critique"), ("research", "submit_draft"),
                 ("drafting and review", "finish_run"), ("after release", None))
# Resource limit: seconds after a session's end within which a message still counts to
# it (the harness writes its last entries while the process exits). The window has no
# slack at its start: a launch's messages are written after it starts, on the same
# clock, so a resumed lead never counts the previous launch's messages again.
END_SLACK_S: Final = 5.0
COST_NOTE: Final = ("the harness's own estimate (result total_cost_usd, modelUsage costUSD) "
                    "at its costBasis; an estimate, not a bill")
_BRANCH_IN_BRIEF = re.compile(r"branches/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/")


@dataclass(frozen=True)
class Message:
    message_id: str
    model: str
    effort: str | None
    at: float  # POSIX seconds, from the entry's timestamp
    tokens: dict[str, int]


def _ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def read_messages(path: Path) -> list[Message]:
    """Every model-written assistant message of one transcript, each once, oldest first."""
    if not path.is_file():
        return []
    by_id: dict[str, Message] = {}
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            model, usage = msg.get("model"), msg.get("usage")
            if not isinstance(model, str) or model == SYNTHETIC_MODEL or not isinstance(
                    usage, dict):
                continue
            mid = msg.get("id") or rec.get("uuid")
            at = _ts(rec.get("timestamp"))
            if not isinstance(mid, str) or at is None:
                continue
            tokens = {k: int(usage.get(k) or 0) for k in TOKEN_KEYS}
            effort = rec.get("effort")
            # The last entry of a message carries its final usage.
            by_id[mid] = Message(mid, model, effort if isinstance(effort, str) else None,
                                 by_id[mid].at if mid in by_id else at, tokens)
    return sorted(by_id.values(), key=lambda m: m.at)


def in_window(messages: Iterable[Message], start: float, end: float) -> list[Message]:
    return [m for m in messages if start <= m.at <= end + END_SLACK_S]


def _zero() -> dict[str, int]:
    return dict.fromkeys(TOKEN_KEYS, 0)


def _add(into: dict[str, int], tokens: dict[str, int]) -> None:
    for k in TOKEN_KEYS:
        into[k] = into.get(k, 0) + int(tokens.get(k, 0))


class Milestones:
    """The first time each phase milestone was recorded, read from events.jsonl in
    increments (the log only grows; a line still being written is read next time)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.first: dict[str, float] = {}

    def update(self) -> dict[str, float]:
        if not self.path.is_file():
            return self.first
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return self.first
        self.offset += end + 1
        wanted = {m for _n, m in PHASES if m}
        for raw in data[: end + 1].splitlines():
            if b'"register_write"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            tool = e.get("tool") if isinstance(e, dict) else None
            if tool in wanted and tool not in self.first and e.get("event") == "register_write":
                t = _ts(e.get("at"))
                if t is not None:
                    self.first[tool] = t
        return self.first


def phase_starts(events_list: Iterable[dict[str, Any]] | dict[str, float]
                 ) -> list[tuple[str, float]]:
    """Each phase with its start time: the first, then one after each milestone seen.
    Takes the events, or the first time of each milestone (Milestones.update())."""
    if isinstance(events_list, dict):
        ends = dict(events_list)
    else:
        ends = {}
        for e in events_list:
            tool = e.get("tool")
            if e.get("event") == "register_write" and isinstance(tool, str) and tool not in ends:
                t = _ts(e.get("at"))
                if t is not None:
                    ends[tool] = t
    out: list[tuple[str, float]] = [(PHASES[0][0], float("-inf"))]
    for (_name, milestone), (nxt, _m) in pairwise(PHASES):
        if milestone is None or milestone not in ends:
            break
        out.append((nxt, ends[milestone]))
    return out


def phase_of(t: float, starts: list[tuple[str, float]]) -> str:
    name = starts[0][0]
    for n, s in starts:
        if t >= s:
            name = n
    return name


def tally(messages: list[Message], starts: list[tuple[str, float]]) -> dict[str, Any]:
    tokens = _zero()
    by_model: dict[str, dict[str, int]] = {}
    by_phase: dict[str, dict[str, int]] = {}
    efforts: set[str] = set()
    for m in messages:
        _add(tokens, m.tokens)
        _add(by_model.setdefault(m.model, _zero()), m.tokens)
        _add(by_phase.setdefault(phase_of(m.at, starts), _zero()), m.tokens)
        if m.effort:
            efforts.add(m.effort)
    return {"messages": len(messages), "tokens": tokens, "by_model": by_model,
            "by_phase": by_phase, "models": sorted(by_model), "efforts": sorted(efforts),
            "first_at": iso(messages[0].at) if messages else None,
            "last_at": iso(messages[-1].at) if messages else None}


def harness_figures(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The harness's own totals from the session's stream-json result, when it wrote one."""
    if not result:
        return None
    mu = result.get("modelUsage")
    by_model = {}
    if isinstance(mu, dict):
        for model, v in mu.items():
            if isinstance(v, dict):
                by_model[model] = {"cost_estimate_usd": v.get("costUSD"),
                                   "cost_basis": v.get("costBasis"),
                                   "input_tokens": v.get("inputTokens"),
                                   "output_tokens": v.get("outputTokens"),
                                   "cache_creation_input_tokens": v.get("cacheCreationInputTokens"),
                                   "cache_read_input_tokens": v.get("cacheReadInputTokens")}
    raw = result.get("usage")
    usage: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {"cost_estimate_usd": result.get("total_cost_usd"),
            "usage": {k: usage.get(k) for k in TOKEN_KEYS},
            "by_model": by_model, "duration_ms": result.get("duration_ms"),
            "duration_api_ms": result.get("duration_api_ms"), "num_turns": result.get("num_turns")}


def branch_from_brief(path: Path) -> str | None:
    """The branch a worker was briefed on: the first branches/<id>/ its first prompt names."""
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("type") == "user":
                m = _BRANCH_IN_BRIEF.search(json.dumps(rec.get("message"), ensure_ascii=False))
                return m.group(1) if m else None
    return None


# ----- the run's summary ---------------------------------------------------------------

def read_records(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / USAGE_FILE
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, Any]] = {}
    by_phase: dict[str, dict[str, int]] = {}
    total = _zero()
    cost = 0.0
    no_cost: list[str] = []
    for r in records:
        role = r["role"] if r["kind"] == "session" else f"{r['role']} (subagent)"
        row = by_role.setdefault(role, {"sessions": 0, "tokens": _zero(),
                                        "cost_estimate_usd": 0.0, "wall_s": 0.0})
        row["sessions"] += 1
        _add(row["tokens"], r["tokens"])
        _add(total, r["tokens"])
        row["wall_s"] += float(r.get("wall_s") or 0.0)
        for ph, t in r.get("by_phase", {}).items():
            _add(by_phase.setdefault(ph, _zero()), t)
        if r["kind"] == "session":
            c = (r.get("harness") or {}).get("cost_estimate_usd")
            if isinstance(c, int | float):
                row["cost_estimate_usd"] += float(c)
                cost += float(c)
            else:
                no_cost.append(r["session"])
    for row in by_role.values():
        row["cost_estimate_usd"] = round(row["cost_estimate_usd"], 4)
        row["wall_s"] = round(row["wall_s"], 1)
    return {"source": "transcripts of every session and subagent; see usage.jsonl",
            "cost_note": COST_NOTE + "; subagents' cost is inside their parent session's",
            "phase_note": "phases cut at the first request_frame_critique, submit_draft "
                          "and finish_run",
            "tokens": total, "cost_estimate_usd": round(cost, 4),
            "sessions_without_cost": no_cost, "by_role": by_role,
            "by_phase": {ph: by_phase[ph] for ph, _m in PHASES if ph in by_phase}}


def _n(v: int) -> str:
    return f"{v:,}"


def render_md(run_id: str, s: dict[str, Any]) -> str:
    def line(name: str, t: dict[str, int], extra: str = "") -> str:
        return (f"- **{name}**: {_n(t['input_tokens'])} input, {_n(t['output_tokens'])} "
                f"output, {_n(t['cache_creation_input_tokens'])} written to cache, "
                f"{_n(t['cache_read_input_tokens'])} read from cache{extra}.")

    out = [f"# Token use of run {run_id}", "",
           f"Counted from {s['source']}. Cost is {s['cost_note']}.", "",
           line("Whole run", s["tokens"],
                f"; cost estimate ${s['cost_estimate_usd']:,.2f}"), "", "## By role", ""]
    for role, row in sorted(s["by_role"].items(),
                            key=lambda kv: -kv[1]["tokens"]["output_tokens"]):
        cost = (f"; cost estimate ${row['cost_estimate_usd']:,.2f}"
                if "subagent" not in role else "; cost inside the parent session")
        out.append(line(f"{role}, {row['sessions']} session(s), "
                        f"{row['wall_s'] / 3600:.2f} h", row["tokens"], cost))
    out += ["", "## By phase", "", f"Phases are {s['phase_note']}.", ""]
    out += [line(ph, t) for ph, t in s["by_phase"].items()]
    if s["sessions_without_cost"]:
        out += ["", "Sessions with no cost estimate (stopped before the harness wrote its "
                    "result): " + ", ".join(s["sessions_without_cost"]) + "."]
    return "\n".join(out) + "\n"


def write_md(run_dir: Path, run_id: str, s: dict[str, Any]) -> None:
    hostio.write_text(run_dir, USAGE_MD, render_md(run_id, s))
