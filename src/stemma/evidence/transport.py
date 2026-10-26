"""The transport seam: one request to one checked address, no redirects followed.

The fetcher decides where to connect; a transport only connects to the address it
is handed. Tests replace `StdlibTransport` with a fake, so unit tests never touch
the network.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import ssl
import threading
import time
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
        self, url: Url, address: str, headers: dict[str, str], timeout: float, deadline: float
    ) -> RawResponse:
        """Send GET for `url` to `address`; raise `TransportFailure` on no response.
        `timeout` bounds each blocking call; `deadline` (a `time.monotonic()` value)
        bounds the whole exchange, body included."""


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
        self.raw_sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = self.raw_sock = socket.create_connection((self._address, self.port),
                                                             self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        context = _tls_context()
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context
        self.raw_sock: socket.socket | None = None

    def connect(self) -> None:
        raw = self.raw_sock = socket.create_connection((self._address, self.port), self.timeout)
        try:
            wrapped = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        # Wrapping detaches the raw socket; the watchdog must shut the wrapped one.
        self.sock = self.raw_sock = wrapped


class _Watchdog:
    """Ends an exchange at its total deadline, whatever it is waiting on (connect,
    TLS, headers or a drip-fed body): at the deadline it shuts the socket down, so
    the blocked read returns and the exchange fails as a timeout."""

    def __init__(self, conn: _PinnedHTTPConnection | _PinnedHTTPSConnection,
                 deadline: float) -> None:
        self.expired = threading.Event()
        self._conn = conn
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self.expired.set()
        sock = self._conn.raw_sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def cancel(self) -> None:
        self._timer.cancel()


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
        self, url: Url, address: str, headers: dict[str, str], timeout: float, deadline: float
    ) -> RawResponse:
        conn: _PinnedHTTPConnection | _PinnedHTTPSConnection
        if url.is_https:
            conn = _PinnedHTTPSConnection(url.host, url.port, address, timeout)
        else:
            conn = _PinnedHTTPConnection(url.host, url.port, address, timeout)
        watchdog = _Watchdog(conn, deadline)

        def failure(code: str, exc: BaseException) -> TransportFailure:
            conn.close()
            watchdog.cancel()
            if watchdog.expired.is_set():
                return TransportFailure("timeout", "total deadline reached")
            return TransportFailure(code, str(exc))

        try:
            conn.putrequest("GET", url.path_and_query, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", url.host_header)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()
            resp = conn.getresponse()
        except ssl.SSLError as exc:
            raise failure("tls_failed", exc) from exc
        except TimeoutError as exc:
            raise failure("timeout", exc) from exc
        except (ConnectionError, OSError) as exc:
            raise failure("connect_failed", exc) from exc
        except (http.client.HTTPException, UnicodeError, ValueError) as exc:
            raise failure("protocol_error", exc) from exc

        def body() -> Iterator[bytes]:
            try:
                while True:
                    try:
                        chunk = resp.read1(65536)
                    except TimeoutError as exc:
                        raise failure("timeout", exc) from exc
                    except (http.client.HTTPException, OSError, ValueError) as exc:
                        raise failure("protocol_error", exc) from exc
                    if watchdog.expired.is_set():
                        raise TransportFailure("timeout", "total deadline reached")
                    if not chunk:
                        return
                    yield chunk
            finally:
                watchdog.cancel()
                conn.close()

        def closer() -> None:
            watchdog.cancel()
            conn.close()

        return RawResponse(resp.status, list(resp.getheaders()), body(), closer)
