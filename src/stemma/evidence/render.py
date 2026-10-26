"""The browser fallback (Playwright 1.64.1, optional extra `evidence-render`).

**Off unless the run turns it on** (`run.json`: `"evidence": {"browser_fallback":
true}`): B1 found no page that needed it, and a browser is the one fetch path
whose network the service cannot fully control. When on, it is tried only after a
plain HTML capture comes back `empty_text` or `short_text`, and never in `as_of`
mode.

Hardening, so the browser fetches nothing the plain fetch would not:
- the page's host is resolved and checked by the plain fetch's address rule, and
  Chromium is started with `--host-resolver-rules` mapping that host to the
  checked address and every other name to nothing, so Chromium's own lookups
  cannot reach anything else;
- every request except the one main-frame navigation to the checked URL is
  aborted: no redirects, no subresources, no other frames; WebSockets are never
  connected. So it renders only what the page's own inline scripts write;
- a non-success HTTP status is a typed failure (`RenderHttpError`), never text.
The rendered DOM is stored as its own bytes with its own receipt, linked to the
plain capture's receipt; nothing replaces the plain capture.
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


class RenderHttpError(Exception):
    """The main document answered with a non-success HTTP status."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _gate_decision(request_url: str, main_url: str, is_main_navigation: bool) -> bool:
    """Allow only the main-frame navigation to the checked URL itself."""
    if not is_main_navigation:
        return False
    try:
        return parse_url(request_url).url == parse_url(main_url).url
    except (InvalidUrl, PolicyRefusal):
        return False


class PlaywrightRenderer:
    name = "playwright-chromium"

    def __init__(self) -> None:
        try:
            from importlib import metadata

            import playwright.sync_api  # type: ignore[import-not-found,unused-ignore]  # noqa: F401

            self.version = metadata.version("playwright")
        except Exception as exc:
            raise RenderUnavailable(f"playwright not installed: {exc}") from exc

    def render(self, url: str) -> Rendered:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found,unused-ignore]

        try:
            parsed = parse_url(url)
            address = check_resolved(parsed.host, StdlibTransport().resolve(parsed.host))
        except (InvalidUrl, PolicyRefusal, TransportFailure) as exc:
            raise RenderUnavailable(f"address refused: {exc}") from exc
        pinned = f"[{address}]" if ":" in address else address
        resolver_rules = f"MAP {parsed.host} {pinned}, MAP * ~NOTFOUND"
        blocked: list[str] = []

        def gate(route: Any) -> None:
            request = route.request
            main = request.is_navigation_request() and request.frame.parent_frame is None
            if _gate_decision(request.url, parsed.url, main):
                route.continue_()
            else:
                blocked.append(request.url[:300])
                route.abort()

        timeout_ms = int(limits.TOTAL_DEADLINE_S * 1000 / 2)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True, args=[f"--host-resolver-rules={resolver_rules}"])
                try:
                    context = browser.new_context(java_script_enabled=True,
                                                  accept_downloads=False,
                                                  service_workers="block")
                    context.route_web_socket("**/*", lambda _ws: None)
                    context.route("**/*", gate)
                    page = context.new_page()
                    response = page.goto(parsed.url, wait_until="load", timeout=timeout_ms)
                    if response is None:
                        raise RenderUnavailable("no response for the main document")
                    if not 200 <= response.status < 300:
                        raise RenderHttpError(response.status)
                    with contextlib.suppress(Exception):  # idle is best effort after load
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    html = page.content().encode("utf-8")
                    final_url = page.url
                finally:
                    browser.close()
        except (RenderUnavailable, RenderHttpError):
            raise
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
