"""The lead's execution time, counted one way for every path that limits it.

`lead_max_s` (run.json) bounds the time the lead's sessions run, added up across every
launch and resume. The gaps between sessions (a wait for a usage reset, a driver that
was down) do not count. The driver's police, its resume of an ended lead, the automatic
resume after a usage stop, and the lead's Stop hook all read it from here, so none of
them gives a resumed lead the full limit again or counts a pause as work.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from stemma.register import jsonl


def _ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def lead_time_used(run_dir: Path, now: float | None = None) -> float:
    """Seconds the lead's sessions have run, from the `session_launched` and
    `session_ended` events of role lead. The launch still open at the end (the session
    running now) counts up to `now` when given, else up to the last event in the log.
    An earlier launch with no recorded end (a driver that died) counts up to the last
    event logged before the next launch: the most it can have run unseen."""
    total = 0.0
    last = 0.0
    open_name: str | None = None
    open_at = 0.0
    for e in jsonl.read_all(run_dir / "events.jsonl"):
        t = _ts(e.get("at"))
        if t is None:
            continue
        if e.get("role") == "lead" and e.get("event") == "session_launched":
            if open_name is not None:
                total += max(0.0, last - open_at)  # an end never recorded
            open_name, open_at = str(e.get("session")), t
        elif (e.get("role") == "lead" and e.get("event") == "session_ended"
              and open_name is not None and str(e.get("session")) == open_name):
            total += max(0.0, t - open_at)
            open_name = None
        last = max(last, t)
    if open_name is not None:
        total += max(0.0, (now if now is not None else last) - open_at)
    return total


def lead_time_left(run_dir: Path, lead_max_s: float, now: float | None = None) -> float:
    return lead_max_s - lead_time_used(run_dir, now)
