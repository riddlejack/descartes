"""The driver's records in `events.jsonl`, through the lock protocol the hooks share."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.register import jsonl

EVENTS = "events.jsonl"

# Events that need a person's attention; status pages list them as alerts.
LOUD = frozenset({
    "canary_failed", "launch_failed", "model_effort_mismatch", "unexpected_subagent",
    "session_stopped_by_limit", "stream_unparsed", "invalid_check_request",
    "transcript_missing", "check_revoked", "revoke_failed",
    "check_given_up", "usage_not_recorded", "usage_write_deferred", "usage_records_skipped",
})


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(run_dir: Path, event: str, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"event": event, "at": now(), "source": "driver", **fields}
    jsonl.append(run_dir / EVENTS, record)
    return record


def read(run_dir: Path) -> list[dict[str, Any]]:
    return jsonl.read_all(run_dir / EVENTS)
