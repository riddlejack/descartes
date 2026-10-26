"""SAMPLE_MODE: record a finished real run as a pack, and replay it, never as live.

A **pack** is a folder holding copies of a real run folder's files and a manifest that
names each file with its SHA-256 and size, the run it came from and when it was
recorded. Only an allowlist of what a reader needs is recorded (the question, frame,
tree, branch records, register, reviews, deliverable, events, receipts and stored text,
the data layer's query log); everything else is listed as left out, with the reason.

A **replay** checks every file against the manifest (a changed byte is a typed error),
copies it into a fresh folder, and writes `REPLAY.json` there: every item with its hash
and the label `replay`. Everything that shows a replay (the judge log, `run.py`) reads
that file and labels each recorded line `[replay]`. Only what really runs now, such as
the World Model conformance suite or a data-layer query over the replayed rows, is
labelled `[live]`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT = "stemma-replay/1"
MARKER = "REPLAY.json"
# What a reader of a run needs; all else is left out and listed (logical invariant:
# the driver's own state, the assembled plugin and raw fetched bytes are never shown).
RECORD = ("run.json", "task/*", "intake.md", "frame.md", "tree.json", "tree.md",
          "framer/*", "branches/*", "register/log.jsonl", "register/checks.jsonl",
          "draft/*", "reviews/*", "deliverable/*", "events.jsonl", "status.json", "retro.md",
          "analysis/*", "store/receipts/*", "store/text/*", "datalayer/queries.jsonl")
# Resource limits: the size of one recorded file, and of a whole pack.
MAX_FILE_BYTES = 5_000_000
MAX_PACK_BYTES = 25_000_000
# Logical invariant: shapes of credentials that must never be in a pack.
_SECRET_SHAPES = re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}|nvapi-[A-Za-z0-9_-]{8,}|"
                            rb"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY")


class ReplayError(Exception):
    """A pack is incomplete, changed, too large, or holds something it must not."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _wanted(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pat) for pat in RECORD)


def _plain(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts or any(x in ("", ".", "..") for x in p.parts):
        raise ReplayError(f"{rel!r} is not a plain path inside a run")
    return p


def record(run_dir: Path, pack_dir: Path) -> dict[str, Any]:
    """Copy a finished run's readable files into a new pack, with a hashed manifest."""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if pack_dir.exists() and any(pack_dir.iterdir()):
        raise ReplayError(f"{pack_dir} is not empty")
    items: list[dict[str, Any]] = []
    left_out: list[dict[str, str]] = []
    total = 0
    for path in sorted(run_dir.rglob("*")):
        rel = path.relative_to(run_dir).as_posix()
        if path.is_symlink():
            left_out.append({"path": rel, "reason": "a link"})
            continue
        if not path.is_file():
            continue
        if not _wanted(rel):
            left_out.append({"path": rel, "reason": "not on the record list"})
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            left_out.append({"path": rel, "reason": f"larger than {MAX_FILE_BYTES} bytes"})
            continue
        if _SECRET_SHAPES.search(data):
            raise ReplayError(f"{rel} holds something shaped like a credential; not recorded")
        total += len(data)
        if total > MAX_PACK_BYTES:
            raise ReplayError(f"the pack would pass {MAX_PACK_BYTES} bytes")
        dest = pack_dir / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        items.append({"path": rel, "sha256": sha256(data), "bytes": len(data)})
    manifest = {"format": FORMAT, "source_run_id": run.get("run_id"),
                "source_created_at": run.get("created_at"),
                "source_harness": run.get("harness_version") or run.get("harness"),
                "recorded_at": _now(), "items": items, "left_out": left_out}
    (pack_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def load_manifest(pack_dir: Path) -> tuple[dict[str, Any], str]:
    raw = (pack_dir / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("items"), list):
        raise ReplayError(f"{pack_dir} is not a {FORMAT} pack")
    return manifest, sha256(raw)


def verify(pack_dir: Path) -> dict[str, Any]:
    """Every listed file present with its recorded hash and size, and nothing unlisted."""
    manifest, _ = load_manifest(pack_dir)
    listed = set()
    for item in manifest["items"]:
        rel = _plain(item["path"]).as_posix()
        listed.add(rel)
        path = pack_dir / "files" / rel
        if path.is_symlink() or not path.is_file():
            raise ReplayError(f"{rel} is missing from the pack")
        data = path.read_bytes()
        if len(data) != item["bytes"] or sha256(data) != item["sha256"]:
            raise ReplayError(f"{rel} does not match its recorded hash")
    for path in (pack_dir / "files").rglob("*"):
        if path.is_file() and path.relative_to(pack_dir / "files").as_posix() not in listed:
            raise ReplayError(f"{path.name} is in the pack but not in its manifest")
    return manifest


@dataclass(frozen=True)
class Replay:
    folder: Path
    marker: dict[str, Any]


def replay(pack_dir: Path, runs_dir: Path) -> Replay:
    """A fresh folder holding the pack's files, each checked, and its REPLAY.json."""
    manifest = verify(pack_dir)
    _, manifest_sha = load_manifest(pack_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder = runs_dir / f"replay-{manifest['source_run_id']}-{stamp}-{os.urandom(3).hex()}"
    folder.mkdir(parents=True)
    items = []
    for item in manifest["items"]:
        rel = item["path"]
        dest = folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pack_dir / "files" / rel, dest)
        if sha256(dest.read_bytes()) != item["sha256"]:
            raise ReplayError(f"{rel} changed while being copied")
        items.append({"path": rel, "sha256": item["sha256"], "bytes": item["bytes"],
                      "label": "replay"})
    marker = {"mode": "replay", "label": "replay",
              "notice": "This folder replays a recorded run. Nothing in it ran now.",
              "source_run_id": manifest["source_run_id"],
              "source_created_at": manifest.get("source_created_at"),
              "recorded_at": manifest["recorded_at"], "replayed_at": _now(),
              "pack_manifest_sha256": manifest_sha, "items": items}
    (folder / MARKER).write_text(json.dumps(marker, indent=1), encoding="utf-8")
    return Replay(folder, marker)


def replay_marker(run_dir: Path) -> dict[str, Any] | None:
    """The folder's REPLAY.json, or None for a live run folder."""
    path = run_dir / MARKER
    if not path.exists():
        return None
    marker: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return marker


def labelled_text(run_dir: Path, rel: str) -> list[tuple[str, str]]:
    """Each line of a run file with its label: `replay sha:<12 hex>` for the bytes that were
    replayed, `live` for anything added since (a live query appended to a replayed log).

    A replayed file whose recorded bytes changed is an error, never relabelled."""
    path = run_dir / rel
    data = path.read_bytes() if path.is_file() else b""
    marker = replay_marker(run_dir)
    item = next((i for i in marker["items"] if i["path"] == rel), None) if marker else None
    if item is None:
        return [("live", line) for line in data.decode("utf-8", "replace").splitlines()]
    head, tail = data[: item["bytes"]], data[item["bytes"]:]
    if sha256(head) != item["sha256"]:
        raise ReplayError(f"{rel} changed after it was replayed")
    label = f"replay sha:{item['sha256'][:12]}"
    return ([(label, line) for line in head.decode("utf-8", "replace").splitlines()]
            + [("live", line) for line in tail.decode("utf-8", "replace").splitlines()])
