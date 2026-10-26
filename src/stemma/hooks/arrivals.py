"""Attachments mid-run (CONTRACTS section 9, "Attachments mid-run").

`stemma attach <run_id> <file>` copies the file into `task/arrivals/<n>/` (tool-only),
records it in `.stemma/arrivals.jsonl` and an `arrival` event, and so queues a notice.
The lead's next tool call takes every queued notice through the PreToolUse hook's
`additionalContext`, which the harness hands to the model with that call (probed on
Claude Code 2.1.308, headless). A notice is marked delivered once, under the log's lock,
and only on a call the guards allow, so a refused call never swallows it.
"""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stemma.register import jsonl, roles, safeio

LOG = ".stemma/arrivals.jsonl"
ARRIVALS_DIR = "task/arrivals"
# Resource limit: bytes of one attached file (it is copied whole into the run).
ATTACH_MAX_BYTES = 512 * 1024 * 1024
# Resource limit: characters of a file name kept in the run (longer names are refused).
NAME_MAX_CHARS = 200


class AttachError(Exception):
    """The file cannot be attached: not a regular file, too large, or a bad name."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log(run_dir: Path) -> Path:
    return run_dir / LOG


def _read_source(src: Path) -> bytes:
    """The user's file, a regular file opened without following a link, within limits."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise AttachError(f"{src} is not a regular file that can be opened") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AttachError(f"{src} is not a regular file")
        if st.st_size > ATTACH_MAX_BYTES:
            raise AttachError(f"{src} is larger than {ATTACH_MAX_BYTES} bytes")
        os.set_blocking(fd, True)
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            if total > ATTACH_MAX_BYTES:
                raise AttachError(f"{src} is larger than {ATTACH_MAX_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _plain_name(name: str) -> str:
    if (not name or name in (".", "..") or "/" in name or "\x00" in name
            or name.startswith(".") or len(name) > NAME_MAX_CHARS):
        raise AttachError(f"file name {name!r} is not a plain name of at most "
                          f"{NAME_MAX_CHARS} characters")
    return name


def attach(run_dir: Path, src: Path) -> dict[str, Any]:
    """Copy `src` into task/arrivals/<n>/ and queue its notice; return the record."""
    if not (run_dir / "run.json").is_file():
        raise AttachError(f"{run_dir} is not a run folder (no run.json)")
    name = _plain_name(src.name)
    data = _read_source(src)
    log = _log(run_dir)
    with jsonl.exclusive(log):
        records = jsonl.read_all(log)
        n = 1 + max((int(r["n"]) for r in records if r["kind"] == "arrival"), default=0)
        rel = f"{ARRIVALS_DIR}/{n}/{name}"
        safeio.write_bytes(run_dir, rel, data)  # no link on the way; a fresh file
        rec = {"kind": "arrival", "n": n, "path": rel, "name": name, "bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest(), "at": _now()}
        jsonl.append_unlocked(log, [rec])
    jsonl.append(run_dir / "events.jsonl",
                  {"event": "arrival", "source": "attach", **{k: v for k, v in rec.items()
                                                              if k != "kind"}})
    return rec


def notice_text(arrivals: list[dict[str, Any]]) -> str:
    lines = ["Stemma: new material arrived from the user during the run."]
    for a in arrivals:
        lines.append(f"- {a['path']} ({a['bytes']} bytes, sha256 {a['sha256'][:12]}, "
                     f"arrived {a['at']}).")
    lines.append("Capture each with capture_file and map it at once against the frame and "
                 "tree, recording in intake.md what it changed.")
    return "\n".join(lines)


def is_lead_main_thread(hook: dict[str, Any]) -> bool:
    return hook.get("agent_type") == roles.LEAD and not hook.get("agent_id")


def take_notice(run_dir: Path, hook: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """For the lead's own tool call: the notice of every arrival not yet delivered, now
    marked delivered, and the events to record. Anyone else gets nothing."""
    if not is_lead_main_thread(hook):
        return None, []
    log = _log(run_dir)
    if not log.exists():
        return None, []
    with jsonl.exclusive(log):
        records = jsonl.read_all(log)
        delivered = {int(r["n"]) for r in records if r["kind"] == "delivered"}
        due = [r for r in records if r["kind"] == "arrival" and int(r["n"]) not in delivered]
        if not due:
            return None, []
        at = _now()
        marks = [{"kind": "delivered", "n": r["n"], "at": at,
                  "session_id": hook.get("session_id"), "tool_use_id": hook.get("tool_use_id"),
                  "tool": hook.get("tool_name")} for r in due]
        jsonl.append_unlocked(log, marks)
    events = [{"event": "arrival_notice_delivered", "at": at, "n": r["n"], "path": r["path"],
               "session_id": hook.get("session_id"), "tool_use_id": hook.get("tool_use_id"),
               "tool": hook.get("tool_name")} for r in due]
    return notice_text(due), events
