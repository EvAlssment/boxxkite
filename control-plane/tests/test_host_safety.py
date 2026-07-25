"""Tests for control_plane.host_safety -- the creation-time backstop check
for secrets' `allowed_hosts` (docs/SECRETS-DESIGN.md section 3/5), plus the
request-time `resolve_and_validate_destination_ip` check webhook delivery
uses (docs/WEBHOOKS-DESIGN.md section 8, GitHub issue #148).

Mirrors tests/test_sidecar_http_request.py's
test_unsafe_destination_ip_classification/test_cgnat_metadata_ip_is_disallowed/
test_nat64_embedded_metadata_ip_is_disallowed -- both layers must agree on
what counts as a disallowed destination address.
"""

from __future__ import annotations

from control_plane import host_safety
from control_plane.host_safety import is_disallowed_destination_ip, resolve_and_validate_destination_ip


def test_unsafe_destination_ip_classification():
    assert is_disallowed_destination_ip("169.254.169.254") is True
    assert is_disallowed_destination_ip("127.0.0.1") is True
    assert is_disallowed_destination_ip("10.0.0.5") is True
    assert is_disallowed_destination_ip("192.168.1.1") is True
    assert is_disallowed_destination_ip("::1") is True
    assert is_disallowed_destination_ip("93.184.216.34") is False


def test_cgnat_metadata_ip_is_disallowed():
    """Alibaba Cloud's IMDS (100.100.100.200) lives in 100.64.0.0/10 (RFC
    6598 CGNAT/shared address space), which ipaddress.is_private does not
    cover -- must be blocked explicitly."""
    assert is_disallowed_destination_ip("100.100.100.200") is True
    assert is_disallowed_destination_ip("100.64.0.1") is True
    assert is_disallowed_destination_ip("100.127.255.254") is True
    assert is_disallowed_destination_ip("100.63.255.255") is False
    assert is_disallowed_destination_ip("100.128.0.0") is False


def test_nat64_embedded_metadata_ip_is_disallowed():
    """64:ff9b::a9fe:a9fe is the NAT64 (RFC 6052) synthesized form of
    169.254.169.254 -- must be unwrapped and blocked the same way."""
    assert is_disallowed_destination_ip("64:ff9b::a9fe:a9fe") is True
    assert is_disallowed_destination_ip("::ffff:169.254.169.254") is True
    assert is_disallowed_destination_ip("64:ff9b::808:808") is False


async def test_resolve_and_validate_destination_ip_returns_ip_for_safe_hostname(monkeypatch):
    def _fake_getaddrinfo(hostname, port):
        assert hostname == "safe.example.com"
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(host_safety.socket, "getaddrinfo", _fake_getaddrinfo)
    assert await resolve_and_validate_destination_ip("safe.example.com") == "93.184.216.34"


async def test_resolve_and_validate_destination_ip_refuses_private_address(monkeypatch):
    """The concrete DNS-rebinding scenario at the request-time layer: a
    hostname that resolves to a private/metadata address right now must be
    refused, not silently allowed through."""

    def _fake_getaddrinfo(hostname, port):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(host_safety.socket, "getaddrinfo", _fake_getaddrinfo)
    assert await resolve_and_validate_destination_ip("looks-safe.example.com") is None


async def test_resolve_and_validate_destination_ip_refuses_mixed_public_and_private(monkeypatch):
    """A hostname with BOTH a public and a private/metadata A record must be
    refused outright -- never "pick the safe one and proceed"."""

    def _fake_getaddrinfo(hostname, port):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]

    monkeypatch.setattr(host_safety.socket, "getaddrinfo", _fake_getaddrinfo)
    assert await resolve_and_validate_destination_ip("mixed.example.com") is None


async def test_resolve_and_validate_destination_ip_returns_none_on_resolution_failure(monkeypatch):
    def _fake_getaddrinfo(hostname, port):
        raise host_safety.socket.gaierror("name or service not known")

    monkeypatch.setattr(host_safety.socket, "getaddrinfo", _fake_getaddrinfo)
    assert await resolve_and_validate_destination_ip("unresolvable.example.com") is None
