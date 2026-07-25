"""GitHub/Google OAuth client glue -- docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md
§4 ("control-plane as OAuth *client*, to GitHub/Google").

Simplification vs. the design doc's own wording: Google's profile is
fetched from its OpenID Connect userinfo endpoint using the access token
from a direct, server-to-server code exchange, rather than performing
local JWKS-based `id_token` signature verification. Both prove the same
thing (Google vouches for this profile) -- verifying an `id_token`
matters most when a client receives it directly from a browser redirect
(the implicit/hybrid flow, where nothing else attests to its origin);
here the access token itself only exists because *this server* already
completed an authenticated, TLS-protected exchange with Google, so a
follow-up authenticated call to Google's own userinfo endpoint carries
the same trust without needing a JWKS-fetch-and-cache subsystem for a
single call site. Neither path is exercised against the real
github.com/accounts.google.com in this repo's test suite either way (see
the design doc's §6) -- both are implemented against each provider's
real, documented API shape and tested against a fake HTTP transport
standing in for it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import settings
from .errors import ApiError

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def get_http_client() -> httpx.AsyncClient:
    """Overridable in tests: `monkeypatch.setattr(oauth_providers,
    "get_http_client", lambda: httpx.AsyncClient(transport=fake_transport))`
    -- same override-a-module-function pattern `hosted_mcp.get_manager`
    already uses, chosen for the same reason `app.dependency_overrides`
    doesn't reach here (this is called from plain functions, not FastAPI
    route dependencies, so it needs to be swappable independent of
    FastAPI's DI)."""
    return httpx.AsyncClient(timeout=10.0)


@dataclass(frozen=True)
class SocialProfile:
    provider_user_id: str
    email: str


async def fetch_github_profile(*, code: str, redirect_uri: str) -> SocialProfile:
    async with get_http_client() as client:
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise ApiError(401, "github_oauth_failed", "GitHub rejected the authorization code")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise ApiError(401, "github_oauth_failed", "GitHub did not return an access token")

        auth_header = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
        profile_resp = await client.get(GITHUB_USER_URL, headers=auth_header)
        if profile_resp.status_code != 200:
            raise ApiError(401, "github_oauth_failed", "Failed to fetch GitHub profile")
        provider_user_id = str(profile_resp.json().get("id"))

        emails_resp = await client.get(GITHUB_USER_EMAILS_URL, headers=auth_header)
        if emails_resp.status_code != 200:
            raise ApiError(401, "github_oauth_failed", "Failed to fetch GitHub account emails")
        # GitHub can return multiple emails on one account -- only a
        # verified, primary one is trustworthy proof of ownership (see the
        # design doc's §4 on why this matters for the account-takeover
        # protection below).
        primary_email = next(
            (e["email"] for e in emails_resp.json() if e.get("primary") and e.get("verified")), None
        )
        if not primary_email:
            raise ApiError(
                401, "github_email_unverified", "GitHub account has no verified primary email"
            )
        return SocialProfile(provider_user_id=provider_user_id, email=primary_email)


async def fetch_google_profile(*, code: str, redirect_uri: str) -> SocialProfile:
    async with get_http_client() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise ApiError(401, "google_oauth_failed", "Google rejected the authorization code")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise ApiError(401, "google_oauth_failed", "Google did not return an access token")

        profile_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile_resp.status_code != 200:
            raise ApiError(401, "google_oauth_failed", "Failed to fetch Google profile")
        profile = profile_resp.json()
        if not profile.get("email_verified"):
            raise ApiError(401, "google_email_unverified", "Google account has no verified email")
        return SocialProfile(provider_user_id=str(profile["sub"]), email=profile["email"])
