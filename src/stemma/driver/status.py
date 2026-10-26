"""`status.json` and a plain `status.html`: what is running, what landed, what waits on the user."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from stemma.driver import events
from stemma.driver.runfolder import write_json_atomic


def waiting_on_user(run_dir: Path) -> list[dict[str, Any]]:
    """Tree nodes whose status is `waiting_user` (section 5). Absent tree: none."""
    try:
        tree = json.loads((run_dir / "tree.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    nodes = tree.get("nodes") if isinstance(tree, dict) else tree
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    if not isinstance(nodes, list):
        return []
    return [{"id": n.get("id"), "statement": n.get("statement")} for n in nodes
            if isinstance(n, dict) and n.get("status") == "waiting_user"]


def build(run_dir: Path, run_id: str, state: str, lead: dict[str, Any],
          checks: list[dict[str, Any]], landed: list[dict[str, Any]]) -> dict[str, Any]:
    alerts = [e for e in events.read(run_dir) if e.get("event") in events.LOUD]
    return {
        "run_id": run_id,
        "updated_at": events.now(),
        "state": state,
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
    }


def _li(items: list[str]) -> str:
    if not items:
        return "<p>None.</p>"
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


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
    waits = [f"{e(str(n.get('id')))}: {e(str(n.get('statement')))}"
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
        f"<p>State: {e(st['state'])}. Updated {e(st['updated_at'])}.</p>",
        "<h2>Running now</h2>", _li(running),
        "<h2>Waiting on you</h2>", _li(waits),
        "<h2>Needs attention</h2>", _li(alerts),
        "<h2>Checks waiting to start</h2>", _li([check_line(c)
                                                 for c in st["checks"]["pending"]]),
        "<h2>Checks finished</h2>", _li([check_line(c) for c in st["checks"]["ended"]]),
        "<h2>What has landed</h2>",
        _li([f"{e(a['path'])} ({e(a['at'])})" for a in st["landed"]]),
        "</body></html>\n",
    ])


def write(run_dir: Path, st: dict[str, Any]) -> None:
    write_json_atomic(run_dir / "status.json", st)
    tmp = run_dir / ".status.html.tmp"
    tmp.write_text(render_html(st), encoding="utf-8")
    tmp.replace(run_dir / "status.html")
