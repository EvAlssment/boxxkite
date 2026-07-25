"""POST /v1/account/link/{provider}/start + DELETE /v1/account/link/{provider}
-- lets an already-authenticated dashboard user link/unlink a GitHub/Google
identity to their own account, closing the dead end
routers/social_login.py's `account_email_exists`/`account_provider_conflict`
errors point at ("link {provider} from account settings").

Distinct trust model from routers/social_login.py's `_resolve_or_create_account`:
here the target account is known from the caller's own dashboard session at
`link_token` mint time, not inferred from matching the OAuth profile's
email -- see `_link_provider_to_account`.
"""

from __future__ import annotations

import httpx

from conftest import signup
from control_plane.config import settings
from control_plane.oauth_providers import SocialProfile
from control_plane.routers import social_login


def _enable_github(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", "gh-client-id")
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "gh-client-secret")


def _enable_google(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BOXKITE_SOCIAL_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")


async def test_start_link_provider_requires_dashboard_token(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    resp = await client.post("/v1/account/link/github/start")
    assert resp.status_code == 401


async def test_start_link_provider_404s_when_provider_disabled(client: httpx.AsyncClient):
    signup_resp = await signup(client, "linkstart@example.com")
    resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 404


async def test_start_link_provider_returns_a_link_token(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "linkstart2@example.com")

    resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 200, resp.text
    link_token = resp.json()["link_token"]
    assert link_token

    from control_plane.security import decode_account_link_intent_token

    payload = decode_account_link_intent_token(link_token)
    assert payload["provider"] == "github"
    assert payload["sub"] == signup_resp["account"]["id"]


async def test_github_start_embeds_link_account_id_in_state(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "linkstate@example.com")

    start_resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    link_token = start_resp.json()["link_token"]

    resp = await client.get("/v1/auth/github/start", params={"link_token": link_token})
    assert resp.status_code == 302

    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    from control_plane.security import decode_social_login_state_token

    payload = decode_social_login_state_token(state)
    assert payload["link_account_id"] == signup_resp["account"]["id"]


async def test_github_start_rejects_invalid_link_token(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    resp = await client.get("/v1/auth/github/start", params={"link_token": "not-a-real-jwt"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_link_token"


async def test_github_start_rejects_link_token_for_wrong_provider(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    _enable_google(monkeypatch)
    signup_resp = await signup(client, "wrongprovider@example.com")

    start_resp = await client.post(
        "/v1/account/link/google/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    google_link_token = start_resp.json()["link_token"]

    resp = await client.get("/v1/auth/github/start", params={"link_token": google_link_token})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_link_token"


async def test_link_flow_end_to_end_attaches_github_identity_to_the_logged_in_account(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "linkflow@example.com")
    account_id = signup_resp["account"]["id"]

    start_resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    link_token = start_resp.json()["link_token"]

    oauth_start_resp = await client.get("/v1/auth/github/start", params={"link_token": link_token})
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(oauth_start_resp.headers["location"]).query)["state"][0]

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        # Deliberately a DIFFERENT email than the account's own -- proves
        # linking targets the account from link_token, not an email match.
        return SocialProfile(provider_user_id="gh-linked-1", email="unrelated-email@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)

    callback_resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert callback_resp.status_code == 200, callback_resp.text
    assert callback_resp.json()["account"]["id"] == account_id

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        assert account.github_id == "gh-linked-1"
        # The account's own email is untouched by linking.
        assert account.email == "linkflow@example.com"


async def test_link_flow_rejects_identity_already_linked_to_a_different_account(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_github(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        await AccountRepository(db).create_social(email="other-owner@example.com", github_id="gh-taken")

    signup_resp = await signup(client, "linkconflict@example.com")
    start_resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    link_token = start_resp.json()["link_token"]

    oauth_start_resp = await client.get("/v1/auth/github/start", params={"link_token": link_token})
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(oauth_start_resp.headers["location"]).query)["state"][0]

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-taken", email="linkconflict@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)

    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "provider_identity_in_use"


async def test_link_flow_rejects_when_account_already_has_a_different_identity_linked(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "linkalreadyhas@example.com")
    account_id = signup_resp["account"]["id"]

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        account.github_id = "gh-existing"
        await db.commit()

    start_resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    link_token = start_resp.json()["link_token"]

    oauth_start_resp = await client.get("/v1/auth/github/start", params={"link_token": link_token})
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(oauth_start_resp.headers["location"]).query)["state"][0]

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-new-identity", email="linkalreadyhas@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)

    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "account_provider_conflict"


async def test_link_flow_is_idempotent_for_the_same_account_and_identity(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "linkidempotent@example.com")
    account_id = signup_resp["account"]["id"]

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        account.github_id = "gh-already-mine"
        await db.commit()

    start_resp = await client.post(
        "/v1/account/link/github/start", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    link_token = start_resp.json()["link_token"]

    oauth_start_resp = await client.get("/v1/auth/github/start", params={"link_token": link_token})
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(oauth_start_resp.headers["location"]).query)["state"][0]

    async def fake_fetch(*, code: str, redirect_uri: str) -> SocialProfile:
        return SocialProfile(provider_user_id="gh-already-mine", email="linkidempotent@example.com")

    monkeypatch.setattr(social_login, "fetch_github_profile", fake_fetch)

    resp = await client.get("/v1/auth/github/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 200, resp.text
    assert resp.json()["account"]["id"] == account_id


# ── DELETE /v1/account/link/{provider} ──────────────────────────────────


async def test_unlink_provider_succeeds_when_a_password_remains(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "unlinkok@example.com")
    account_id = signup_resp["account"]["id"]

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        account.github_id = "gh-to-unlink"
        await db.commit()

    resp = await client.delete(
        "/v1/account/link/github", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 204

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        assert account.github_id is None


async def test_unlink_provider_rejects_when_nothing_is_linked(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    signup_resp = await signup(client, "unlinknothing@example.com")

    resp = await client.delete(
        "/v1/account/link/github", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "not_linked"


async def test_unlink_provider_refuses_to_remove_the_only_login_method(client: httpx.AsyncClient, monkeypatch):
    """A social-only account (no password) with exactly one linked
    provider must not be able to unlink it -- that would permanently lock
    the account out with no way to authenticate again."""
    _enable_github(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository
    from control_plane.security import create_access_token

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).create_social(email="onlymethod@example.com", github_id="gh-only")
    access_token, _ttl = create_access_token(account_id=account.id, email=account.email)

    resp = await client.delete("/v1/account/link/github", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "last_login_method"

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account.id)
        assert account.github_id == "gh-only"


async def test_unlink_provider_succeeds_when_another_provider_remains(client: httpx.AsyncClient, monkeypatch):
    _enable_github(monkeypatch)
    _enable_google(monkeypatch)

    from control_plane import db as db_module
    from control_plane.repository import AccountRepository
    from control_plane.security import create_access_token

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).create_social(
            email="twoproviders@example.com", github_id="gh-two", google_id="google-two"
        )
    access_token, _ttl = create_access_token(account_id=account.id, email=account.email)

    resp = await client.delete("/v1/account/link/github", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 204

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account.id)
        assert account.github_id is None
        assert account.google_id == "google-two"
