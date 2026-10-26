"""`status.json` and a plain `status.html`: what is running, what landed, what waits on the user."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from stemma.driver import events, hostio


def _latest_verdict(run_dir: Path, branch_id: str) -> dict[str, Any] | None:
    """The newest `verdicts/<n>.json` of a branch (at any depth under branches/)."""
    if not branch_id or "/" in branch_id or branch_id.startswith("."):
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for d in [run_dir / "branches" / branch_id, *(run_dir / "branches").glob(f"**/sub/{branch_id}")]:
        for f in (d / "verdicts").glob("*.json"):
            if not f.stem.isdigit():
                continue
            try:
                v = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(v, dict) and (best is None or int(f.stem) > best[0]):
                best = (int(f.stem), v)
    return best[1] if best else None


def waiting_on_user(run_dir: Path) -> list[dict[str, Any]]:
    """Tree nodes whose status is `waiting_user` (section 5), each with its question: the
    reason of the branch's latest verdict when that verdict is `ask_user`."""
    try:
        tree = json.loads((run_dir / "tree.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    nodes = tree.get("nodes") if isinstance(tree, dict) else tree
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    if not isinstance(nodes, list):
        return []
    out = []
    for n in nodes:
        if not isinstance(n, dict) or n.get("status") != "waiting_user":
            continue
        v = _latest_verdict(run_dir, str(n.get("branch_id") or ""))
        question = v.get("reason") if v and v.get("verdict") == "ask_user" else None
        out.append({"id": n.get("id"), "statement": n.get("statement"),
                    "branch_id": n.get("branch_id"), "question": question})
    return out


def build(run_dir: Path, run_id: str, state: str, lead: dict[str, Any],
          checks: list[dict[str, Any]], landed: list[dict[str, Any]],
          outcome: str | None = None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    alerts = [e for e in events.read(run_dir) if e.get("event") in events.LOUD]
    return {
        "run_id": run_id,
        "updated_at": events.now(),
        "state": state,
        "outcome": outcome,
        "lead": lead,
        "checks": {
            "running": [c for c in checks if c["status"] == "running"],
            "pending": [c for c in checks if c["status"] == "pending"],
            "ended": [c for c in checks if c["status"] == "ended"],
        },
        "landed": landed,
        "waiting_on_user": waiting_on_user(run_dir),
        "lead_last_message": (lead.get("result") or {}).get("result"),
        "alerts": alerts,
        "usage": usage,
    }


def _li(items: list[str]) -> str:
    if not items:
        return "<p>None.</p>"
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def _usage_html(u: dict[str, Any] | None) -> str:
    if not u or not u.get("by_role"):
        return "<p>No session has ended yet.</p>"
    e = html.escape

    def tok(t: dict[str, int]) -> str:
        return (f"{t['input_tokens']:,} input, {t['output_tokens']:,} output, "
                f"{t['cache_creation_input_tokens']:,} written to cache, "
                f"{t['cache_read_input_tokens']:,} read from cache")

    rows = [f"Whole run: {tok(u['tokens'])}; cost estimate ${u['cost_estimate_usd']:,.2f}"]
    rows += [f"{e(role)} ({row['sessions']}): {tok(row['tokens'])}"
             + (f"; cost estimate ${row['cost_estimate_usd']:,.2f}"
                if "subagent" not in role else "")
             for role, row in sorted(u["by_role"].items())]
    rows += [f"Phase {e(ph)}: {tok(t)}" for ph, t in u.get("by_phase", {}).items()]
    return _li(rows) + f"<p>{e(u['cost_note'])}. Phases are {e(u['phase_note'])}.</p>"


def render_html(st: dict[str, Any]) -> str:
    e = html.escape
    lead = st["lead"]
    lead_line = (f"Lead session {e(str(lead.get('session_id')))}: "
                 f"{'running' if lead.get('running') else 'not running'}"
                 + (f", last outcome {e(str(lead.get('outcome')))}" if lead.get("outcome") else ""))
    def check_line(c: dict[str, Any]) -> str:
        return (f"{e(c['role'])} on {e(c['prompt'])}"
                + (f", outcome {e(str(c.get('outcome')))}" if c.get("outcome") else ""))
    running = [lead_line] if lead.get("running") else []
    running += [check_line(c) for c in st["checks"]["running"]]
    waits = [f"{e(str(n.get('id')))} ({e(str(n.get('statement')))}): "
             + (e(str(n["question"])) if n.get("question")
                else "no question recorded (the branch's latest verdict is not ask_user)")
             for n in st["waiting_on_user"]]
    if st["state"] != "running" and st.get("lead_last_message"):
        waits.append("The lead's last message: " + e(str(st["lead_last_message"])))
    alerts = [f"{e(a.get('at', ''))} {e(a.get('event', ''))}: "
              f"{e(json.dumps({k: v for k, v in a.items() if k not in ('at', 'event')}))}"
              for a in st["alerts"]]
    return "".join([
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f"<title>Run {e(st['run_id'])}</title>",
        ("<style>body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;"
        "padding:0 1rem;line-height:1.5}</style></head><body>"),
        f"<h1>Run {e(st['run_id'])}</h1>",
        f"<p>State: {e(st['state'])}"
        + (f", outcome {e(str(st['outcome']))}" if st.get("outcome") else "")
        + f". Updated {e(st['updated_at'])}.</p>",
        "<h2>Running now</h2>", _li(running),
        "<h2>Waiting on you</h2>", _li(waits),
        "<h2>Needs attention</h2>", _li(alerts),
        "<h2>Checks waiting to start</h2>", _li([check_line(c)
                                                 for c in st["checks"]["pending"]]),
        "<h2>Checks finished</h2>", _li([check_line(c) for c in st["checks"]["ended"]]),
        "<h2>Token use (ended sessions)</h2>", _usage_html(st.get("usage")),
        "<h2>What has landed</h2>",
        _li([f"{e(a['path'])} ({e(a['at'])})" for a in st["landed"]]),
        "</body></html>\n",
    ])


def write(run_dir: Path, st: dict[str, Any]) -> None:
    hostio.write_json(run_dir, "status.json", st)
    hostio.write_text(run_dir, "status.html", render_html(st))
