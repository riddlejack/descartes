"""Where a capture may point the host's network: the public web, nothing else.

The rule is checked on the requested address and again at every redirect hop, on
every address a name resolves to. The connection then goes to an address that was
checked, never to a fresh lookup of the name, so a short DNS lifetime cannot turn a
public answer at check time into a loopback one at connect time.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from stemma.evidence.limits import ALLOWED_PORTS


class PolicyRefusal(Exception):
    """The address may not be fetched. `reason` names the rule."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class InvalidUrl(Exception):
    """The string is not a URL we can parse."""


@dataclass(frozen=True)
class Url:
    """A parsed absolute http(s) URL that passed the spelling rules."""

    scheme: str
    host: str  # lower case; IPv6 without brackets
    port: int
    path_and_query: str  # what goes on the request line
    url: str  # canonical text, without fragment

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.is_https else 80
        return host if self.port == default else f"{host}:{self.port}"


# Invariant: characters never allowed anywhere in a URL we fetch.
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
# Invariant: a host made only of digits, dots and hex markers is a numeric spelling
# (`2130706433`, `0x7f.1`, `127.1`) that resolvers expand in surprising ways. Only
# plain dotted quads and bracketed IPv6 are accepted as literals.
_NUMERIC_LOOKING = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]*))*\.?$", re.IGNORECASE)


# Invariant: characters left as they are when percent-encoding a path or query.
_SAFE = "/%:@!$&'()*+,;=-._~"


def parse_url(raw: str) -> Url:
    """Parse and check spelling. Raises `InvalidUrl` or `PolicyRefusal`."""
    if not isinstance(raw, str) or not raw:
        raise InvalidUrl("empty URL")
    if _CONTROL.search(raw):
        raise InvalidUrl("URL contains whitespace or control characters")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidUrl("URL is not valid Unicode text") from exc
    try:
        parts = urlsplit(raw)
        port_value = parts.port
    except ValueError as exc:
        raise InvalidUrl(str(exc)) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise PolicyRefusal("scheme_not_allowed", f"scheme {scheme or '(none)'!r}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise PolicyRefusal("userinfo_not_allowed", "URL carries a user name or password")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidUrl("URL has no host")
    host = host.removesuffix(".")
    port = port_value if port_value is not None else (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise PolicyRefusal("port_not_allowed", f"port {port}")
    literal = _ip_literal(host)
    if literal is None:
        # IDNA first: it maps fullwidth digits and ideographic full stops
        # ("127。0。0。1") to ASCII, and the address rules must see that form.
        try:
            host = host.encode("idna").decode("ascii").lower().removesuffix(".")
        except UnicodeError as exc:
            raise InvalidUrl(f"host {host!r} is not a valid name") from exc
        literal = _ip_literal(host)
    if literal is None and _NUMERIC_LOOKING.match(host):
        raise PolicyRefusal("ambiguous_numeric_host", f"host {host!r}")
    if literal is not None:
        reason = blocked_address_reason(literal)
        if reason is not None:
            raise PolicyRefusal(reason, f"address {literal}")
    # Non-ASCII in the path or query goes on the wire percent-encoded as UTF-8;
    # existing escapes and reserved characters are left as they are.
    path = quote(parts.path or "/", safe=_SAFE)
    query = quote(parts.query, safe=_SAFE + "?")
    path_and_query = path + (f"?{query}" if query else "")
    netloc_host = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    netloc = netloc_host if port == default else f"{netloc_host}:{port}"
    canonical = urlunsplit((scheme, netloc, path, query, ""))
    return Url(scheme, host, port, path_and_query, canonical)


def resolve_redirect(current: Url, location: str) -> Url:
    """The next hop's URL, checked by the same rules as the first."""
    return parse_url(urljoin(current.url, location.strip()))


def _ip_literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def blocked_address_reason(address: str) -> str | None:
    """Name the class that makes `address` unfetchable, or None if it is public.

    IPv4-mapped, 6to4 and Teredo IPv6 forms are unwrapped first, so
    `::ffff:127.0.0.1` is loopback. The deciding rule is `is_global`; the named
    branches exist only so the receipt says why.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "unparseable_address"
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            candidates.append(parsed.ipv4_mapped)
        if parsed.sixtofour is not None:
            candidates.append(parsed.sixtofour)
        if parsed.teredo is not None:
            candidates.extend(parsed.teredo)
    candidates.append(parsed)
    for item in candidates:
        if item.is_unspecified:
            return "unspecified_address"
        if item.is_loopback:
            return "loopback_address"
        if item.is_link_local:
            return "link_local_address"
        if isinstance(item, ipaddress.IPv6Address) and item.is_site_local:
            return "site_local_address"
        if item.is_multicast:
            return "multicast_address"
        if item.is_reserved:
            return "reserved_address"
        if item.is_private:
            return "private_address"
        if not item.is_global:
            return "not_global_address"
    return None


def check_resolved(host: str, addresses: list[str]) -> str:
    """Every address a name answers with must be public; return the one to use."""
    if not addresses:
        raise PolicyRefusal("dns_no_address", f"host {host!r} resolved to nothing")
    for address in addresses:
        reason = blocked_address_reason(address)
        if reason is not None:
            raise PolicyRefusal(reason, f"host {host!r} resolves to {address}")
    return addresses[0]
