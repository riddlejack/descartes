"""Attachments mid-run (CONTRACTS section 9, "Attachments mid-run").

`stemma attach <run_id> <file>` copies the file into `task/arrivals/<n>/` (tool-only),
records it in `.stemma/arrivals.jsonl` and an `arrival` event, and so queues a notice.
The lead's next tool call takes every queued notice through the PreToolUse hook's
`additionalContext`, which the harness hands to the model with that call (probed on
Claude Code 2.1.308, headless). The hook holds the log's lock while it records its
events and builds its answer, and marks the notice delivered last: a call the guards
refuse, or any failure before that mark, leaves the notice queued for the next call.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from stemma.register import jsonl, roles, safeio

LOG = ".stemma/arrivals.jsonl"
ARRIVALS_DIR = "task/arrivals"
STAGING = "task/arrivals/.staging"  # copies land here, unlocked, before they are published
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
    digest = hashlib.sha256(data).hexdigest()
    staged = f"{STAGING}/{secrets.token_hex(8)}"
    safeio.write_bytes(run_dir, staged, data)  # the copy, without the lock; no link on the way
    log = _log(run_dir)
    try:
        with jsonl.exclusive(log):  # held only to number, publish (a rename) and queue
            records = jsonl.read_all(log)
            n = 1 + max((int(r["n"]) for r in records if r["kind"] in ("arrival", "notice")),
                        default=0)
            rel = f"{ARRIVALS_DIR}/{n}/{name}"
            safeio.move(run_dir, staged, rel)
            rec = {"kind": "arrival", "n": n, "path": rel, "name": name, "bytes": len(data),
                   "sha256": digest, "at": _now()}
            jsonl.append_unlocked(log, [rec])
    finally:
        try:
            with safeio.folder_fd(run_dir, STAGING) as sfd:
                os.unlink(PurePosixPath(staged).name, dir_fd=sfd)
        except (FileNotFoundError, safeio.UnsafePath):
            pass  # published, or never staged
    jsonl.append(run_dir / "events.jsonl",
                  {"event": "arrival", "source": "attach", **{k: v for k, v in rec.items()
                                                              if k != "kind"}})
    return rec


def queue_notice(run_dir: Path, name: str, text: str) -> dict[str, Any]:
    """Queue a driver notice for the lead's next tool call (delivered like an arrival,
    once). Idempotent per name."""
    log = _log(run_dir)
    with jsonl.exclusive(log):
        records = jsonl.read_all(log)
        for r in records:
            if r["kind"] == "notice" and r["name"] == name:
                return r
        n = 1 + max((int(r["n"]) for r in records if r["kind"] in ("arrival", "notice")),
                    default=0)
        rec = {"kind": "notice", "n": n, "name": name, "text": text, "at": _now()}
        jsonl.append_unlocked(log, [rec])
    return rec


def notice_text(due: list[dict[str, Any]]) -> str:
    lines = [r["text"] for r in due if r["kind"] == "notice"]
    arrivals = [r for r in due if r["kind"] == "arrival"]
    if arrivals:
        lines.append("Stemma: new material arrived from the user during the run.")
        for a in arrivals:
            lines.append(f"- {a['path']} ({a['bytes']} bytes, sha256 {a['sha256'][:12]}, "
                         f"arrived {a['at']}).")
        # The lead reads no sources (D6): a worker captures each arrival.
        lines.append("Have a worker capture each with capture_file, and map it at once "
                     "against the frame and tree, recording in intake.md what it changed.")
    return "\n".join(lines)


def is_lead_main_thread(hook: dict[str, Any]) -> bool:
    return hook.get("agent_type") == roles.LEAD and not hook.get("agent_id")  # see guards


@contextmanager
def held(run_dir: Path, hook: dict[str, Any]) -> Iterator[list[dict[str, Any]]]:
    """For the lead's own tool call: hold the log's lock and yield every arrival not
    yet delivered (empty for anyone else, or when nothing arrived). The caller records
    its events, builds its answer, then calls `mark_delivered` inside the block."""
    log = _log(run_dir)
    if not is_lead_main_thread(hook) or not log.exists():
        yield []
        return
    with jsonl.exclusive(log):
        records = jsonl.read_all(log)
        delivered = {int(r["n"]) for r in records if r["kind"] == "delivered"}
        yield [r for r in records
               if r["kind"] in ("arrival", "notice") and int(r["n"]) not in delivered]


def delivery_events(due: list[dict[str, Any]], hook: dict[str, Any]) -> list[dict[str, Any]]:
    at = _now()
    return [{"event": "arrival_notice_delivered" if r["kind"] == "arrival"
             else "driver_notice_delivered", "at": at, "n": r["n"],
             "path": r.get("path"), "name": r.get("name"),
             "session_id": hook.get("session_id"), "tool_use_id": hook.get("tool_use_id"),
             "tool": hook.get("tool_name")} for r in due]


def mark_delivered(run_dir: Path, due: list[dict[str, Any]], hook: dict[str, Any]) -> None:
    """Inside `held`: the notice of `due` went out with this call's answer."""
    at = _now()
    jsonl.append_unlocked(_log(run_dir), [
        {"kind": "delivered", "n": r["n"], "at": at, "session_id": hook.get("session_id"),
         "tool_use_id": hook.get("tool_use_id"), "tool": hook.get("tool_name")} for r in due])
