"""Stable, content-derived ids (World Model spec, docs/stable-ids.md).

Same input, same id, in every process and on every re-run. Nothing here reads a
clock or a counter.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from typing import Any

# Logical invariant: the spec fixes the name-keyed id to the first 16 hex digits.
_ID_HEX = 16

# Zero-width characters carry no name content and NFKD leaves them in place.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))


def sha256Hex(text: str) -> str:
    """Hex sha-256 of the UTF-8 bytes of `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalizeName(name: str) -> str:
    """NFKD, strip combining marks, lowercase, collapse whitespace, trim.

    NFKD turns a no-break space into a plain space and splits accents off their
    letters; the accents (Unicode category Mn) are then dropped. Nothing else is
    removed: "Acme Corp" and "Acme Corporation" stay different.
    """
    if not isinstance(name, str):
        raise TypeError(f"normalizeName needs a string, got {type(name).__name__}")
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.translate(_ZERO_WIDTH).lower().split())


def entityIdFromName(type: str, name: str) -> str:
    """Name-keyed entity id: `ent_` + sha256(`type:normalizeName(name)`)[:16]."""
    return "ent_" + sha256Hex(f"{type}:{normalizeName(name)}")[:_ID_HEX]


def externalId(type: str, system: str, external_id: str) -> str:
    """Externally keyed id: the literal `type:system:externalId` grammar."""
    return f"{type}:{system}:{external_id}"


def contextIdFromAttrs(
    type: str,
    attrs: dict[str, Any],
    parent: str | None,
) -> str:
    """Attr-keyed context id; the parent takes part, so the same attrs under two
    parents are two contexts."""
    return "ctx_" + sha256Hex(f"{type}:{canonicalJson(attrs)}:{parent or ''}")[:_ID_HEX]


def relationshipId(
    from_id: str,
    type: str,
    to_id: str,
    context: str | None,
    valid_from: str | None,
) -> str:
    """hash(from + type + to + context? + validFrom?): relating the same edge twice is a no-op."""
    key = f"{from_id}|{type}|{to_id}|{context or ''}|{valid_from or ''}"
    return "rel_" + sha256Hex(key)[:_ID_HEX]


def factId(
    about: str,
    predicate: str,
    value: Any,
    asserted_at: str,
    source_kind: str,
    source_url: str | None,
) -> str:
    """Content-addressed fact id: asserting the identical fact twice is a no-op."""
    key = f"{about}|{predicate}|{canonicalJson(value)}|{asserted_at}|{source_kind}|{source_url or ''}"
    return "fact_" + sha256Hex(key)[:_ID_HEX]


def proposalId(left: str, right: str) -> str:
    """Merge proposal id over the unordered pair, so a pair is proposed once."""
    a, b = sorted((left, right))
    return "merge_" + sha256Hex(f"{a}|{b}")[:_ID_HEX]


def canonicalJson(value: Any) -> str:
    """Deterministic JSON: keys sorted, no whitespace, integral floats written as integers
    (as JavaScript writes them), non-finite numbers refused."""
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonicalJson: non-finite number")
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"canonicalJson: non-string key {k!r}")
            out[k] = _canonical(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    raise TypeError(f"canonicalJson: {type(value).__name__} is not JSON data")
