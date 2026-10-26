"""The fetcher: redirects re-checked per hop, limits enforced and recorded."""

from __future__ import annotations

import gzip

import pytest
from conftest import FakeTransport, Reply

from stemma.evidence import limits
from stemma.evidence.errors import CaptureStatus
from stemma.evidence.fetch import Fetcher
from stemma.evidence.transport import TransportFailure


def fetcher(transport: FakeTransport) -> Fetcher:
    return Fetcher(transport, throttle=False)


def test_redirect_to_private_address_refused() -> None:
    t = FakeTransport({"https://example.org/": Reply(302, [("Location", "http://127.0.0.1/admin")])})
    result = fetcher(t).fetch("https://example.org/")
    assert result.failure is CaptureStatus.REFUSED_ADDRESS
    assert "redirect refused" in (result.detail or "")
    assert [c[0] for c in t.calls] == ["https://example.org/"]  # loopback never contacted


def test_redirect_to_name_resolving_private_refused() -> None:
    t = FakeTransport(
        {"https://example.org/": Reply(301, [("Location", "https://inside.example.net/x")])},
        dns={"inside.example.net": ["192.168.0.10"]},
    )
    result = fetcher(t).fetch("https://example.org/")
    assert result.failure is CaptureStatus.REFUSED_ADDRESS
    assert len(t.calls) == 1


def test_connection_goes_to_checked_address() -> None:
    t = FakeTransport({"https://example.org/": Reply(200, [], b"hello")},
                      dns={"example.org": ["93.184.216.34"]})
    result = fetcher(t).fetch("https://example.org/")
    assert result.body == b"hello"
    assert t.calls[0][1] == "93.184.216.34"
    assert result.hops[0].address == "93.184.216.34"


def test_redirects_followed_and_recorded() -> None:
    t = FakeTransport({
        "https://example.org/a": Reply(301, [("Location", "/b")]),
        "https://example.org/b": Reply(302, [("Location", "https://example.com/c")]),
        "https://example.com/c": Reply(200, [("Content-Type", "text/plain")], b"done"),
    })
    result = fetcher(t).fetch("https://example.org/a")
    assert result.failure is None and result.body == b"done"
    assert [h.url for h in result.hops] == [
        "https://example.org/a", "https://example.org/b", "https://example.com/c"]
    assert result.final_url == "https://example.com/c"


def test_too_many_redirects() -> None:
    t = FakeTransport({"https://example.org/loop": Reply(302, [("Location", "/loop")])})
    result = fetcher(t).fetch("https://example.org/loop")
    assert result.failure is CaptureStatus.TOO_MANY_REDIRECTS
    assert len(t.calls) == limits.MAX_REDIRECTS + 1


def test_oversized_declared_body_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_BODY_BYTES", 1000)
    t = FakeTransport({"https://example.org/big": Reply(200, [("Content-Length", "5000")], b"x" * 5000)})
    result = fetcher(t).fetch("https://example.org/big")
    assert result.failure is CaptureStatus.TOO_LARGE and result.body is None


def test_oversized_streamed_body_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # The server lies about (or omits) its length; the cap still holds mid-stream.
    monkeypatch.setattr(limits, "MAX_BODY_BYTES", 1000)
    t = FakeTransport({"https://example.org/big": Reply(200, [], b"x" * 5000, chunk=300)})
    result = fetcher(t).fetch("https://example.org/big")
    assert result.failure is CaptureStatus.TOO_LARGE and result.body is None
    assert result.bytes_read <= 1000 + 300


def test_gzip_bomb_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_DECODED_BYTES", 10_000)
    bomb = gzip.compress(b"\0" * 1_000_000)
    t = FakeTransport({"https://example.org/z": Reply(200, [("Content-Encoding", "gzip")], bomb)})
    result = fetcher(t).fetch("https://example.org/z")
    assert result.failure is CaptureStatus.TOO_LARGE


def test_unrequested_gzip_decoded_and_recorded() -> None:
    t = FakeTransport({"https://example.org/z": Reply(
        200, [("Content-Encoding", "gzip")], gzip.compress(b"plain text"))})
    result = fetcher(t).fetch("https://example.org/z")
    assert result.body == b"plain text"
    assert result.content_encoding == "gzip" and result.wire_sha256


def test_unknown_coding_refused() -> None:
    t = FakeTransport({"https://example.org/z": Reply(200, [("Content-Encoding", "br")], b"???")})
    assert fetcher(t).fetch("https://example.org/z").failure is (
        CaptureStatus.UNSUPPORTED_CONTENT_ENCODING)


def test_transport_failures_typed() -> None:
    t = FakeTransport()
    t.routes["https://example.org/t"] = [TransportFailure("timeout", "slow")]
    assert fetcher(t).fetch("https://example.org/t").failure is CaptureStatus.TIMEOUT
    t.routes["https://example.org/c"] = [TransportFailure("connect_failed", "refused")]
    assert fetcher(t).fetch("https://example.org/c").failure is CaptureStatus.TRANSPORT_ERROR
    t.routes["https://example.org/m"] = Reply(200, [], b"x" * 200_000, chunk=1000, fail_after=5000)
    assert fetcher(t).fetch("https://example.org/m").failure is CaptureStatus.TIMEOUT


def test_redirect_without_location() -> None:
    t = FakeTransport({"https://example.org/": Reply(302, [], b"")})
    assert fetcher(t).fetch("https://example.org/").failure is CaptureStatus.BAD_REDIRECT


def test_sec_hosts_get_identity_user_agent() -> None:
    t = FakeTransport({"https://www.sec.gov/x": Reply(200, [], b"ok"),
                       "https://example.org/": Reply(200, [], b"ok")})
    f = Fetcher(t, sec_user_agent="Test Builder test@example.org", throttle=False)
    f.fetch("https://www.sec.gov/x")
    f.fetch("https://example.org/")
    assert t.calls[0][2]["User-Agent"] == "Test Builder test@example.org"
    assert "test@example.org" not in t.calls[1][2]["User-Agent"]
    assert t.calls[0][2]["Accept-Encoding"] == "identity"
