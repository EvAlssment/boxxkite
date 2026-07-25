"""MCP OAuth 2.1 authorization server -- docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md
§3, closing GitHub issue #86.

Exercises the full authorization_code+PKCE(S256) and refresh_token grants
end-to-end against this control-plane's own in-process authorization
server -- no external dependency, per the design doc's §6 testing
strategy ("tested end-to-end against a real in-process authorization
server -- this is boxkite's *own* AS, not GitHub/Google's").
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from conftest import signup
from control_plane import db as db_module
from control_plane.config import settings
from control_plane.models_orm import Account

REDIRECT_URI = "http://localhost:9999/callback"


async def _deactivate_account(account_id: str) -> None:
    """Directly sets `scim_deactivated_at`, bypassing the SCIM webhook
    machinery entirely -- these tests are about whether deactivation is
    *enforced* at every credential-resolution path in this module, not
    about the webhook/signature plumbing itself (already covered by
    test_scim_provisioning.py)."""
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one()
        account.scim_deactivated_at = datetime.now(timezone.utc)
        await db.commit()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _register_client(client: httpx.AsyncClient, *, redirect_uris: list[str] | None = None) -> dict:
    resp = await client.post(
        "/oauth/register",
        json={"client_name": "Test MCP Client", "redirect_uris": redirect_uris or [REDIRECT_URI]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _resource_identifier(client: httpx.AsyncClient) -> str:
    """This authorization server's own RFC 8707 resource identifier for
    `/mcp/`, as advertised by its RFC 9728 protected-resource metadata --
    fetched rather than hardcoded so tests stay correct regardless of the
    host the in-process test client happens to run against."""
    resp = await client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200, resp.text
    return resp.json()["resource"]


def _authorize_query(
    *,
    client_id: str,
    code_challenge: str,
    resource: str,
    state: str = "xyz",
    redirect_uri: str = REDIRECT_URI,
) -> str:
    return (
        f"response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
        f"&code_challenge={code_challenge}&code_challenge_method=S256&state={state}"
        f"&resource={resource}"
    )


async def _login_and_get_cookie(client: httpx.AsyncClient, *, authorize_query: str, email: str, password: str) -> httpx.Response:
    return await client.post(
        "/oauth/authorize/login",
        data={"authorize_query": authorize_query, "email": email, "password": password},
    )


async def test_metadata_endpoints_404_when_oauth_disabled(client: httpx.AsyncClient, monkeypatch):
    # BOXKITE_MCP_OAUTH_ENABLED now defaults to True (GitHub issue #114's
    # security review completed) -- explicitly disable to exercise the
    # still-supported opt-out path, rather than relying on a bare default.
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", False)
    resp = await client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 404


async def test_metadata_endpoints_shape_when_enabled(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)

    as_meta = await client.get("/.well-known/oauth-authorization-server")
    assert as_meta.status_code == 200
    body = as_meta.json()
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert body["token_endpoint"].endswith("/oauth/token")
    assert body["registration_endpoint"].endswith("/oauth/register")
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]

    pr_meta = await client.get("/.well-known/oauth-protected-resource")
    assert pr_meta.status_code == 200
    assert pr_meta.json()["resource"].endswith("/mcp/")


async def test_register_client_returns_client_id_no_secret(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)

    body = await _register_client(client)
    assert body["client_id"].startswith("mcp_client_")
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"


async def test_register_client_rejects_bad_redirect_uri_scheme(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)

    resp = await client.post(
        "/oauth/register", json={"client_name": "Evil", "redirect_uris": ["ftp://evil.example.com"]}
    )
    assert resp.status_code == 422


async def test_dcr_rate_limited(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_OAUTH_DCR_RATE_LIMIT_PER_MINUTE", 2)

    for _ in range(2):
        resp = await client.post(
            "/oauth/register", json={"client_name": "C", "redirect_uris": [REDIRECT_URI]}
        )
        assert resp.status_code == 201

    resp = await client.post("/oauth/register", json={"client_name": "C", "redirect_uris": [REDIRECT_URI]})
    assert resp.status_code == 429


async def test_authorize_unknown_client_id_shows_error(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "does-not-exist",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": "x",
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert "Unknown client_id" in resp.text


async def test_authorize_redirect_uri_mismatch_shows_error(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    reg = await _register_client(client)

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/not-registered",
            "code_challenge": "x",
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert "redirect_uri" in resp.text


async def test_authorize_unsupported_response_type_redirects_with_error(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    reg = await _register_client(client)

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "token",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": "x",
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(REDIRECT_URI)
    qs = parse_qs(urlparse(location).query)
    assert qs["error"] == ["unsupported_response_type"]
    assert qs["state"] == ["s1"]


async def test_authorize_shows_login_form_when_unauthenticated(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    reg = await _register_client(client)
    _, challenge = _pkce_pair()
    resource = await _resource_identifier(client)

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
            "resource": resource,
        },
    )
    assert resp.status_code == 200
    assert "Sign in to continue" in resp.text
    assert "Test MCP Client" in resp.text


async def test_authorize_missing_resource_redirects_with_invalid_target(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    reg = await _register_client(client)
    _, challenge = _pkce_pair()

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["error"] == ["invalid_target"]
    assert qs["state"] == ["s1"]


async def test_authorize_wrong_resource_redirects_with_invalid_target(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    reg = await _register_client(client)
    _, challenge = _pkce_pair()

    resp = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
            "resource": "https://some-other-resource-server.example.com/",
        },
    )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["error"] == ["invalid_target"]
    assert qs["state"] == ["s1"]


async def _authorize_and_get_code(client: httpx.AsyncClient, *, email: str, password: str = "hunter2pass") -> dict:
    """Runs register -> login -> consent, stopping right before the final
    code exchange -- lets a caller deactivate the account in between
    getting the code and exchanging it, to test the exchange step's own
    deactivation check in isolation from account-resolution elsewhere."""
    signup_resp = await signup(client, email, password=password)
    reg = await _register_client(client)
    verifier, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    login_resp = await _login_and_get_cookie(
        client, authorize_query=authorize_query, email=email, password=password
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == f"/oauth/authorize?{authorize_query}"

    consent_resp = await client.get(f"/oauth/authorize?{authorize_query}")
    assert consent_resp.status_code == 200
    assert "wants to access your boxkite account" in consent_resp.text

    decide_resp = await client.post(
        "/oauth/authorize/decide", data={"authorize_query": authorize_query, "decision": "allow"}
    )
    assert decide_resp.status_code == 302
    location = decide_resp.headers["location"]
    assert location.startswith(REDIRECT_URI)
    qs = parse_qs(urlparse(location).query)
    code = qs["code"][0]
    assert qs["state"] == ["xyz"]

    return {
        "code": code,
        "verifier": verifier,
        "client_id": reg["client_id"],
        "resource": resource,
        "account_id": signup_resp["account"]["id"],
    }


async def _full_authorize_flow(client: httpx.AsyncClient, *, email: str, password: str = "hunter2pass") -> dict:
    """Runs register -> login -> consent -> code exchange, returns the
    token response body plus the registered client_id for reuse in tests
    that need to exercise the token endpoint further (refresh, reuse)."""
    ctx = await _authorize_and_get_code(client, email=email, password=password)

    token_resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": ctx["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": ctx["client_id"],
            "code_verifier": ctx["verifier"],
            "resource": ctx["resource"],
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    body = token_resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    return {**body, "client_id": ctx["client_id"], "code": ctx["code"], "resource": ctx["resource"], "account_id": ctx["account_id"]}


async def test_full_authorization_code_pkce_flow_succeeds(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await _full_authorize_flow(client, email="oauth-flow@example.com")


async def test_authorization_code_cannot_be_reused(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-reuse@example.com")

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": result["client_id"],
            "code_verifier": "irrelevant-since-code-already-consumed",
            "resource": result["resource"],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_wrong_pkce_verifier_rejected(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await signup(client, "oauth-badpkce@example.com", password="hunter2pass")
    reg = await _register_client(client)
    _verifier, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-badpkce@example.com", password="hunter2pass"
    )
    decide_resp = await client.post(
        "/oauth/authorize/decide", data={"authorize_query": authorize_query, "decision": "allow"}
    )
    code = parse_qs(urlparse(decide_resp.headers["location"]).query)["code"][0]

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": reg["client_id"],
            "code_verifier": "totally-wrong-verifier",
            "resource": resource,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_token_endpoint_missing_resource_rejected(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await signup(client, "oauth-noresource@example.com", password="hunter2pass")
    reg = await _register_client(client)
    verifier, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-noresource@example.com", password="hunter2pass"
    )
    decide_resp = await client.post(
        "/oauth/authorize/decide", data={"authorize_query": authorize_query, "decision": "allow"}
    )
    code = parse_qs(urlparse(decide_resp.headers["location"]).query)["code"][0]

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


async def test_token_endpoint_wrong_resource_rejected(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await signup(client, "oauth-badresource@example.com", password="hunter2pass")
    reg = await _register_client(client)
    verifier, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-badresource@example.com", password="hunter2pass"
    )
    decide_resp = await client.post(
        "/oauth/authorize/decide", data={"authorize_query": authorize_query, "decision": "allow"}
    )
    code = parse_qs(urlparse(decide_resp.headers["location"]).query)["code"][0]

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": "https://some-other-resource-server.example.com/",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_target"


async def test_deny_decision_redirects_with_access_denied(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await signup(client, "oauth-deny@example.com", password="hunter2pass")
    reg = await _register_client(client)
    _verifier, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-deny@example.com", password="hunter2pass"
    )
    decide_resp = await client.post(
        "/oauth/authorize/decide", data={"authorize_query": authorize_query, "decision": "deny"}
    )
    assert decide_resp.status_code == 302
    qs = parse_qs(urlparse(decide_resp.headers["location"]).query)
    assert qs["error"] == ["access_denied"]


async def test_authorize_decide_wrong_resource_rejected(client: httpx.AsyncClient, monkeypatch):
    """Defense-in-depth: `/oauth/authorize/decide` re-validates `resource`
    from the posted-back `authorize_query`, not just the initial `GET
    /oauth/authorize` -- mirrors the existing client_id/redirect_uri
    re-validation already done at this step."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await signup(client, "oauth-decide-badresource@example.com", password="hunter2pass")
    reg = await _register_client(client)
    _verifier, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-decide-badresource@example.com", password="hunter2pass"
    )
    tampered_query = authorize_query.replace(
        f"resource={resource}", "resource=https://some-other-resource-server.example.com/"
    )
    decide_resp = await client.post(
        "/oauth/authorize/decide", data={"authorize_query": tampered_query, "decision": "allow"}
    )
    assert decide_resp.status_code == 302
    qs = parse_qs(urlparse(decide_resp.headers["location"]).query)
    assert qs["error"] == ["invalid_target"]


async def test_login_wrong_password_shows_error_on_login_page(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    await signup(client, "oauth-wrongpw@example.com", password="hunter2pass")
    reg = await _register_client(client)
    _, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    resp = await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-wrongpw@example.com", password="totally-wrong"
    )
    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text


async def test_refresh_token_grant_rotates_and_old_token_then_fails(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-refresh@example.com")

    refresh_resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
            "resource": result["resource"],
        },
    )
    assert refresh_resp.status_code == 200, refresh_resp.text
    new_body = refresh_resp.json()
    # The access token is a stateless JWT -- minted with second-granularity
    # iat/exp claims, it can be byte-identical to the previous one if both
    # are issued within the same wall-clock second for the same
    # account/client. What must actually differ (and be independently
    # revocable) is the refresh token.
    assert new_body["refresh_token"] != result["refresh_token"]

    # The old refresh token must no longer be usable on its own.
    stale_resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
            "resource": result["resource"],
        },
    )
    assert stale_resp.status_code == 400
    assert stale_resp.json()["error"] == "invalid_grant"


async def test_refresh_token_grant_missing_resource_rejected(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-refresh-noresource@example.com")

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


async def test_refresh_token_grant_wrong_resource_rejected(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-refresh-badresource@example.com")

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
            "resource": "https://some-other-resource-server.example.com/",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_target"


async def test_refresh_token_reuse_revokes_whole_chain(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-reuse-chain@example.com")

    refresh_resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
            "resource": result["resource"],
        },
    )
    rotated = refresh_resp.json()

    # Reuse of the original (now-stale) refresh token is a theft signal --
    # this must revoke the ENTIRE chain, including the token that was
    # legitimately issued by the rotation above.
    reuse_resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
            "resource": result["resource"],
        },
    )
    assert reuse_resp.status_code == 400

    now_dead_resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": rotated["refresh_token"],
            "client_id": result["client_id"],
            "resource": result["resource"],
        },
    )
    assert now_dead_resp.status_code == 400
    assert now_dead_resp.json()["error"] == "invalid_grant"


async def test_concurrent_refresh_token_reuse_does_not_mint_two_token_pairs(
    client: httpx.AsyncClient, monkeypatch
):
    """Regression test for a TOCTOU race found in security review (GitHub
    issue #114): OAuthTokenRepository.revoke used to be a SELECT, a
    Python-side `revoked_at is None` check, then a separate write -- two
    concurrent refresh_token grants presenting the same not-yet-rotated
    token (the actual theft scenario reuse detection exists to catch)
    could both read `revoked_at IS NULL` before either committed, and both
    mint an independent valid token pair from a single refresh token.
    `revoke` is now a single atomic `UPDATE ... WHERE revoked_at IS NULL`;
    exactly one of two concurrent presentations may win."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-concurrent-refresh@example.com")

    async def _refresh():
        return await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": result["refresh_token"],
                "client_id": result["client_id"],
                "resource": result["resource"],
            },
        )

    first, second = await asyncio.gather(_refresh(), _refresh())
    statuses = sorted([first.status_code, second.status_code])
    # This is the core guarantee the atomic UPDATE ... WHERE revoked_at IS
    # NULL fix provides, and the one that matters: two concurrent
    # presentations of the same refresh token can never BOTH mint a valid
    # token pair. (revoke_family additionally tries to kill the winner's
    # newly-minted child too, as defense in depth against the "concurrent
    # presentation is itself a theft signal" case -- but that's only
    # guaranteed under strictly *sequential* reuse, already covered by
    # test_refresh_token_reuse_revokes_whole_chain above. Under true
    # concurrent interleaving the loser's revoke_family walk can run before
    # the winner's child row is even committed, so it may not find it --
    # not asserted here since it isn't a reliable guarantee to test.)
    assert statuses == [200, 400], (first.text, second.text)


async def test_concurrent_authorization_code_exchange_only_succeeds_once(
    client: httpx.AsyncClient, monkeypatch
):
    """Same TOCTOU bug class as the refresh-token race above, in
    OAuthAuthorizationCodeRepository.mark_consumed -- two concurrent
    exchanges of the same authorization code (a leaked code, or a client
    retrying after a timeout) used to both pass the "unconsumed" check
    before either committed, minting two token pairs from a single-use
    code. mark_consumed is now a single atomic `UPDATE ... WHERE
    consumed_at IS NULL`; exactly one of two concurrent exchanges may
    succeed."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    ctx = await _authorize_and_get_code(client, email="oauth-concurrent-code@example.com")

    async def _exchange():
        return await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": ctx["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": ctx["client_id"],
                "code_verifier": ctx["verifier"],
                "resource": ctx["resource"],
            },
        )

    first, second = await asyncio.gather(_exchange(), _exchange())
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 400], (first.text, second.text)


async def test_oauth_token_endpoint_denies_framing(client: httpx.AsyncClient, monkeypatch):
    """Regression test for the missing clickjacking protection security
    review found (GitHub issue #114): /oauth/authorize's consent screen is
    this control-plane's first cookie-authenticated, browser-rendered
    page, and any caller can self-register an OAuth client controlling its
    displayed client_name (POST /oauth/register's open-registration model)
    -- without a framing defense, a logged-in victim could be lured onto
    an attacker page framing /oauth/authorize and clickjacked into
    approving that attacker's access. Checked against a plain JSON
    response (not just the consent screen itself) since the fix is applied
    globally -- see main.py's deny_framing middleware docstring for why."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    resp = await client.post("/oauth/token", data={"grant_type": "password"})
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


async def test_token_endpoint_unsupported_grant_type(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    resp = await client.post("/oauth/token", data={"grant_type": "password"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


async def test_mcp_endpoint_accepts_oauth_access_token(client: httpx.AsyncClient, monkeypatch):
    """/mcp/'s BearerTokenAuthMiddleware must accept the JWT this
    authorization server issues, not just a static API key -- see
    docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.5.

    `BOXKITE_PUBLIC_URL` is pinned so both the authorization server (this
    test's main `client`) and the standalone `/mcp` ASGI app spun up below
    (a separate in-process app, on its own arbitrary test host) agree on
    the same canonical resource identifier for the RFC 8707 audience check
    -- in a real deployment both live behind the one public URL already."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_PUBLIC_URL", "http://testserver")
    result = await _full_authorize_flow(client, email="oauth-mcp@example.com")

    from control_plane.hosted_mcp import build_hosted_mcp_asgi_app

    mcp, asgi_app = build_hosted_mcp_asgi_app()
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as mcp_client:
            resp = await mcp_client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.1"},
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {result['access_token']}",
                },
            )
    assert resp.status_code == 200
    assert "boxkite" in resp.text


async def test_mcp_endpoint_rejects_token_minted_for_a_different_resource(client: httpx.AsyncClient, monkeypatch):
    """RFC 8707 audience check at the protected-resource boundary: an
    access token whose `aud` claim doesn't match this deployment's own MCP
    resource identifier must be rejected, not silently accepted just
    because it's a validly-signed JWT of the right `type` -- the exact
    token-confusion gap GitHub issue #115 closes."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_PUBLIC_URL", "http://testserver")

    from control_plane.security import create_mcp_access_token

    other_resource_token, _ = create_mcp_access_token(
        account_id="00000000-0000-0000-0000-000000000000",
        client_id="mcp_client_irrelevant",
        audience="https://some-other-resource-server.example.com/mcp/",
    )

    from control_plane.hosted_mcp import build_hosted_mcp_asgi_app

    mcp, asgi_app = build_hosted_mcp_asgi_app()
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as mcp_client:
            resp = await mcp_client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.1"},
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {other_resource_token}",
                },
            )
    assert resp.status_code == 401


# ── SCIM deactivation must be enforced everywhere this authorization
# server/protected resource resolves an already-issued credential back to
# an Account -- the two CRITICAL gaps this section closes. ────────────────


async def test_authorize_login_rejects_deactivated_account(client: httpx.AsyncClient, monkeypatch):
    """`POST /oauth/authorize/login` mints a brand new login-session cookie
    -- same credential-issuance-time gate as routers/auth.py's login/
    refresh and routers/social_login.py's _finish_login."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    signup_resp = await signup(client, "oauth-deactivated-login@example.com", password="hunter2pass")
    await _deactivate_account(signup_resp["account"]["id"])

    reg = await _register_client(client)
    _, challenge = _pkce_pair()
    resource = await _resource_identifier(client)
    authorize_query = _authorize_query(client_id=reg["client_id"], code_challenge=challenge, resource=resource)

    resp = await _login_and_get_cookie(
        client, authorize_query=authorize_query, email="oauth-deactivated-login@example.com", password="hunter2pass"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_deactivated"


async def test_deactivation_blocks_authorization_code_grant(client: httpx.AsyncClient, monkeypatch):
    """The account is active through login/consent, then deactivated
    BEFORE the code is exchanged at /oauth/token -- this is the real gap:
    a code minted while active must not still mint a fresh MCP access/
    refresh token pair once the account has since been deactivated."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    ctx = await _authorize_and_get_code(client, email="oauth-deactivated-code@example.com")
    await _deactivate_account(ctx["account_id"])

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": ctx["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": ctx["client_id"],
            "code_verifier": ctx["verifier"],
            "resource": ctx["resource"],
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_grant"


async def test_deactivation_blocks_refresh_token_grant(client: httpx.AsyncClient, monkeypatch):
    """A refresh token minted while the account was active must not be
    usable to mint a fresh access/refresh token pair once the account has
    since been deactivated -- the refresh-token-grant half of the same
    gap the authorization-code-grant test above covers."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    result = await _full_authorize_flow(client, email="oauth-deactivated-refresh@example.com")
    await _deactivate_account(result["account_id"])

    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": result["refresh_token"],
            "client_id": result["client_id"],
            "resource": result["resource"],
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_grant"


async def test_mcp_endpoint_rejects_already_issued_access_token_after_deactivation(
    client: httpx.AsyncClient, monkeypatch
):
    """The real already-issued-credential TOCTOU gap: an MCP access token
    minted BEFORE deactivation must stop working on the very next /mcp
    call AFTER deactivation, not just fail to be re-mintable. Before this
    fix, hosted_mcp.py's `_resolve_account_for_bearer_token` had no
    deactivation check at all on its JWT-decode-success branch, so a
    deactivated account's already-issued access token kept authenticating
    every tool call indefinitely."""
    monkeypatch.setattr(settings, "BOXKITE_MCP_OAUTH_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_PUBLIC_URL", "http://testserver")
    result = await _full_authorize_flow(client, email="oauth-mcp-deactivated@example.com")

    from control_plane.hosted_mcp import build_hosted_mcp_asgi_app

    mcp, asgi_app = build_hosted_mcp_asgi_app()
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as mcp_client:
            init_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.1"},
                },
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {result['access_token']}",
            }

            # Sanity: the token works before deactivation.
            before_resp = await mcp_client.post("/", json=init_body, headers=headers)
            assert before_resp.status_code == 200, before_resp.text

            await _deactivate_account(result["account_id"])

            # Same still-valid, still-unexpired token -- rejected on the
            # very next call after deactivation.
            after_resp = await mcp_client.post("/", json=init_body, headers=headers)
    assert after_resp.status_code == 401, after_resp.text
    assert after_resp.json()["error"]["code"] == "account_deactivated"
