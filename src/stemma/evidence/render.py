"""The browser fallback (Playwright 1.64.1, optional extra `evidence-render`).

Tried only when a plain HTML capture comes back `empty_text` or `short_text`. The
rendered page (the DOM after scripts ran, serialised as HTML) is stored as its own
bytes with its own receipt, linked to the plain capture's receipt; nothing replaces
the plain capture. Every request the browser makes is checked by the same address
rule as a plain fetch, and refused requests are aborted.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Protocol

from stemma.evidence import limits
from stemma.evidence.netpolicy import InvalidUrl, PolicyRefusal, check_resolved, parse_url
from stemma.evidence.transport import StdlibTransport, TransportFailure


@dataclass
class Rendered:
    html: bytes
    final_url: str
    blocked_requests: list[str]


class Renderer(Protocol):
    name: str
    version: str

    def render(self, url: str) -> Rendered: ...


class RenderUnavailable(Exception):
    pass


def _allowed(url: str, transport: StdlibTransport) -> bool:
    if url.startswith(("data:", "blob:", "about:")):
        return True
    try:
        parsed = parse_url(url)
        check_resolved(parsed.host, transport.resolve(parsed.host))
    except (InvalidUrl, PolicyRefusal, TransportFailure):
        return False
    return True


class PlaywrightRenderer:
    name = "playwright-chromium"

    def __init__(self) -> None:
        try:
            from importlib import metadata

            import playwright.sync_api  # type: ignore[import-not-found]  # noqa: F401

            self.version = metadata.version("playwright")
        except Exception as exc:
            raise RenderUnavailable(f"playwright not installed: {exc}") from exc

    def render(self, url: str) -> Rendered:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found,unused-ignore]

        transport = StdlibTransport()
        blocked: list[str] = []

        def gate(route: Any) -> None:
            target = route.request.url
            if _allowed(target, transport):
                route.continue_()
            else:
                blocked.append(target[:300])
                route.abort()

        timeout_ms = int(limits.TOTAL_DEADLINE_S * 1000 / 2)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    context = browser.new_context(java_script_enabled=True,
                                                  accept_downloads=False,
                                                  service_workers="block")
                    page = context.new_page()
                    page.route("**/*", gate)
                    page.goto(url, wait_until="load", timeout=timeout_ms)
                    with contextlib.suppress(Exception):  # idle is best effort after load
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    html = page.content().encode("utf-8")
                    final_url = page.url
                finally:
                    browser.close()
        except Exception as exc:
            raise RenderUnavailable(f"render failed: {exc}") from exc
        if len(html) > limits.MAX_BODY_BYTES:
            raise RenderUnavailable("rendered page passed the byte limit")
        return Rendered(html, final_url, blocked)


def default_renderer() -> Renderer | None:
    try:
        return PlaywrightRenderer()
    except RenderUnavailable:
        return None
