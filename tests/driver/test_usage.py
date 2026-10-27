"""Token accounting: an invented recorded transcript read the way 2.1.308 writes it, and
a driver run on the fake harness that records every session and subagent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from driverkit import Fake, write_task

from stemma.driver import usage
from stemma.driver.config import DriverConfig, Limits
from stemma.driver.runfolder import create_run
from stemma.driver.runner import resume, start
from stemma.register import checks_api


def u(i: int, o: int, cw: int, cr: int) -> dict[str, int]:
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr}


def entry(mid: str, at: str, usage_: dict[str, int], model: str = "claude-opus-5-5",
          effort: str = "high") -> dict[str, Any]:
    return {"type": "assistant", "timestamp": at, "effort": effort,
            "message": {"id": mid, "model": model, "content": [], "usage": usage_}}


def write(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def test_a_recorded_transcript_counts_each_message_once(tmp_path: Path) -> None:
    t = write(tmp_path / "s.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "claim:ab"}},
        entry("m1", "2026-10-26T07:00:01.000Z", u(2, 10, 100, 500)),
        entry("m1", "2026-10-26T07:00:01.200Z", u(2, 40, 100, 500)),  # same message, final
        entry("m2", "2026-10-26T07:00:05.000Z", u(1, 7, 0, 900)),
        {"type": "assistant", "timestamp": "2026-10-26T07:00:06.000Z",
         "message": {"id": "m3", "model": "<synthetic>", "usage": u(9, 9, 9, 9)}},
        {"type": "assistant", "timestamp": "2026-10-26T07:00:07.000Z",
         "message": {"id": "m4", "model": "claude-opus-5-5"}},  # no usage: not counted
    ])
    with open(t, "a") as f:
        f.write("a line cut short by a crash\n")
    msgs = usage.read_messages(t)
    assert [m.message_id for m in msgs] == ["m1", "m2"]
    tally = usage.tally(msgs, [("framing", float("-inf"))])
    assert tally["tokens"] == u(3, 47, 100, 1400)
    assert tally["messages"] == 2 and tally["efforts"] == ["high"]
    assert tally["first_at"] == "2026-10-26T07:00:01.000Z"


def test_a_resumed_session_counts_only_its_own_launch(tmp_path: Path) -> None:
    t = write(tmp_path / "lead.jsonl", [
        entry("a", "2026-10-26T07:00:00.000Z", u(1, 1, 1, 1)),
        entry("b", "2026-10-26T08:00:00.000Z", u(2, 2, 2, 2)),
    ])
    first, second = usage._ts("2026-10-26T06:59:59.000Z"), usage._ts(
        "2026-10-26T07:59:59.500Z")
    assert first is not None and second is not None
    msgs = usage.read_messages(t)
    assert [m.message_id for m in usage.in_window(msgs, first, first + 60)] == ["a"]
    assert [m.message_id for m in usage.in_window(msgs, second, second + 60)] == ["b"]


def test_phases_are_cut_at_the_first_milestone_of_each() -> None:
    ev = [{"event": "register_write", "tool": "tree_upsert", "at": "2026-10-26T07:01:00Z"},
          {"event": "register_write", "tool": "request_frame_critique",
           "at": "2026-10-26T07:10:00Z"},
          {"event": "register_write", "tool": "submit_draft", "at": "2026-10-26T08:00:00Z"},
          {"event": "register_write", "tool": "submit_draft", "at": "2026-10-26T09:00:00Z"}]
    starts = usage.phase_starts(ev)
    assert [n for n, _ in starts] == ["framing", "research", "drafting and review"]
    at = usage._ts
    assert usage.phase_of(at("2026-10-26T07:05:00Z") or 0, starts) == "framing"
    assert usage.phase_of(at("2026-10-26T07:30:00Z") or 0, starts) == "research"
    assert usage.phase_of(at("2026-10-26T09:30:00Z") or 0, starts) == "drafting and review"


def test_the_harness_estimate_is_kept_and_labelled() -> None:
    h = usage.harness_figures({"total_cost_usd": 0.4, "usage": u(18, 3077, 37336, 198866),
                               "modelUsage": {"claude-opus-5-5": {"costUSD": 0.4,
                                                                  "costBasis": "list",
                                                                  "outputTokens": 3077}}})
    assert h is not None and h["cost_estimate_usd"] == 0.4
    assert h["by_model"]["claude-opus-5-5"]["cost_basis"] == "list"
    assert usage.harness_figures(None) is None
    s = usage.summary([{"kind": "session", "role": "claim-checker", "session": "c1",
                        "tokens": u(1, 2, 3, 4), "by_phase": {"research": u(1, 2, 3, 4)},
                        "wall_s": 10, "harness": h},
                       {"kind": "session", "role": "lead", "session": "lead",
                        "tokens": u(5, 5, 5, 5), "by_phase": {}, "wall_s": 5,
                        "harness": None}])
    assert s["cost_estimate_usd"] == 0.4 and s["sessions_without_cost"] == ["lead"]
    md = usage.render_md("r1", s)
    assert "estimate, not a bill" in md and "stopped before the harness wrote" in md


def test_a_run_records_every_session_and_subagent(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                  limits: Limits) -> None:
    fake.configure({
        "lead": {"sleep_s": 0.5, "cost": 1.5, "usage": u(10, 200, 3000, 9000),
                 "subagents": [{"agentType": "stemma:worker", "model": "claude-opus-5-5",
                                "effort": "high", "branch": "b2-supply",
                                "usage": u(4, 80, 900, 2000)}]},
        "blind-framer": {"cost": 0.1, "usage": u(1, 30, 400, 600)},
        "idea-generator": {"cost": 0.0}})
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits)
    recs = usage.read_records(run)
    kinds = sorted((r["kind"], r["role"]) for r in recs)
    assert kinds == [("session", "blind-framer"), ("session", "idea-generator"),
                     ("session", "lead"), ("subagent", "worker")]
    lead = next(r for r in recs if r["role"] == "lead")
    assert lead["tokens"] == u(10, 200, 3000, 9000)  # the message in two entries, once
    assert lead["harness"]["cost_estimate_usd"] == 1.5
    worker = next(r for r in recs if r["kind"] == "subagent")
    assert worker["tokens"] == u(4, 80, 900, 2000) and worker["branch"] == "b2-supply"
    assert worker["models"] == ["claude-opus-5-5"] and worker["efforts"] == ["high"]
    st = json.loads((run / "status.json").read_text())
    assert st["usage"]["by_role"]["worker (subagent)"]["sessions"] == 1
    assert st["usage"]["cost_estimate_usd"] == 1.6
    assert "Token use" in (run / "status.html").read_text()
    md = (run / "usage.md").read_text()
    assert "lead, 1 session(s)" in md and "worker (subagent)" in md

    # A resume appends to the lead's transcript; the new record counts only the new launch.
    fake.configure({"lead": {"sleep_s": 0.3, "usage": u(1, 1, 1, 1)}})
    resume(cfg, run, checks_api, limits)
    leads = [r for r in usage.read_records(run) if r["role"] == "lead"]
    assert [x["tokens"] for x in leads] == [u(10, 200, 3000, 9000), u(1, 1, 1, 1)]
