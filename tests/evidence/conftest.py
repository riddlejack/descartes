"""Shared fakes for the evidence tests. Nothing here touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stemma.evidence.netpolicy import Url
from stemma.evidence.sec import CompanyRef, FilingRef
from stemma.evidence.service import EvidenceService
from stemma.evidence.transport import RawResponse, TransportFailure

# An address `ipaddress` calls global, for invented public host names.
PUBLIC_ADDRESS = "93.184.216.34"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: reaches the real web; run with STEMMA_LIVE=1")


@dataclass
class Reply:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    chunk: int = 65536
    fail_after: int | None = None  # raise TransportFailure after this many bytes

    def raw(self) -> RawResponse:
        def gen() -> Iterator[bytes]:
            sent = 0
            for i in range(0, len(self.body), self.chunk):
                if self.fail_after is not None and sent >= self.fail_after:
                    raise TransportFailure("timeout", "fake read timeout")
                piece = self.body[i:i + self.chunk]
                sent += len(piece)
                yield piece

        return RawResponse(self.status, list(self.headers), gen(), lambda: None)


def html(body: str, title: str = "Invented page") -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
            f"<style>.x{{color:red}}</style><script>var hidden = 'SCRIPT-TEXT';</script>"
            f"</head><body>{body}</body></html>").encode()


class FakeTransport:
    """Routes keyed by canonical URL. A value is a Reply, a list of Replies (served
    in turn), or a callable taking the Url."""

    def __init__(self, routes: dict[str, Any] | None = None,
                 dns: dict[str, list[str]] | None = None) -> None:
        self.routes: dict[str, Any] = dict(routes or {})
        self.dns = dict(dns or {})
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def resolve(self, host: str) -> list[str]:
        if host in self.dns:
            return self.dns[host]
        return [PUBLIC_ADDRESS]

    def request(self, url: Url, address: str, headers: dict[str, str],
                timeout: float, deadline: float | None = None) -> RawResponse:
        self.calls.append((url.url, address, dict(headers)))
        route = self.routes.get(url.url)
        if route is None:
            return Reply(404, [("Content-Type", "text/plain")], b"no route").raw()
        if isinstance(route, list):
            reply = route.pop(0) if len(route) > 1 else route[0]
        elif callable(route):
            reply = route(url)
        else:
            reply = route
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, Reply)
        return reply.raw()


class FakeSec:
    def __init__(self, filings: list[FilingRef]) -> None:
        self._filings = filings

    def resolve(self, company: str) -> CompanyRef:
        if company.upper() not in ("INVT", "0000999999", "999999", "INVENTED CO"):
            from stemma.evidence.errors import EvidenceError

            raise EvidenceError("sec_company_not_found", company)
        return CompanyRef(999999, "Invented Co")

    def filings(self, cik: int, form: str | None) -> list[FilingRef]:
        return [f for f in self._filings if form is None or f.form == form]

    def lookup(self, accession: str) -> FilingRef:
        for f in self._filings:
            if f.accession == accession:
                return f
        from stemma.evidence.errors import EvidenceError

        raise EvidenceError("sec_filing_not_found", accession)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    (folder / "task" / "attachments").mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps({"run_id": "t", "as_of": None}))
    return folder


def set_as_of(run_dir: Path, as_of: str | None) -> None:
    (run_dir / "run.json").write_text(json.dumps({"run_id": "t", "as_of": as_of}))


@pytest.fixture
def make_service(run_dir: Path, tmp_path: Path) -> Callable[..., EvidenceService]:
    def factory(transport: FakeTransport | None = None, **kwargs: Any) -> EvidenceService:
        kwargs.setdefault("cache_dir", tmp_path / "cache")
        kwargs.setdefault("renderer", None)
        kwargs.setdefault("sec_identity", "Test Builder test@example.org")
        kwargs.setdefault("throttle", False)
        return EvidenceService(run_dir, transport=transport or FakeTransport(), **kwargs)

    return factory


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small valid PDF (Helvetica text, one line per string), built by hand."""
    objects: list[bytes] = []
    kids = []
    font_id = 3
    next_id = 4
    page_ids = []
    for lines in pages:
        content_id, page_id = next_id, next_id + 1
        next_id += 2
        page_ids.append((page_id, content_id, lines))
        kids.append(f"{page_id} 0 R")
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_id, content_id, lines in page_ids:
        ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                       + stream + b"\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                       f"/Contents {content_id} 0 R >>".encode())
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
            .encode())
    return bytes(out)
