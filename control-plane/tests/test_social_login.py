"""GitHub/Google social login -- docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md
§4, closing GitHub issue #86.

Two layers, per the design doc's §6 testing strategy:
- `oauth_providers.fetch_github_profile`/`fetch_google_profile` tested
  directly against `httpx.MockTransport` standing in for each provider's
  real token/profile endpoints (never the real github.com/
  accounts.google.com -- no real credentials exist to test against).
- The router's account-resolution logic (new-account auto-registration,
  already-linked login, auto-linking a verified-email match onto an
  existing *social-only* account, refusing to reassign a conflicting
  provider identity, and refusing to auto-link onto a password- or
  SSO/SCIM-managed account) tested by monkeypatching the higher-level
  `fetch_*_profile` functions directly, so these tests assert boxkite's own
  behavior rather than re-mocking the same provider HTTP shape twice.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import signup
from control_plane import oauth_providers
from control_plane.config import settings
from control_plane.oauth_providers import SocialProfile
from control_plane.routers import social_login


def _state_with_nonce_cookie(client: httpx.AsyncClient, *, provider: str, next_path: str | None = None) -> str:
    """Mint a state token AND set the matching nonce cookie on `client`, the
    same way a real browser would carry it from /{provider}/start to
    /{provider}/callback -- required now that the callback rejects a state
    whose embedded nonce doesn't match a cookie set by the same browser
    (login-CSRF protection, see create_social_login_state_token's
    docstring)."""
    from control_plane.security import create_social_login_state_token

    state, nonce = create_social_login_state_token(provider=provider, next_path=next_path)
    client.cookies.set("boxkite_oauth_state_nonce", nonce)
    return state


# ── Layer 1: provider HTTP glue against a fake transport ────────────────
async def test_fetch_github_profile_against_fake_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gh-access-tok"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 4242})
        if request.url.path == "/user/emails":
            return httpx.Response(
                200,
                json=[
                    {"email": "secondary@example.com", "primary": False, "verified": True},
                    {"email": "primary@example.com", "primary": True, "verified": True},
                ],
            )
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(oauth_providers, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    profile = await oauth_providers.fetch_github_profile(code="abc", redirect_uri="https://cp.example.com/cb")
    assert profile == SocialProfile(provider_user_id="4242", email="primary@example.com")


async def test_fetch_github_profile_rejects_unverified_email(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gh-access-tok"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 1})
        if request.url.path == "/user/emails":
            return httpx.Response(200, json=[{"email": "unverified@example.com", "primary": True, "verified": False}])
        raise AssertionError

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(oauth_providers, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    from control_plane.errors import ApiError

    with pytest.raises(ApiError) as exc_info:
        await oauth_providers.fetch_github_profile(code="abc", redirect_uri="https://cp.example.com/cb")
    assert exc_info.value.code == "github_email_unverified"


async def test_fetch_google_profile_against_fake_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "google-access-tok"})
        if request.url.path == "/v1/userinfo":
            return httpx.Response(200, json={"sub": "goog-999", "email": "user@example.com", "email_verified": True})
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(oauth_providers, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    profile = await oauth_providers.fetch_google_profile(code="abc", redirect_uri="https://cp.example.com/cb")
    assert profile == SocialProfile(provider_user_id="goog-999", email="user@example.com")


async def test_fetch_google_profile_rejects_unverified_email(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/v1/userinfo":
            return httpx.Response(200, json={"sub": "1", "email": "x@example.com", "email_verified": False})
        raise AssertionError

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(oauth_providers, "get_http_client", lambda: httpx.AsyncClient(transport=transport))

    from control_plane.errors import ApiError

    with pytest.raises(ApiError) as exc_info:
        await oauth_providers.fetch_google_profile(code="abc", redirect_uri="https://cp.example.com/cb")
    assert exc_info.value.code == "google_email_unverified"


# ── Layer 2: router behavior, gating, and account resolution ───────────
async def test_github_routes_404_when_social_login_disabled(client: httpx.AsyncClient, monkeypatch):
    # BOXKITE_SOCIAL_LOGIN_ENABLED now defaults to True (GitHub issue #114's
    # security review completed) -- explicitly disable to exercise the
    # still-supported opt-out path, rather than relying on a bare default.
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", False)
    resp = await client.get("/v1/auth/github/start")
    assert resp.status_code == 404


async def test_github_routes_404_when_only_master_flag_set(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", True)
    # No client id/secret configured -- still 404 per config.py's "both must be set" contract.
    resp = await client.get("/v1/auth/github/start")
    assert resp.status_code == 404


def _enable_github(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", "gh-client-id")
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "gh-client-secret")


async def test_github_start_redirects_to_github_authorize(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    resp = await client.get("/v1/auth/github/start")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(oauth_providers.GITHUB_AUTHORIZE_URL)
    assert "client_id=gh-client-id" in location
    assert "state=" in location


async def test_github_start_drops_unsafe_next(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    resp = await client.get("/v1/auth/github/start", params={"next": "https://evil.example.com/steal"})
    assert resp.status_code == 302
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    from control_plane.security import decode_social_login_state_token

    payload = decode_social_login_state_token(state)
    assert payload["next"] is None


async def test_github_callback_auto_registers_new_account(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-111", email="newuser@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account"]["email"] == "newuser@example.com"
    assert body["access_token"]

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_github_id("gh-111")
        assert account is not None
        assert account.password_hash is None


async def test_github_callback_logs_in_already_linked_account(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        existing = await AccountRepository(db).create_social(email="linked@example.com", github_id="gh-222")

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-222", email="linked@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 200
    assert resp.json()["account"]["id"] == existing.id


async def test_github_callback_refuses_to_link_onto_password_account(
    client: httpx.AsyncClient, monkeypatch
):
    """Auto-linking must NOT extend to an existing password-based account,
    even though GitHub itself reports this email as verified: `/v1/auth/
    signup` performs no ownership verification at all (anyone can type any
    email there), so a matching email on a password account is not proof
    that account belongs to whoever is completing this OAuth flow -- unlike
    the social-only case (see
    test_google_then_github_with_same_email_links_both_to_one_account),
    where both sides independently proved control via their own provider's
    OAuth consent. Auto-linking here would let a pre-registered attacker
    account silently absorb a victim's real GitHub identity."""
    _enable_github(monkeypatch)
    await signup(client, "collision@example.com", password="hunter2pass")

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-333", email="collision@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "account_email_exists"

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email("collision@example.com")
        assert account.github_id is None


async def test_github_callback_refuses_to_link_onto_sso_managed_account(
    client: httpx.AsyncClient, monkeypatch
):
    """Auto-linking must NOT extend to an account with sso_provider_user_id
    set -- linking a personal GitHub identity onto an enterprise-SSO-managed
    account would bypass whatever IdP-level policy (MFA, conditional
    access) that organization enforces, the same boundary
    enterprise_sso.py's own `_resolve_or_create_account` protects with an
    organization_id check."""
    _enable_github(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        await AccountRepository(db).create_sso(
            email="ssomanaged@example.com",
            sso_provider_user_id="sso-user-1",
            sso_organization_id="org-1",
            sso_connection_id="conn-1",
        )

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-444", email="ssomanaged@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "account_email_exists"

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email("ssomanaged@example.com")
        assert account.github_id is None


async def test_github_callback_rejects_conflicting_provider_identity(
    client: httpx.AsyncClient, monkeypatch
):
    """If this email's account already has a DIFFERENT github_id linked,
    don't silently reassign it -- that's an unusual state (e.g. the
    provider-side account was deleted and recreated under a new id), not
    the common "signing in with a second provider for the first time"
    case `_resolve_or_create_account` otherwise auto-links."""
    _enable_github(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        await AccountRepository(db).create_social(email="reassigned@example.com", github_id="gh-old")

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-new", email="reassigned@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "account_provider_conflict"

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email("reassigned@example.com")
        assert account.github_id == "gh-old"


async def test_github_callback_with_next_sets_cookie_and_redirects(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    _enable_github(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-444", email="viaconsent@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    next_path = "/oauth/authorize?client_id=abc&redirect_uri=http://localhost/cb"
    state = _state_with_nonce_cookie(client, provider="github", next_path=next_path)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 303
    assert resp.headers["location"] == next_path
    assert "boxkite_oauth_session" in resp.cookies


async def test_github_callback_rejects_bad_state(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": "not-a-real-jwt"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


# ── Negative-path coverage for the login-CSRF nonce fix (GitHub issue #147)
# ── every other callback test above supplies a CORRECT nonce cookie via
# _state_with_nonce_cookie; these two assert the actual exploit scenario the
# fix exists to prevent -- a valid, well-formed state JWT presented by a
# browser that never held (or holds the wrong) matching cookie -- is
# rejected, not silently accepted.


async def test_github_callback_rejects_state_with_no_nonce_cookie(client: httpx.AsyncClient, monkeypatch):
    """The exact login-CSRF scenario create_social_login_state_token's
    docstring describes: a valid (code, state) pair obtained from a
    different browser's completed round-trip (or, here, simply never
    having visited /github/start on this client at all) must not be
    accepted just because the state JWT itself is validly signed."""
    _enable_github(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-csrf-1", email="csrf-victim@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)

    from control_plane.security import create_social_login_state_token

    state, _nonce = create_social_login_state_token(provider="github", next_path=None)
    # Deliberately do NOT set the boxkite_oauth_state_nonce cookie -- this
    # client never called /github/start, so it never received one.
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"

    # And no account was created from the rejected callback.
    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_github_id("gh-csrf-1")
        assert account is None


async def test_github_callback_rejects_state_with_mismatched_nonce_cookie(
    client: httpx.AsyncClient, monkeypatch
):
    """Same scenario, but the browser DOES hold a state-nonce cookie -- just
    not the one matching this particular state JWT (e.g. a stale cookie
    from an earlier, abandoned login attempt, or an attacker's page setting
    an arbitrary cookie value)."""
    _enable_github(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-csrf-2", email="csrf-victim-2@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)

    from control_plane.security import create_social_login_state_token

    state, _nonce = create_social_login_state_token(provider="github", next_path=None)
    client.cookies.set("boxkite_oauth_state_nonce", "some-other-unrelated-nonce-value")
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


async def test_oauth_consent_screen_html_denies_framing(client: httpx.AsyncClient, monkeypatch):
    """deny_framing (main.py) is applied globally, but every existing test
    of it (test_mcp_oauth.py) only checks a JSON API response -- this
    asserts the header is actually present on the one page it was written
    to protect: the real, HTML-rendered OAuth consent screen
    (GET /oauth/authorize), not just any endpoint."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", "gh-client-id")
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "gh-client-secret")
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", True)

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "some-client-id",
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": "http://testserver/mcp/",
        },
    )
    assert resp.headers.get("content-type", "").startswith("text/html")
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in resp.headers.get("content-security-policy", "")


# ── Dashboard oauth-callback redirect (BOXKITE_DASHBOARD_URL) ────────────


def _dashboard_url(monkeypatch) -> str:
    monkeypatch.setattr(settings, "BOXKITE_DASHBOARD_URL", "https://dashboard.example.com")
    return "https://dashboard.example.com/dashboard/oauth-callback"


async def test_github_start_preserves_dashboard_callback_next(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    dashboard_next = _dashboard_url(monkeypatch)

    resp = await client.get("/v1/auth/github/start", params={"next": dashboard_next})
    assert resp.status_code == 302
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    from control_plane.security import decode_social_login_state_token

    payload = decode_social_login_state_token(state)
    assert payload["next"] == dashboard_next


async def test_github_start_drops_dashboard_next_when_url_not_configured(
    client: httpx.AsyncClient, monkeypatch
):
    """Without BOXKITE_DASHBOARD_URL set, no dashboard callback URL can
    ever match -- the same next value that would be honored once
    configured must be dropped when it isn't."""
    _enable_github(monkeypatch)
    resp = await client.get(
        "/v1/auth/github/start",
        params={"next": "https://dashboard.example.com/dashboard/oauth-callback"},
    )
    assert resp.status_code == 302
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    from control_plane.security import decode_social_login_state_token

    payload = decode_social_login_state_token(state)
    assert payload["next"] is None


async def test_github_start_drops_lookalike_dashboard_next(client: httpx.AsyncClient, monkeypatch):
    """An exact-match allowlist, not a prefix/domain check -- a next value
    that merely starts with the configured dashboard origin but isn't the
    exact callback URL must still be dropped."""
    _enable_github(monkeypatch)
    _dashboard_url(monkeypatch)
    resp = await client.get(
        "/v1/auth/github/start",
        params={"next": "https://dashboard.example.com/dashboard/oauth-callback/../../evil"},
    )
    assert resp.status_code == 302
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    from control_plane.security import decode_social_login_state_token

    payload = decode_social_login_state_token(state)
    assert payload["next"] is None


async def test_github_callback_with_dashboard_next_redirects_with_fragment_token(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_github(monkeypatch)
    dashboard_next = _dashboard_url(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-555", email="dashboarduser@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=dashboard_next)
    resp = await client.get(
        "/v1/auth/github/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(f"{dashboard_next}#")
    # The access token must be in the fragment, never a query param -- a
    # query param would be sent to the server and logged; a fragment never
    # leaves the browser.
    assert "?access_token=" not in location
    from urllib.parse import parse_qs, urlparse

    fragment = parse_qs(urlparse(location).fragment)
    assert fragment["access_token"][0]
    assert fragment["token_type"][0] == "bearer"
    assert "refresh_token" not in fragment
    assert "boxkite_oauth_session" not in resp.cookies


async def test_github_callback_with_dashboard_next_includes_refresh_token_when_enabled(
    client: httpx.AsyncClient, monkeypatch
):
    """OAuth login must mint a refresh token exactly like password login
    does when BOXKITE_REFRESH_TOKENS_ENABLED is on -- otherwise an
    OAuth-authenticated dashboard session would still expire at
    ACCESS_TOKEN_TTL_MINUTES with no way to renew it silently."""
    _enable_github(monkeypatch)
    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)
    dashboard_next = _dashboard_url(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-777", email="refreshuser@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=dashboard_next)
    resp = await client.get(
        "/v1/auth/github/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 303
    location = resp.headers["location"]

    from urllib.parse import parse_qs, urlparse

    fragment = parse_qs(urlparse(location).fragment)
    assert fragment["access_token"][0]
    assert fragment["refresh_token"][0]
    assert len(fragment["refresh_token"][0]) > 20


async def test_github_callback_with_dashboard_next_redirects_error_for_deactivated_account(
    client: httpx.AsyncClient, monkeypatch
):
    """An ApiError raised after a validly-decoded state (here,
    account_deactivated from _finish_login) must redirect back to the
    dashboard's own callback page with `?error=`/`?error_description=`
    rather than leave the browser sitting on this control-plane's raw JSON
    response -- see _dashboard_error_redirect."""
    _enable_github(monkeypatch)
    dashboard_next = _dashboard_url(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        from datetime import datetime, timezone

        existing = await AccountRepository(db).create_social(email="deactivated@example.com", github_id="gh-666")
        existing.scim_deactivated_at = datetime.now(timezone.utc)
        await db.commit()

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-666", email="deactivated@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=dashboard_next)
    resp = await client.get(
        "/v1/auth/github/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 303
    from urllib.parse import parse_qs, urlparse

    location = resp.headers["location"]
    assert location.startswith(f"{dashboard_next}?")
    query = parse_qs(urlparse(location).query)
    assert query["error"][0] == "account_deactivated"
    assert query["error_description"][0]


async def test_github_callback_without_dashboard_next_still_raises_deactivated_account_error(
    client: httpx.AsyncClient, monkeypatch
):
    """Without a recognized dashboard `next`, the pragmatic raw-JSON
    fallback is unchanged -- only a caller arriving via the dashboard gets
    the redirect treatment."""
    _enable_github(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        from datetime import datetime, timezone

        existing = await AccountRepository(db).create_social(email="deactivated2@example.com", github_id="gh-777")
        existing.scim_deactivated_at = datetime.now(timezone.utc)
        await db.commit()

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-777", email="deactivated2@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="github", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_deactivated"


async def test_github_callback_rejects_state_from_other_provider(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    from control_plane.security import create_social_login_state_token

    state = create_social_login_state_token(provider="google", next_path=None)
    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


def _enable_google(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")


async def test_google_start_redirects_to_google_authorize(client: httpx.AsyncClient, monkeypatch):
    _enable_google(monkeypatch)
    resp = await client.get("/v1/auth/google/start")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(oauth_providers.GOOGLE_AUTHORIZE_URL)


async def test_google_callback_auto_registers_new_account(client: httpx.AsyncClient, monkeypatch):
    _enable_google(monkeypatch)

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="google-555", email="newgoogleuser@example.com")

    monkeypatch.setattr(social_login, "fetch_google_profile", fake_fetch)


    state = _state_with_nonce_cookie(client, provider="google", next_path=None)
    resp = await client.get("/v1/auth/google/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 200
    assert resp.json()["account"]["email"] == "newgoogleuser@example.com"


async def test_google_then_github_with_same_email_links_both_to_one_account(
    client: httpx.AsyncClient, monkeypatch
):
    """The exact real-world sequence this auto-link behavior exists for:
    sign in with Google first (auto-registers a password-less account),
    then sign in with GitHub using the same verified email -- must link
    onto the same account rather than 409ing with account_email_exists."""
    _enable_github(monkeypatch)
    _enable_google(monkeypatch)

    async def fake_google_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="google-999", email="bothproviders@example.com")

    monkeypatch.setattr(social_login, "fetch_google_profile", fake_google_fetch)


    google_state = _state_with_nonce_cookie(client, provider="google")
    google_resp = await client.get("/v1/auth/google/callback", params={"code": "abc", "state": google_state})
    assert google_resp.status_code == 200
    account_id = google_resp.json()["account"]["id"]

    async def fake_github_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-999", email="bothproviders@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_github_fetch)

    github_state = _state_with_nonce_cookie(client, provider="github")
    github_resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": github_state})
    assert github_resp.status_code == 200, github_resp.text
    assert github_resp.json()["account"]["id"] == account_id

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email("bothproviders@example.com")
        assert account.google_id == "google-999"
        assert account.github_id == "gh-999"
