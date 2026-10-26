"""The request-keyed fetch cache in `$STEMMA_CACHE` (contract section 4).

A cache entry is keyed by the exact request: the kind of fetch, the URL as the
agent asked for it, the run's `as_of` date, and the extractor versions. A run gets
cached bytes only when it makes that same request itself, so no run can see what
another run discovered. Only successful responses are cached; a failure is always
tried again. On a hit the bytes are checked against their recorded sha256 and a
mismatch is ignored with a warning (the fetch is made afresh), as is an entry
filed under a key it does not name. Code cannot stop a writer who rewrites both
files of an entry consistently: `$STEMMA_CACHE` must not be writable by agents.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def request_key(kind: str, url: str, as_of: str | None, versions: dict[str, str]) -> str:
    material = json.dumps(
        {"kind": kind, "url": url, "as_of": as_of, "extractors": versions},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class RequestCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _paths(self, key: str) -> tuple[Path, Path]:
        folder = self.root / "requests" / key[:2]
        return folder / f"{key}.json", folder / f"{key}.bytes"

    def get(self, key: str) -> tuple[dict[str, Any], bytes] | None:
        meta_path, bytes_path = self._paths(key)
        if not meta_path.exists() or not bytes_path.exists():
            return None
        try:
            meta: dict[str, Any] = json.loads(meta_path.read_text("utf-8"))
            body = bytes_path.read_bytes()
        except (OSError, ValueError):
            return None
        if hashlib.sha256(body).hexdigest() != meta.get("sha256") or meta.get("key") != key:
            return {"_corrupt": True}, b""
        return meta, body

    def put(self, key: str, meta: dict[str, Any], body: bytes) -> None:
        meta_path, bytes_path = self._paths(key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(meta, sha256=hashlib.sha256(body).hexdigest(), key=key)
        for path, data in ((bytes_path, body), (meta_path, json.dumps(meta).encode("utf-8"))):
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
