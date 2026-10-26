"""Hostile tests of the address rule: every spelling of a private address is refused."""

from __future__ import annotations

import pytest

from stemma.evidence.netpolicy import (
    InvalidUrl,
    PolicyRefusal,
    blocked_address_reason,
    check_resolved,
    parse_url,
)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://10.0.0.5/admin",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://100.64.0.1/",  # shared address space
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    "http://[2002:7f00:1::1]/",  # 6to4 wrapping 127.0.0.1
    "http://[fe80::1]/",
    "http://[fc00::1]/",
    "http://224.0.0.1/",
    "http://240.0.0.1/",
])
def test_private_literals_refused(url: str) -> None:
    with pytest.raises(PolicyRefusal):
        parse_url(url)


@pytest.mark.parametrize("url", [
    "http://2130706433/",  # 127.0.0.1 as one decimal number
    "http://0x7f.0.0.1/",
    "http://127.1/",
    "http://017700000001/",
    "http://0x7f000001/",
])
def test_numeric_spellings_refused(url: str) -> None:
    with pytest.raises(PolicyRefusal) as info:
        parse_url(url)
    assert info.value.reason == "ambiguous_numeric_host"


@pytest.mark.parametrize("url, reason", [
    ("file:///etc/passwd", "scheme_not_allowed"),
    ("ftp://example.org/x", "scheme_not_allowed"),
    ("gopher://example.org/", "scheme_not_allowed"),
    ("http://user:pw@example.org/", "userinfo_not_allowed"),
    ("http://example.org@127.0.0.1/", "userinfo_not_allowed"),
    ("http://example.org:22/", "port_not_allowed"),
    ("https://example.org:8443/", "port_not_allowed"),
])
def test_scheme_userinfo_port_refused(url: str, reason: str) -> None:
    with pytest.raises(PolicyRefusal) as info:
        parse_url(url)
    assert info.value.reason == reason


@pytest.mark.parametrize("url", ["", "http://exa mple.org/", "http://example.org/\x00", "http://"])
def test_malformed(url: str) -> None:
    with pytest.raises((InvalidUrl, PolicyRefusal)):
        parse_url(url)


def test_public_urls_parse() -> None:
    url = parse_url("HTTPS://Example.ORG./a/b?q=1#frag")
    assert url.url == "https://example.org/a/b?q=1"
    assert url.host == "example.org" and url.port == 443
    assert parse_url("http://93.184.216.34/").host == "93.184.216.34"


def test_resolution_must_be_all_public() -> None:
    # A name answering with one public and one private address is refused whole.
    with pytest.raises(PolicyRefusal):
        check_resolved("mixed.example.org", ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(PolicyRefusal):
        check_resolved("none.example.org", [])
    assert check_resolved("ok.example.org", ["93.184.216.34"]) == "93.184.216.34"


def test_reason_names_class() -> None:
    assert blocked_address_reason("127.0.0.1") == "loopback_address"
    assert blocked_address_reason("::ffff:10.0.0.1") == "private_address"
    assert blocked_address_reason("8.8.8.8") is None
