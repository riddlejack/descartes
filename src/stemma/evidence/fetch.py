"""One fetch: from a requested URL to final bytes or a typed failure.

Each hop is checked by `netpolicy` (spelling, port, every resolved address) and by
an optional `hop_check` the caller supplies (the archive uses it to refuse a
snapshot dated after `as_of`). Limits on hops, bytes and time are recorded on the
result. A body that passes its byte limit is dropped, never stored part-way.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from stemma.evidence import limits
from stemma.evidence.errors import CaptureStatus
from stemma.evidence.netpolicy import (
    InvalidUrl,
    PolicyRefusal,
    Url,
    check_resolved,
    parse_url,
    resolve_redirect,
)
from stemma.evidence.transport import Transport, TransportFailure

DEFAULT_USER_AGENT = "Stemma/0.0.1 (research capture; exact-bytes archive)"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_ASCII_DIGITS = re.compile(r"[0-9]{1,20}")


@dataclass
class Hop:
    url: str
    address: str | None
    http_status: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "address": self.address, "http_status": self.http_status}


@dataclass
class FetchResult:
    """What a fetch produced. `body` is set only when `failure` is None."""

    requested_url: str
    final_url: str | None = None
    hops: list[Hop] = field(default_factory=list)
    http_status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes | None = None
    content_encoding: str | None = None
    wire_sha256: str | None = None
    failure: CaptureStatus | None = None
    detail: str | None = None
    bytes_read: int = 0
    elapsed_s: float = 0.0
    sec_identity_sent: bool = False

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def limits_record(self) -> dict[str, Any]:
        return {
            "max_body_bytes": limits.MAX_BODY_BYTES,
            "max_redirects": limits.MAX_REDIRECTS,
            "read_timeout_s": limits.READ_TIMEOUT_S,
            "total_deadline_s": limits.TOTAL_DEADLINE_S,
            "allowed_ports": sorted(limits.ALLOWED_PORTS),
        }


HopCheck = Callable[[Url], str | None]  # returns a refusal detail, or None to allow


class _Throttle:
    """Minimum spacing between requests to a group of hosts (a resource limit)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, key: str, interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            ready = self._last.get(key, 0.0) + interval
            if ready > now:
                time.sleep(ready - now)
            self._last[key] = time.monotonic()


_THROTTLE = _Throttle()


def _throttle_group(host: str) -> tuple[str, float] | None:
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return "sec", limits.SEC_MIN_INTERVAL_S
    if host == "archive.org" or host.endswith(".archive.org"):
        return "archive", limits.ARCHIVE_MIN_INTERVAL_S
    return None


class Fetcher:
    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        sec_user_agent: str | None = None,
        throttle: bool = True,
    ) -> None:
        self.transport = transport
        self.user_agent = user_agent
        self.sec_user_agent = sec_user_agent
        self.throttle = throttle

    def _headers(self, url: Url) -> dict[str, str]:
        agent = self.user_agent
        if (url.host == "sec.gov" or url.host.endswith(".sec.gov")) and self.sec_user_agent:
            agent = self.sec_user_agent
        return {
            "User-Agent": agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en",
            "Connection": "close",
        }

    def fetch(self, raw_url: str, *, hop_check: HopCheck | None = None) -> FetchResult:
        started = time.monotonic()
        deadline = started + limits.TOTAL_DEADLINE_S
        result = FetchResult(requested_url=raw_url)

        def fail(status: CaptureStatus, detail: str) -> FetchResult:
            result.failure = status
            result.detail = detail
            result.elapsed_s = round(time.monotonic() - started, 3)
            return result

        try:
            url = parse_url(raw_url)
        except InvalidUrl as exc:
            return fail(CaptureStatus.INVALID_URL, str(exc))
        except PolicyRefusal as exc:
            result.hops.append(Hop(raw_url, None, None))
            return fail(CaptureStatus.REFUSED_ADDRESS, f"{exc.reason}: {exc.detail}")

        for hop_index in range(limits.MAX_REDIRECTS + 1):
            hop = Hop(url.url, None, None)
            result.hops.append(hop)
            if hop_check is not None:
                refusal = hop_check(url)
                if refusal is not None:
                    return fail(CaptureStatus.REFUSED_ADDRESS, refusal)
            try:
                addresses = self.transport.resolve(url.host)
                address = check_resolved(url.host, addresses)
            except TransportFailure as exc:
                return fail(CaptureStatus.TRANSPORT_ERROR, f"{exc.code}: {exc.detail}")
            except PolicyRefusal as exc:
                return fail(CaptureStatus.REFUSED_ADDRESS, f"{exc.reason}: {exc.detail}")
            hop.address = address
            if self.throttle:
                group = _throttle_group(url.host)
                if group is not None:
                    _THROTTLE.wait(*group)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return fail(CaptureStatus.TIMEOUT, "total deadline reached before request")
            headers = self._headers(url)
            if self.sec_user_agent and headers["User-Agent"] == self.sec_user_agent:
                result.sec_identity_sent = True
            try:
                resp = self.transport.request(
                    url, address, headers, min(limits.READ_TIMEOUT_S, remaining), deadline
                )
            except TransportFailure as exc:
                status = CaptureStatus.TIMEOUT if exc.code == "timeout" else (
                    CaptureStatus.TRANSPORT_ERROR
                )
                return fail(status, f"{exc.code}: {exc.detail}")
            hop.http_status = resp.status
            result.http_status = resp.status
            result.headers = _bounded_headers(resp.headers)
            if resp.status in _REDIRECTS:
                resp.closer()
                location = resp.header("Location")
                if not location:
                    return fail(CaptureStatus.BAD_REDIRECT, f"{resp.status} without Location")
                if hop_index == limits.MAX_REDIRECTS:
                    return fail(
                        CaptureStatus.TOO_MANY_REDIRECTS,
                        f"more than {limits.MAX_REDIRECTS} redirects",
                    )
                try:
                    url = resolve_redirect(url, location)
                except InvalidUrl as exc:
                    return fail(CaptureStatus.BAD_REDIRECT, str(exc))
                except PolicyRefusal as exc:
                    result.hops.append(Hop(location, None, None))
                    return fail(
                        CaptureStatus.REFUSED_ADDRESS,
                        f"redirect refused, {exc.reason}: {exc.detail}",
                    )
                continue
            result.final_url = url.url
            return self._read_body(resp, result, deadline, started, fail)
        return fail(CaptureStatus.TOO_MANY_REDIRECTS, "redirect loop")  # pragma: no cover

    def _read_body(
        self,
        resp: Any,
        result: FetchResult,
        deadline: float,
        started: float,
        fail: Callable[[CaptureStatus, str], FetchResult],
    ) -> FetchResult:
        declared_text = resp.header("Content-Length")
        declared: int | None = None
        if declared_text is not None:
            # ASCII digits only: str.isdigit() also accepts "²" and other digits.
            if _ASCII_DIGITS.fullmatch(declared_text.strip()):
                declared = int(declared_text.strip())
            else:
                resp.closer()
                return fail(CaptureStatus.TRANSPORT_ERROR,
                            f"unusable Content-Length {declared_text[:40]!r}")
        if declared is not None and declared > limits.MAX_BODY_BYTES:
            resp.closer()
            return fail(
                CaptureStatus.TOO_LARGE,
                f"Content-Length {declared} over limit {limits.MAX_BODY_BYTES}",
            )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in resp.body:
                total += len(chunk)
                result.bytes_read = total
                if total > limits.MAX_BODY_BYTES:
                    return fail(
                        CaptureStatus.TOO_LARGE,
                        f"body passed limit {limits.MAX_BODY_BYTES} bytes; nothing stored",
                    )
                if time.monotonic() > deadline:
                    return fail(CaptureStatus.TIMEOUT, "total deadline reached while reading")
                chunks.append(chunk)
        except TransportFailure as exc:
            status = CaptureStatus.TIMEOUT if exc.code == "timeout" else (
                CaptureStatus.TRANSPORT_ERROR
            )
            return fail(status, f"body: {exc.code}: {exc.detail}")
        finally:
            resp.closer()
        body = b"".join(chunks)
        if declared is not None and len(body) != declared:
            # A connection closed early is a truncated body, never a capture.
            return fail(CaptureStatus.TRANSPORT_ERROR,
                        f"body is {len(body)} bytes but Content-Length said {declared}; "
                        "nothing stored")
        coding = (resp.header("Content-Encoding") or "").strip().lower()
        if coding and coding != "identity":
            result.content_encoding = coding
            result.wire_sha256 = hashlib.sha256(body).hexdigest()
            if coding not in ("gzip", "x-gzip", "deflate"):
                return fail(
                    CaptureStatus.UNSUPPORTED_CONTENT_ENCODING,
                    f"Content-Encoding {coding!r} not decoded",
                )
            decoded = _decode_content(coding, body)
            if decoded is None:
                return fail(CaptureStatus.TRANSPORT_ERROR,
                            f"{coding} body is damaged or truncated; nothing stored")
            if len(decoded) > limits.MAX_DECODED_BYTES:
                return fail(CaptureStatus.TOO_LARGE, "decoded body passed limit")
            body = decoded
        result.body = body
        result.elapsed_s = round(time.monotonic() - started, 3)
        return result


def _decode_content(coding: str, body: bytes) -> bytes | None:
    """Undo gzip or deflate (sent though we asked for identity), bounded in size.
    Every gzip member is decoded (a body may hold several); a stream that ends
    before its end marker, or trailing bytes that are not another member, is None."""
    cap = limits.MAX_DECODED_BYTES + 1
    if coding in ("gzip", "x-gzip"):
        out = bytearray()
        rest = body
        while rest:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                out += decoder.decompress(rest, cap - len(out))
            except zlib.error:
                return None
            if len(out) >= cap:
                return bytes(out)  # the caller reports the size limit
            if not decoder.eof:
                return None  # truncated member
            rest = decoder.unused_data
            if rest and not rest.startswith(b"\x1f\x8b"):
                return None  # trailing bytes that are not a gzip member
        return bytes(out)
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):  # zlib-wrapped, then raw deflate
        decoder = zlib.decompressobj(wbits)
        try:
            data = decoder.decompress(body, cap)
        except zlib.error:
            continue
        if len(data) >= cap or (decoder.eof and not decoder.unused_data):
            return data
        return None
    return None


def _bounded_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    kept: list[tuple[str, str]] = []
    size = 0
    for key, value in headers:
        size += len(key) + len(value)
        if size > limits.MAX_RECORDED_HEADER_BYTES:
            kept.append(("X-Stemma-Headers-Truncated", "true"))
            break
        kept.append((key, value))
    return kept
