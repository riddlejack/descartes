"""The run's evidence store: `store/bytes/`, `store/text/`, `store/receipts/`,
`store/index.sqlite` (contract section 4).

Bytes and text are written once, under the sha256 of the bytes, and made read-only.
A second writer of the same bytes finds the file there and checks it holds the same
bytes; a mismatch is a typed `store_corrupt` error, never an overwrite. Receipts are
written once under a fresh id. The index is one SQLite file in WAL mode; a document
is indexed by whichever writer inserts its row first, inside one transaction, so
concurrent writers never index it twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import EvidenceError
from stemma.evidence.quotes import normalise

# Invariant: the spellings of the two kinds of id the store accepts from callers.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_ID_RE = re.compile(r"^r_[0-9]{8}T[0-9]{6}_[0-9a-f]{12}$")
# A stored text's identity: the bytes' sha256, plus "~<variant>" when the same bytes
# were also read under another content type or charset (see service._variant).
TEXT_ID_RE = re.compile(r"^[0-9a-f]{64}(?:~[0-9a-f]{12})?$")

# Resource limit: seconds a writer waits for the SQLite lock.
_SQLITE_TIMEOUT_S = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    sha256 TEXT PRIMARY KEY,
    text_chars INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    first_receipt_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    sha256 TEXT,
    status TEXT NOT NULL,
    url TEXT,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_words USING fts5(
    content, sha256 UNINDEXED, start UNINDEXED, end UNINDEXED,
    tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_exact USING fts5(
    content, sha256 UNINDEXED, tokenize = 'trigram'
);
"""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_receipt_id() -> str:
    return f"r_{datetime.now(UTC):%Y%m%dT%H%M%S}_{secrets.token_hex(6)}"


def check_sha256(value: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise EvidenceError("bad_sha256", "expected 64 lower-case hex characters")
    return value


def check_text_id(value: str) -> str:
    if not isinstance(value, str) or not TEXT_ID_RE.fullmatch(value):
        raise EvidenceError("bad_sha256", "expected a sha256 (64 lower-case hex characters), "
                            "optionally followed by ~ and a 12-character text variant")
    return value


def check_receipt_id(value: str) -> str:
    if not isinstance(value, str) or not RECEIPT_ID_RE.fullmatch(value):
        raise EvidenceError("bad_receipt_id", "not a receipt id this store issues")
    return value


def _write_once(path: Path, data: bytes) -> bool:
    """Create `path` holding `data`, atomically and exclusively. Returns False if it
    already existed with the same content; raises `store_corrupt` if different."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o444)
        try:
            os.link(tmp, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != data:
                raise EvidenceError(
                    "store_corrupt", f"{path.name} exists with different content"
                ) from None
            return False
        return True
    finally:
        os.unlink(tmp)


def chunk_for_words(text: str) -> list[tuple[int, int]]:
    """Split text into ranges of about `WORDS_CHUNK_CHARS`, breaking at blank lines
    where possible. Ranges cover the whole text with no gaps."""
    size = limits.WORDS_CHUNK_CHARS
    ranges: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            cut = text.rfind("\n\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        ranges.append((start, end))
        start = end
    return ranges


class Store:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "store"
        self.bytes_dir = self.root / "bytes"
        self.text_dir = self.root / "text"
        self.receipts_dir = self.root / "receipts"
        self.derived_dir = self.root / "derived"
        self.index_path = self.root / "index.sqlite"
        for folder in (self.bytes_dir, self.text_dir, self.receipts_dir, self.derived_dir):
            folder.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.index_path, timeout=_SQLITE_TIMEOUT_S, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=60000")
            yield db
        finally:
            db.close()

    # Bytes and text -----------------------------------------------------------

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        _write_once(self.bytes_dir / sha, data)
        return sha

    def bytes_path(self, sha: str) -> Path:
        return self.bytes_dir / check_sha256(sha)

    def get_bytes(self, sha: str) -> bytes:
        path = self.bytes_path(sha)
        if not path.exists():
            raise EvidenceError("unknown_sha256", f"no stored bytes {sha}")
        return path.read_bytes()

    def text_rel_path(self, sha: str) -> str:
        return f"store/text/{sha}.txt"

    def put_text(self, sha: str, text: str, outline: list[dict[str, Any]],
                 record: dict[str, Any]) -> None:
        """Write the text, its outline and the record of how it was extracted. The
        record goes last, so its presence means the other two are complete."""
        _write_once(self.text_dir / f"{sha}.txt", text.encode("utf-8"))
        _write_once(
            self.text_dir / f"{sha}.outline.json",
            json.dumps(outline, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        _write_once(
            self.text_dir / f"{sha}.extraction.json",
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    def get_extraction(self, sha: str) -> dict[str, Any] | None:
        """How the stored text of these bytes was made, or None if never extracted."""
        path = self.text_dir / f"{check_text_id(sha)}.extraction.json"
        if not path.exists():
            return None
        loaded: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return loaded

    def has_text(self, sha: str) -> bool:
        return (self.text_dir / f"{check_text_id(sha)}.txt").exists()

    def get_text(self, sha: str) -> str:
        path = self.text_dir / f"{check_text_id(sha)}.txt"
        if not path.exists():
            raise EvidenceError("no_text", f"no stored text for {sha}")
        return _read_text_cached(str(path), path.stat().st_mtime_ns)

    def get_outline(self, sha: str) -> list[dict[str, Any]]:
        path = self.text_dir / f"{check_text_id(sha)}.outline.json"
        if not path.exists():
            raise EvidenceError("no_text", f"no stored text for {sha}")
        loaded: list[dict[str, Any]] = json.loads(path.read_text("utf-8"))
        return loaded

    # Receipts -----------------------------------------------------------------

    def put_receipt(self, receipt: dict[str, Any]) -> None:
        receipt_id = check_receipt_id(receipt["receipt_id"])
        try:
            data = json.dumps(receipt, ensure_ascii=False, indent=1, sort_keys=True).encode()
        except UnicodeEncodeError:  # a lone surrogate from agent input: escape it
            data = json.dumps(receipt, ensure_ascii=True, indent=1, sort_keys=True).encode()
        if not _write_once(self.receipts_dir / f"{receipt_id}.json", data):
            raise EvidenceError("store_corrupt", f"receipt {receipt_id} written twice")
        with self._db() as db:
            db.execute(
                "INSERT INTO receipts(receipt_id, sha256, status, url, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (receipt_id, receipt.get("sha256"), receipt["status"],
                 receipt.get("final_url") or receipt.get("requested_url"),
                 receipt["created_at"]),
            )

    def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        path = self.receipts_dir / f"{check_receipt_id(receipt_id)}.json"
        if not path.exists():
            raise EvidenceError("unknown_receipt", f"no receipt {receipt_id} in this run")
        loaded: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return loaded

    def first_receipt_for(self, sha: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT first_receipt_id FROM docs WHERE sha256 = ?",
                             (sha,)).fetchone()
        return str(row[0]) if row else None

    # Index --------------------------------------------------------------------

    def index_document(self, sha: str, text: str, content_type: str, receipt_id: str) -> bool:
        """Index once per document. Returns True if this call did the indexing."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO docs(sha256, text_chars, content_type, "
                    "first_receipt_id) VALUES (?, ?, ?, ?)",
                    (sha, len(text), content_type, receipt_id),
                )
                inserted = cursor.rowcount == 1
                if inserted:
                    db.executemany(
                        "INSERT INTO fts_words(content, sha256, start, end) VALUES (?, ?, ?, ?)",
                        [(text[a:b], sha, a, b) for a, b in chunk_for_words(text)],
                    )
                    db.execute("INSERT INTO fts_exact(content, sha256) VALUES (?, ?)",
                               (normalise(text)[0], sha))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return inserted

    def search_words(self, query: str, sha: str | None, limit: int) -> list[dict[str, Any]]:
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            raise EvidenceError("bad_query", "a words query needs at least one word")
        match = " ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        sql = ("SELECT sha256, start, end, snippet(fts_words, 0, '[', ']', '…', 24), "
               "bm25(fts_words) FROM fts_words WHERE fts_words MATCH ?")
        params: list[Any] = [match]
        if sha is not None and "~" in sha:
            sql += " AND sha256 = ?"
            params.append(sha)
        elif sha is not None:  # a bytes sha256 covers every text read from those bytes
            sql += " AND (sha256 = ? OR sha256 LIKE ?)"
            params.extend([sha, sha + "~%"])
        sql += " ORDER BY bm25(fts_words) LIMIT ?"
        params.append(limit)
        with self._db() as db:
            rows = db.execute(sql, params).fetchall()
        return [{"sha256": str(r[0]).split("~")[0], "text_id": r[0], "start": int(r[1]),
                 "end": int(r[2]), "snippet": r[3],
                 "rank": round(float(r[4]), 4)} for r in rows]

    def search_exact(self, query: str, sha: str | None, limit: int) -> list[dict[str, Any]]:
        if len(query) < limits.EXACT_QUERY_MIN_CHARS:
            raise EvidenceError(
                "query_too_short",
                f"exact search needs at least {limits.EXACT_QUERY_MIN_CHARS} characters",
            )
        needle = normalise(query)[0].strip(" ")
        if len(needle) < limits.EXACT_QUERY_MIN_CHARS:
            raise EvidenceError(
                "query_too_short",
                f"exact search needs at least {limits.EXACT_QUERY_MIN_CHARS} characters",
            )
        match = '"' + needle.replace('"', '""') + '"'
        sql = "SELECT sha256 FROM fts_exact WHERE fts_exact MATCH ?"
        params: list[Any] = [match]
        if sha is not None and "~" in sha:
            sql += " AND sha256 = ?"
            params.append(sha)
        elif sha is not None:  # a bytes sha256 covers every text read from those bytes
            sql += " AND (sha256 = ? OR sha256 LIKE ?)"
            params.extend([sha, sha + "~%"])
        with self._db() as db:
            shas = [str(r[0]) for r in db.execute(sql, params).fetchall()]
        hits: list[dict[str, Any]] = []
        context = limits.SNIPPET_CONTEXT_CHARS
        for doc in sorted(shas):
            text = self.get_text(doc)
            haystack, origin = normalise(text)
            position = haystack.find(needle)
            while position != -1 and len(hits) < limit:
                start = origin[position]
                end = origin[position + len(needle) - 1] + 1
                snippet = (text[max(0, start - context):start] + "[" + text[start:end] + "]"
                           + text[end:end + context])
                hits.append({"sha256": doc.split("~")[0], "text_id": doc, "start": start,
                             "end": end, "snippet": snippet})
                position = haystack.find(needle, position + len(needle))
            if len(hits) >= limit:
                break
        return hits


@lru_cache(maxsize=32)
def _read_text_cached(path: str, mtime_ns: int) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()
