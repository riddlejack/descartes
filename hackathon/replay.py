"""SAMPLE_MODE: record a finished real run as a pack, and replay it, never as live.

A **pack** is a folder holding copies of a real run folder's files and a manifest that
names each file with its SHA-256 and size, the run it came from and when it was
recorded. Only an allowlist of what a reader needs is recorded (the question, frame,
tree, branch records, register, reviews, deliverable, events, receipts and stored text,
the data layer's query log and the provider receipts); everything else is listed as left
out, with the reason. A file holding anything shaped like a credential stops the
recording: it is refused, never redacted.

A **replay** checks every file against the manifest (a changed byte is a typed error),
copies it into a fresh folder, and writes `REPLAY.json` there: every item with its hash
and the label `replay`.

**Labels.** A line read from a file is never `live`. It is `replay sha:<hash>` when its
bytes are the replayed bytes, `recorded sha:<hash>` in a folder without a replay marker,
and `appended after replay` for bytes added to a replayed file later. Only a step this
process ran itself, and recorded as it ran, is shown `[live]` (see `judgelog`).

Every manifest field is hostile: paths must be canonical relative paths, the run id a
plain identifier, and every destination is checked to stay inside its folder after
resolution.
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
          "analysis/*", "store/receipts/*", "store/text/*", "datalayer/queries.jsonl",
          "datalayer/providers.jsonl")
# Resource limits: the size of one recorded file, and of a whole pack.
MAX_FILE_BYTES = 5_000_000
MAX_PACK_BYTES = 25_000_000
# Logical invariants: what a run id and a path segment may be.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+=-]{0,254}")

# Logical invariants: the shapes of credentials a pack must never hold. A value made
# only of masking characters (`***`, `<redacted>`, `<set>`) is not a credential.
_SECRET_NAME = (r"[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|credential|"
                r"private[_-]?key|access[_-]?key|auth[_-]?token|authorization)[A-Za-z0-9_.-]*")
_MASK = r"(?:\*+|<[a-z ]+>|redacted|REDACTED)"
_SECRET_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a key, token or password assignment", re.compile(
        rf"(?im)^\s*(?:export\s+)?{_SECRET_NAME}\s*[=:]\s*(?!{_MASK}\s*$)['\"]?[^\s'\"#]{{4,}}")),
    ("a key, token or password field", re.compile(
        rf"(?i)[\"']{_SECRET_NAME}[\"']\s*:\s*[\"'](?!{_MASK}[\"'])[^\"']{{4,}}[\"']")),
    ("a password option", re.compile(
        rf"(?i)\bpassw(?:or)?d\s+[\"'](?!{_MASK}[\"'])[^\"']+[\"']")),
    ("a bearer header", re.compile(r"(?i)\bbearer\s+(?!\*+)[A-Za-z0-9._~+/=-]{8,}")),
    ("a known key format", re.compile(
        r"sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{8,}|"
        r"gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY")),
)


class ReplayError(Exception):
    """A pack is incomplete, changed, too large, hostile, or holds something it must not."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(rel: object) -> str:
    """`rel` as a canonical relative path of plain segments, or a ReplayError."""
    if not isinstance(rel, str) or not rel:
        raise ReplayError(f"{rel!r} is not a path")
    parts = rel.split("/")
    if any(not _SEGMENT.fullmatch(p) or p in (".", "..") for p in parts):
        raise ReplayError(f"{rel!r} is not a canonical path inside a run")
    return "/".join(parts)


def _inside(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ReplayError(f"{rel!r} leads outside {base}")
    return target


def credential_in(data: bytes) -> str | None:
    """What kind of credential the bytes appear to hold, or None."""
    text = data.decode("utf-8", "replace")
    for kind, pattern in _SECRET_FORMS:
        if pattern.search(text):
            return kind
    return None


def _wanted(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pat) for pat in RECORD)


def record(run_dir: Path, pack_dir: Path) -> dict[str, Any]:
    """Copy a finished run's readable files into a new pack, with a hashed manifest."""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ReplayError(f"run id {run_id!r} is not a plain identifier")
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
        rel = canonical(rel)
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            left_out.append({"path": rel, "reason": f"larger than {MAX_FILE_BYTES} bytes"})
            continue
        kind = credential_in(data)
        if kind is not None:
            raise ReplayError(f"{rel} holds {kind}; nothing was recorded")
        total += len(data)
        if total > MAX_PACK_BYTES:
            raise ReplayError(f"the pack would pass {MAX_PACK_BYTES} bytes")
        items.append({"path": rel, "sha256": sha256(data), "bytes": len(data), "data": data})
    for item in items:
        dest = _inside(pack_dir / "files", item["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(item.pop("data"))
    manifest = {"format": FORMAT, "source_run_id": run_id,
                "source_created_at": run.get("created_at"),
                "source_harness": run.get("harness_version") or run.get("harness"),
                "recorded_at": _now(), "items": items, "left_out": left_out}
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def load_manifest(pack_dir: Path) -> tuple[dict[str, Any], str]:
    raw = (pack_dir / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT \
            or not isinstance(manifest.get("items"), list):
        raise ReplayError(f"{pack_dir} is not a {FORMAT} pack")
    run_id = manifest.get("source_run_id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ReplayError(f"the pack's run id {run_id!r} is not a plain identifier")
    return manifest, sha256(raw)


def verify(pack_dir: Path) -> dict[str, Any]:
    """Every listed file present with its recorded hash and size, nothing unlisted, no
    path twice, and nothing shaped like a credential."""
    manifest, _ = load_manifest(pack_dir)
    files = pack_dir / "files"
    listed: set[str] = set()
    for item in manifest["items"]:
        if not isinstance(item, dict):
            raise ReplayError("a manifest item is not an object")
        rel = canonical(item.get("path"))
        if rel != item["path"] or rel in listed:
            raise ReplayError(f"{item['path']!r} is not canonical or is listed twice")
        listed.add(rel)
        path = _inside(files, rel)
        if (files / rel).is_symlink() or not path.is_file():
            raise ReplayError(f"{rel} is missing from the pack")
        data = path.read_bytes()
        if len(data) != item.get("bytes") or sha256(data) != item.get("sha256"):
            raise ReplayError(f"{rel} does not match its recorded hash")
        kind = credential_in(data)
        if kind is not None:
            raise ReplayError(f"{rel} holds {kind}")
    for path in files.rglob("*"):
        if path.is_symlink() or (path.is_file()
                                 and path.relative_to(files).as_posix() not in listed):
            raise ReplayError(f"{path.name} is in the pack but not in its manifest")
    return manifest


@dataclass(frozen=True)
class Replay:
    folder: Path
    marker: dict[str, Any]


def replay(pack_dir: Path, runs_dir: Path) -> Replay:
    """A fresh folder inside `runs_dir` holding the pack's files, each checked, and its
    REPLAY.json."""
    manifest = verify(pack_dir)
    _, manifest_sha = load_manifest(pack_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    runs_dir.mkdir(parents=True, exist_ok=True)
    name = f"replay-{manifest['source_run_id']}-{stamp}-{os.urandom(3).hex()}"
    folder = _inside(runs_dir, canonical(name))
    if folder.parent != runs_dir.resolve():
        raise ReplayError(f"{name!r} is not a folder directly inside {runs_dir}")
    folder.mkdir()
    items = []
    for item in manifest["items"]:
        rel = item["path"]
        dest = _inside(folder, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_inside(pack_dir / "files", rel), dest)
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
    """The folder's REPLAY.json, or None when the folder has none."""
    path = run_dir / MARKER
    if not path.exists():
        return None
    marker: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return marker


def normalised(rel: str) -> str:
    """A path a caller wrote (`./a/b`, `a//b`) in canonical form, for lookups only; a
    manifest's own paths must already be canonical."""
    return canonical(str(PurePosixPath(rel)))


def labelled_text(run_dir: Path, rel: str) -> list[tuple[str, str]]:
    """Each line of a run file with its label; never `live` (see the module notes).

    A replayed file whose recorded bytes changed is an error, never relabelled."""
    rel = normalised(rel)
    path = _inside(run_dir, rel)
    data = path.read_bytes() if path.is_file() else b""
    if not data:
        return []
    marker = replay_marker(run_dir)
    if marker is None:
        label = f"recorded sha:{sha256(data)[:12]}"
        return [(label, line) for line in data.decode("utf-8", "replace").splitlines()]
    item = next((i for i in marker["items"] if canonical(i["path"]) == rel), None)
    if item is None:
        return [("appended after replay", line)
                for line in data.decode("utf-8", "replace").splitlines()]
    head, tail = data[: item["bytes"]], data[item["bytes"]:]
    if sha256(head) != item["sha256"]:
        raise ReplayError(f"{rel} changed after it was replayed")
    label = f"replay sha:{item['sha256'][:12]}"
    return ([(label, line) for line in head.decode("utf-8", "replace").splitlines()]
            + [("appended after replay", line)
               for line in tail.decode("utf-8", "replace").splitlines()])
