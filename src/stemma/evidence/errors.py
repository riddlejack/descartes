"""Typed outcomes for the evidence service.

Every capture attempt ends in exactly one `CaptureStatus`, written on its receipt.
Only `ok` is a clean success. `short_text` and `empty_text` are loud: the bytes are
kept and any text is kept, but the receipt says the extraction looks wrong. Every
other value is a failure that names what happened and evidences nothing about the
source (a timeout is not a missing page).

Calls that do not capture (reading, searching, quote checks) raise `EvidenceError`
with a typed `code` when they cannot answer. They never return an invented result.
"""

from __future__ import annotations

from enum import StrEnum


class CaptureStatus(StrEnum):
    """The terminal state of one capture attempt. Every value has a producer."""

    # Bytes stored and text extracted with no sanity flag raised.
    OK = "ok"
    # Bytes stored; text extracted but implausibly short for the bytes. Kept whole.
    SHORT_TEXT = "short_text"
    # Bytes stored; the extractor produced no text at all.
    EMPTY_TEXT = "empty_text"

    # The address was refused before any request: scheme, userinfo, port, a
    # numeric spelling we will not guess at, or an address that is not public.
    REFUSED_ADDRESS = "refused_address"
    # The string is not a URL we can parse.
    INVALID_URL = "invalid_url"
    # The redirect chain reached its recorded hop limit.
    TOO_MANY_REDIRECTS = "too_many_redirects"
    # A redirect without a usable Location.
    BAD_REDIRECT = "bad_redirect"
    # The body passed its recorded byte limit. Nothing is stored: a truncated body
    # is never a capture.
    TOO_LARGE = "too_large"
    # The recorded time limit was reached.
    TIMEOUT = "timeout"
    # No response: DNS, connection or TLS failure, or a broken exchange.
    TRANSPORT_ERROR = "transport_error"

    # HTTP said the resource is not there (404, 410).
    NOT_FOUND = "not_found"
    # HTTP asked for credentials (401) or refused (403).
    FORBIDDEN = "forbidden"
    # HTTP said too many requests (429).
    RATE_LIMITED = "rate_limited"
    # Any other non-2xx final response.
    HTTP_ERROR = "http_error"

    # Bytes stored, but no extractor for their type (images, archives, Office files).
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    # Bytes stored, but their declared charset is not on the allowlist.
    UNSUPPORTED_CHARSET = "unsupported_charset"
    # Bytes arrived with a content coding we will not decode.
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    # Bytes stored, but the extractor raised (a damaged or encrypted PDF).
    EXTRACTION_FAILED = "extraction_failed"
    # Bytes stored; text deliberately not extracted because it would show
    # information dated after the run's `as_of` date.
    TEXT_WITHHELD_AS_OF = "text_withheld_as_of"

    # `as_of` mode: no archived copy dated before the run's `as_of` date was found.
    NO_SNAPSHOT_BEFORE_AS_OF = "no_snapshot_before_as_of"
    # `as_of` mode: the archive itself could not be queried (down, rate limited).
    ARCHIVE_UNAVAILABLE = "archive_unavailable"
    # `as_of` mode: a SEC filing dated on or after the run's `as_of` date.
    FILED_AFTER_AS_OF = "filed_after_as_of"
    # `as_of` mode: no filing with that accession filed before the run's `as_of` date,
    # whether a later one exists or none does (one answer, so neither is revealed).
    NO_FILING_BEFORE_AS_OF = "no_filing_before_as_of"
    # `as_of` mode: a market series came back with a row dated on or after the run's
    # `as_of` date although the host asked it to end before; nothing was kept.
    SERIES_PAST_AS_OF = "series_past_as_of"
    # `market_series`: the answer is not a dated series (no rows, a message instead of
    # data, a first column that is not a date); nothing was kept.
    SERIES_UNREADABLE = "series_unreadable"

    # `capture_file`: the path is outside the run's `task/` folder.
    PATH_OUTSIDE_TASK = "path_outside_task"
    # `capture_file`: no such file.
    FILE_NOT_FOUND = "file_not_found"
    # `capture_file`: the file exists but cannot be opened or read.
    FILE_UNREADABLE = "file_unreadable"

    # A fault in this service, recorded rather than raised so the attempt keeps its
    # receipt. Evidences nothing.
    INTERNAL_ERROR = "internal_error"


# Invariant: the statuses whose receipt carries stored text that may be read,
# searched and quoted.
HAS_TEXT: frozenset[CaptureStatus] = frozenset({CaptureStatus.OK, CaptureStatus.SHORT_TEXT})


class EvidenceError(Exception):
    """A typed failure of a non-capture call. `code` is stable; `message` says what."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}
