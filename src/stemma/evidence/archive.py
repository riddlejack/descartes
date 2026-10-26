"""`as_of` captures from the Internet Archive's Wayback Machine.

Lookup: the CDX API lists captures of the URL up to the day before `as_of`, newest
last, keeping only those the archive recorded as HTTP 200. The newest is fetched in
`id_` mode (`/web/<timestamp>id_/<url>`), which returns the archived bytes as the
site served them, without the archive's toolbar or link rewriting.

Every hop of that fetch must be a Wayback URL whose timestamp is before `as_of`:
Wayback redirects to the nearest capture it has, which can be later than asked. A
hop anywhere else, or dated on or after `as_of`, ends the attempt. The final
response's `Memento-Datetime` header, when present, must also be before `as_of`.

"Before `as_of`" means strictly before 00:00:00 UTC on the `as_of` date.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from stemma.evidence import limits
from stemma.evidence.errors import CaptureStatus
from stemma.evidence.fetch import Fetcher, FetchResult
from stemma.evidence.netpolicy import Url

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
_WAYBACK_HOP = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})(?:[a-z]{2}_)?/")
# Resource limit: captures listed by one CDX lookup (the newest few before the date).
_CDX_ROWS = 5


@dataclass
class Snapshot:
    timestamp: str
    original: str


@dataclass
class ArchiveOutcome:
    fetch: FetchResult | None
    status: CaptureStatus | None  # set on failure
    detail: str | None
    record: dict[str, Any]


def as_of_cutoff(as_of: str) -> datetime:
    day = date.fromisoformat(as_of)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _timestamp_time(stamp: str) -> datetime:
    padded = (stamp + "00000101000000"[len(stamp):]) if len(stamp) < 14 else stamp[:14]
    return datetime.strptime(padded, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def cdx_query_url(url: str, as_of: str) -> str:
    last_day = (date.fromisoformat(as_of) - timedelta(days=1)).strftime("%Y%m%d")
    return (f"{CDX_ENDPOINT}?url={quote(url, safe='')}&to={last_day}&output=json"
            f"&fl=timestamp,original,statuscode,mimetype,digest"
            f"&filter=statuscode:200&limit=-{_CDX_ROWS}")


def snapshot_url(snap: Snapshot) -> str:
    return f"https://web.archive.org/web/{snap.timestamp}id_/{snap.original}"


class Archive:
    def __init__(self, fetcher: Fetcher, *, sleep: Any = time.sleep) -> None:
        self.fetcher = fetcher
        self.sleep = sleep

    def _lookup(self, url: str, as_of: str, record: dict[str, Any]) -> tuple[
        Snapshot | None, CaptureStatus | None, str | None
    ]:
        query = cdx_query_url(url, as_of)
        record["lookup_url"] = query
        last_detail = ""
        for attempt in range(limits.ARCHIVE_ATTEMPTS):
            result = self.fetcher.fetch(query)
            record.setdefault("lookup_attempts", []).append(
                {"http_status": result.http_status, "failure": result.failure,
                 "detail": result.detail}
            )
            if result.failure is None and result.http_status == 200:
                break
            last_detail = result.detail or f"HTTP {result.http_status}"
            retryable = result.http_status in (429, 502, 503, 504) or result.failure in (
                CaptureStatus.TIMEOUT, CaptureStatus.TRANSPORT_ERROR)
            if not retryable or attempt == limits.ARCHIVE_ATTEMPTS - 1:
                return None, CaptureStatus.ARCHIVE_UNAVAILABLE, f"CDX lookup failed: {last_detail}"
            self.sleep(2.0 * (attempt + 1))
        assert result.body is not None
        text = result.body.decode("utf-8", "replace").strip()
        if not text:
            return None, CaptureStatus.NO_SNAPSHOT_BEFORE_AS_OF, "CDX lists no capture"
        try:
            rows = json.loads(text)
        except ValueError:
            return None, CaptureStatus.ARCHIVE_UNAVAILABLE, "CDX answer was not JSON"
        if not isinstance(rows, list) or len(rows) < 2:
            return None, CaptureStatus.NO_SNAPSHOT_BEFORE_AS_OF, "CDX lists no capture"
        cutoff = as_of_cutoff(as_of)
        candidates: list[Snapshot] = []
        for row in rows[1:]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            stamp, original = str(row[0]), str(row[1])
            if not re.fullmatch(r"\d{14}", stamp):
                continue
            if _timestamp_time(stamp) < cutoff:
                candidates.append(Snapshot(stamp, original))
        if not candidates:
            return None, CaptureStatus.NO_SNAPSHOT_BEFORE_AS_OF, "no listed capture is before as_of"
        candidates.sort(key=lambda snap: snap.timestamp)
        record["candidates"] = [c.timestamp for c in candidates]
        return candidates[-1], None, None

    def capture(self, url: str, as_of: str) -> ArchiveOutcome:
        record: dict[str, Any] = {"service": "internet_archive_wayback", "as_of": as_of,
                                  "cutoff_utc": as_of_cutoff(as_of).isoformat()}
        snap, status, detail = self._lookup(url, as_of, record)
        if snap is None:
            return ArchiveOutcome(None, status, detail, record)
        record["snapshot_timestamp"] = snap.timestamp
        record["snapshot_url"] = snapshot_url(snap)
        cutoff = as_of_cutoff(as_of)

        def hop_check(hop: Url) -> str | None:
            match = _WAYBACK_HOP.match(hop.url)
            if match is None:
                return f"archive redirect left the archive: {hop.url}"
            if _timestamp_time(match.group(1)) >= cutoff:
                return f"archive redirected to a capture dated {match.group(1)}, not before as_of"
            return None

        fetched = self.fetcher.fetch(snapshot_url(snap), hop_check=hop_check)
        if fetched.failure is CaptureStatus.REFUSED_ADDRESS and fetched.detail and (
            "archive redirect" in fetched.detail
        ):
            return ArchiveOutcome(fetched, CaptureStatus.NO_SNAPSHOT_BEFORE_AS_OF,
                                  fetched.detail, record)
        if fetched.failure is None:
            memento = fetched.header("Memento-Datetime")
            if memento:
                record["memento_datetime"] = memento
                try:
                    when = parsedate_to_datetime(memento)
                except (TypeError, ValueError):
                    when = None
                if when is None or when >= cutoff:
                    return ArchiveOutcome(
                        fetched, CaptureStatus.NO_SNAPSHOT_BEFORE_AS_OF,
                        f"archived copy is dated {memento}, not before as_of", record)
            final = _WAYBACK_HOP.match(fetched.final_url or "")
            if final is not None:
                record["served_timestamp"] = final.group(1)
        return ArchiveOutcome(fetched, None, None, record)
