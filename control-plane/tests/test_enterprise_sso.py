"""Enterprise SSO login -- docs/ENTERPRISE-SSO-DESIGN.md, issue #126 Phase
1 only (SCIM provisioning is explicitly not attempted here).

Two layers, mirroring test_social_login.py's own split:
- Layer 1: WorkOSSsoClient tested directly against httpx.MockTransport
  standing in for WorkOS's real /sso/authorize and /sso/token endpoints --
  no real WorkOS account exists to test against.
- Layer 2: the router's gating, state-token round-trip, and
  account-resolution logic (auto-registration, already-linked login, the
  email-collision anti-takeover refusal) tested by monkeypatching
  enterprise_sso_client.get_enterprise_sso_client to return a
  FakeEnterpriseSsoClient (conftest.py), so these tests assert boxkite's
  own behavior rather than re-mocking WorkOS's HTTP shape a second time.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import signup
from control_plane import enterprise_sso_client
from control_plane.config import settings
from control_plane.enterprise_sso_client import EnterpriseSsoProfile, WorkOSSsoClient
from control_plane.routers import enterprise_sso


# ── Layer 1: WorkOSSsoClient against a fake transport ───────────────────
def test_authorization_url_includes_connection_and_state():
    client = WorkOSSsoClient()
    url = client.authorization_url(
        connection_selector="conn_abc123", redirect_uri="https://cp.example.com/v1/auth/sso/callback", state="xyz"
    )
    assert url.startswith(enterprise_sso_client.WORKOS_AUTHORIZE_URL)
    query = parse_qs(urlparse(url).query)
    assert query["connection"][0] == "conn_abc123"
    assert query["state"][0] == "xyz"
    assert query["redirect_uri"][0] == "https://cp.example.com/v1/auth/sso/callback"


async def test_fetch_profile_against_fake_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sso/token":
            return httpx.Response(
                200,
                json={
                    "profile": {
                        "id": "prof_111",
                        "email": "employee@enterprise.example.com",
                        "organization_id": "org_999",
                        "connection_id": "conn_888",
                    }
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(enterprise_sso_client, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    client = WorkOSSsoClient()
    profile = await client.fetch_profile(code="abc", redirect_uri="https://cp.example.com/callback")
    assert profile == EnterpriseSsoProfile(
        provider_user_id="prof_111",
        email="employee@enterprise.example.com",
        organization_id="org_999",
        connection_id="conn_888",
    )


async def test_fetch_profile_rejects_broker_error_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(enterprise_sso_client, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    from control_plane.errors import ApiError

    client = WorkOSSsoClient()
    with pytest.raises(ApiError) as exc_info:
        await client.fetch_profile(code="bad-code", redirect_uri="https://cp.example.com/callback")
    assert exc_info.value.code == "enterprise_sso_failed"


async def test_fetch_profile_rejects_missing_profile_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"profile": {"id": "", "email": ""}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(enterprise_sso_client, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    from control_plane.errors import ApiError

    client = WorkOSSsoClient()
    with pytest.raises(ApiError) as exc_info:
        await client.fetch_profile(code="abc", redirect_uri="https://cp.example.com/callback")
    assert exc_info.value.code == "enterprise_sso_failed"


def test_get_enterprise_sso_client_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "ENTERPRISE_SSO_PROVIDER", "nonsense")
    from control_plane.errors import ApiError

    with pytest.raises(ApiError) as exc_info:
        enterprise_sso_client.get_enterprise_sso_client()
    assert exc_info.value.code == "enterprise_sso_misconfigured"


def test_get_enterprise_sso_client_returns_workos_client_by_default():
    assert isinstance(enterprise_sso_client.get_enterprise_sso_client(), WorkOSSsoClient)


# ── Layer 2: router gating, state token, and account resolution ────────
async def test_sso_routes_404_when_disabled(client: httpx.AsyncClient):
    resp = await client.get("/v1/auth/sso/start", params={"connection": "conn_1"})
    assert resp.status_code == 404


async def test_sso_routes_404_when_only_master_flag_set(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_ENTERPRISE_SSO_ENABLED", True)
    # No WorkOS credentials configured -- still 404, mirrors
    # github_oauth_configured/google_oauth_configured's "both must be set" contract.
    resp = await client.get("/v1/auth/sso/start", params={"connection": "conn_1"})
    assert resp.status_code == 404


def _enable_sso(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BOXKITE_ENTERPRISE_SSO_ENABLED", True)
    monkeypatch.setattr(settings, "WORKOS_CLIENT_ID", "workos-client-id")
    monkeypatch.setattr(settings, "WORKOS_API_KEY", "workos-api-key")


async def test_sso_start_redirects_to_broker_with_connection_and_state(client: httpx.AsyncClient, monkeypatch):
    _enable_sso(monkeypatch)
    resp = await client.get("/v1/auth/sso/start", params={"connection": "conn_abc123"})
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "connection=conn_abc123" in location
    assert "state=" in location


async def test_sso_start_drops_unsafe_next(client: httpx.AsyncClient, monkeypatch):
    _enable_sso(monkeypatch)
    resp = await client.get(
        "/v1/auth/sso/start",
        params={"connection": "conn_abc123", "next": "https://evil.example.com/steal"},
    )
    assert resp.status_code == 302
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    from control_plane.security import decode_enterprise_sso_state_token

    payload = decode_enterprise_sso_state_token(state)
    assert payload["next"] is None
    assert payload["connection"] == "conn_abc123"


async def test_sso_callback_auto_registers_new_account(client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client):
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)
    fake_enterprise_sso_client.seed_profile(
        "auth-code-1",
        EnterpriseSsoProfile(
            provider_user_id="prof_new",
            email="newhire@enterprise.example.com",
            organization_id="org_1",
            connection_id="conn_1",
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_1", next_path=None)
    resp = await client.get("/v1/auth/sso/callback", params={"code": "auth-code-1", "state": state})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account"]["email"] == "newhire@enterprise.example.com"
    assert body["access_token"]

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_sso_subject_id("prof_new")
        assert account is not None
        assert account.password_hash is None
        assert account.sso_organization_id == "org_1"
        assert account.sso_connection_id == "conn_1"


async def test_sso_callback_logs_in_already_linked_account(client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client):
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        existing = await AccountRepository(db).create_sso(
            email="linked@enterprise.example.com", sso_provider_user_id="prof_existing"
        )

    fake_enterprise_sso_client.seed_profile(
        "auth-code-2",
        EnterpriseSsoProfile(
            provider_user_id="prof_existing",
            email="linked@enterprise.example.com",
            organization_id=None,
            connection_id=None,
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_1", next_path=None)
    resp = await client.get("/v1/auth/sso/callback", params={"code": "auth-code-2", "state": state})
    assert resp.status_code == 200
    assert resp.json()["account"]["id"] == existing.id


async def test_sso_callback_refuses_to_link_on_email_collision(client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client):
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)
    await signup(client, "collision@enterprise.example.com", password="hunter2pass")

    fake_enterprise_sso_client.seed_profile(
        "auth-code-3",
        EnterpriseSsoProfile(
            provider_user_id="prof_collision",
            email="collision@enterprise.example.com",
            organization_id="org_1",
            connection_id="conn_1",
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_1", next_path=None)
    resp = await client.get("/v1/auth/sso/callback", params={"code": "auth-code-3", "state": state})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "account_email_exists"


async def test_sso_callback_with_dashboard_next_redirects_error_on_email_collision(
    client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client
):
    """Same treatment as social_login.py's equivalent test: an ApiError
    raised after a validly-decoded state (here, the email-collision
    refusal) must redirect back to the dashboard's own callback page with
    `?error=`/`?error_description=` rather than leave the browser on this
    control-plane's raw JSON response."""
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)
    monkeypatch.setattr(settings, "BOXKITE_DASHBOARD_URL", "https://dashboard.example.com")
    dashboard_next = "https://dashboard.example.com/dashboard/oauth-callback"
    await signup(client, "ssodashcollision@enterprise.example.com", password="hunter2pass")

    fake_enterprise_sso_client.seed_profile(
        "auth-code-dash",
        EnterpriseSsoProfile(
            provider_user_id="prof_dash_collision",
            email="ssodashcollision@enterprise.example.com",
            organization_id="org_1",
            connection_id="conn_1",
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_1", next_path=dashboard_next)
    resp = await client.get(
        "/v1/auth/sso/callback", params={"code": "auth-code-dash", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(f"{dashboard_next}?")
    query = parse_qs(urlparse(location).query)
    assert query["error"][0] == "account_email_exists"
    assert query["error_description"][0]


async def test_sso_callback_rejects_invalid_state(client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client):
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)
    resp = await client.get("/v1/auth/sso/callback", params={"code": "whatever", "state": "not-a-real-jwt"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


async def test_sso_callback_rejects_unknown_authorization_code(client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client):
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_1", next_path=None)
    resp = await client.get("/v1/auth/sso/callback", params={"code": "never-seeded", "state": state})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "enterprise_sso_failed"


async def test_sso_callback_resumes_into_oauth_authorize_via_cookie(
    client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client
):
    _enable_sso(monkeypatch)
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)
    fake_enterprise_sso_client.seed_profile(
        "auth-code-4",
        EnterpriseSsoProfile(
            provider_user_id="prof_resume",
            email="resume@enterprise.example.com",
            organization_id="org_1",
            connection_id="conn_1",
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    next_path = "/oauth/authorize?client_id=abc&response_type=code"
    state = create_enterprise_sso_state_token(connection="conn_1", next_path=next_path)
    resp = await client.get(
        "/v1/auth/sso/callback", params={"code": "auth-code-4", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == next_path
    assert "boxkite_oauth_session" in resp.cookies
