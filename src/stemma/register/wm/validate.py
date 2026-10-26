"""The validate stage: input shapes, provenance and timestamps.

Exact checks only. A failure raises a typed error naming the field.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .errors import ProvenanceError, ValidationError
from .records import Provenance

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROVENANCE_KEYS = {"kind", "at", "by", "url", "checksum", "runId"}


def iso(value: Any, field: str) -> str:
    """Normalize a timestamp to UTC ISO-8601 with milliseconds and a `Z` suffix
    (sub-millisecond digits are dropped).

    A date alone means midnight UTC. A date-time without a time zone is refused:
    the engine compares timestamps as text, so an ambiguous zone would order
    records wrongly.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if _DATE_ONLY.match(text):
            text += "T00:00:00+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field}: {value!r} is not an ISO-8601 timestamp") from exc
    else:
        raise ValidationError(f"{field}: expected an ISO-8601 string, got {type(value).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError(f"{field}: {value!r} has no time zone")
    dt = dt.astimezone(UTC)
    # Millisecond precision, as JavaScript's toISOString writes it. One fixed width
    # is what makes text order equal time order.
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond // 1000:03d}Z"
    )


def check_keys(
    data: Any, op: str, required: set[str], optional: set[str]
) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValidationError(f"{op}: input must be an object, got {type(data).__name__}")
    unknown = sorted(set(data) - required - optional)
    if unknown:
        raise ValidationError(f"{op}: unknown field(s) {', '.join(unknown)}")
    missing = sorted(k for k in required if k not in data or data[k] is None)
    if missing:
        raise ValidationError(f"{op}: missing field(s) {', '.join(missing)}")
    return data


def text(data: Mapping[str, Any], key: str, op: str, *, colon_free: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{op}: `{key}` must be a non-empty string")
    if colon_free and ":" in value:
        raise ValidationError(f"{op}: `{key}` may not contain ':' (it is part of an id)")
    return value


def optional_text(data: Mapping[str, Any], key: str, op: str) -> str | None:
    if data.get(key) is None:
        return None
    return text(data, key, op)


def provenance(source: Any, op: str) -> Provenance:
    """No provenance, no write."""
    if source is None:
        raise ProvenanceError(f"{op}: no provenance; every write carries `source` (kind, at)")
    if not isinstance(source, Mapping):
        raise ProvenanceError(f"{op}: `source` must be an object with `kind` and `at`")
    unknown = sorted(set(source) - _PROVENANCE_KEYS)
    if unknown:
        raise ValidationError(f"{op}: unknown provenance field(s) {', '.join(unknown)}")
    kind = source.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ProvenanceError(f"{op}: provenance has no `kind`")
    if source.get("at") is None:
        raise ProvenanceError(f"{op}: provenance has no `at`")
    out: Provenance = {"kind": kind, "at": iso(source["at"], f"{op}: source.at")}
    for key in ("by", "url", "checksum", "runId"):
        value = source.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValidationError(f"{op}: source.{key} must be a string")
        out[key] = value
    return out


def json_data(value: Any, field: str) -> Any:
    """A deep copy of `value` as plain JSON data, or a ValidationError."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{field}: non-finite number")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValidationError(f"{field}: object keys must be strings")
            out[k] = json_data(v, f"{field}.{k}")
        return out
    if isinstance(value, (list, tuple)):
        return [json_data(v, f"{field}[{i}]") for i, v in enumerate(value)]
    raise ValidationError(f"{field}: {type(value).__name__} is not JSON data")


def attrs(data: Mapping[str, Any], op: str) -> dict[str, Any]:
    value = data.get("attrs")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError(f"{op}: `attrs` must be an object")
    result: dict[str, Any] = json_data(value, f"{op}: attrs")
    return result
