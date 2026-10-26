"""The transport seam: one request to one checked address, no redirects followed.

The fetcher decides where to connect; a transport only connects to the address it
is handed. Tests replace `StdlibTransport` with a fake, so unit tests never touch
the network.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from stemma.evidence.netpolicy import Url


class TransportFailure(Exception):
    """No usable response. `code` is `dns_failed`, `connect_failed`, `tls_failed`,
    `timeout` or `protocol_error`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class RawResponse:
    """One response with its body still unread. Headers keep order and repeats."""

    status: int
    headers: list[tuple[str, str]]
    body: Iterator[bytes]
    closer: Callable[[], None]

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None


class Transport(Protocol):
    def resolve(self, host: str) -> list[str]:
        """Every address `host` answers with."""

    def request(
        self, url: Url, address: str, headers: dict[str, str], timeout: float
    ) -> RawResponse:
        """Send GET for `url` to `address`; raise `TransportFailure` on no response."""


def _tls_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi is pinned in the evidence extra
        return ssl.create_default_context()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        context = _tls_context()
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class StdlibTransport:
    """The real transport: Python's `http.client`, connecting to the checked address,
    with TLS verified against the URL's host name. No proxy, no cookies, no cache."""

    def resolve(self, host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise TransportFailure("dns_failed", f"{host}: {exc}") from exc
        seen: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if address not in seen:
                seen.append(address)
        return seen

    def request(
        self, url: Url, address: str, headers: dict[str, str], timeout: float
    ) -> RawResponse:
        conn: http.client.HTTPConnection
        if url.is_https:
            conn = _PinnedHTTPSConnection(url.host, url.port, address, timeout)
        else:
            conn = _PinnedHTTPConnection(url.host, url.port, address, timeout)
        try:
            conn.putrequest("GET", url.path_and_query, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", url.host_header)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()
            resp = conn.getresponse()
        except ssl.SSLError as exc:
            conn.close()
            raise TransportFailure("tls_failed", str(exc)) from exc
        except TimeoutError as exc:
            conn.close()
            raise TransportFailure("timeout", str(exc)) from exc
        except (ConnectionError, OSError) as exc:
            conn.close()
            raise TransportFailure("connect_failed", str(exc)) from exc
        except http.client.HTTPException as exc:
            conn.close()
            raise TransportFailure("protocol_error", str(exc)) from exc

        def body() -> Iterator[bytes]:
            try:
                while True:
                    try:
                        chunk = resp.read(65536)
                    except TimeoutError as exc:
                        raise TransportFailure("timeout", str(exc)) from exc
                    except (http.client.HTTPException, OSError) as exc:
                        raise TransportFailure("protocol_error", str(exc)) from exc
                    if not chunk:
                        return
                    yield chunk
            finally:
                conn.close()

        return RawResponse(resp.status, list(resp.getheaders()), body(), conn.close)
