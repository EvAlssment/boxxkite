"""Enterprise SSO client abstraction -- docs/ENTERPRISE-SSO-DESIGN.md §3.

`EnterpriseSsoClient` is the boundary `routers/enterprise_sso.py` depends
on. `WorkOSSsoClient` (this pass's only real implementation) terminates
the actual SAML/OIDC protocol complexity at a hosted-SSO-as-a-service
broker rather than this codebase parsing SAML XML directly -- see the
design doc's §2 for why that tradeoff was chosen given no real IdP test
credentials exist in this environment. A from-scratch SAML implementation
can be added later as a second class implementing this same Protocol
without touching the router or account-resolution logic at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx

from .config import settings
from .errors import ApiError

WORKOS_AUTHORIZE_URL = "https://api.workos.com/sso/authorize"
WORKOS_TOKEN_URL = "https://api.workos.com/sso/token"


@dataclass(frozen=True)
class EnterpriseSsoProfile:
    """Normalized shape every EnterpriseSsoClient implementation returns,
    regardless of whether the underlying IdP speaks SAML or OIDC --
    mirrors oauth_providers.SocialProfile's role for GitHub/Google."""

    provider_user_id: str
    email: str
    organization_id: str | None
    connection_id: str | None


class EnterpriseSsoClient(Protocol):
    """Backend-agnostic interface routers/enterprise_sso.py depends on.
    See this module's docstring for why a hosted broker is the only
    implementation in this pass, and docs/ENTERPRISE-SSO-DESIGN.md §3 for
    the intended swap-in path for a from-scratch SAML implementation."""

    def authorization_url(self, *, connection_selector: str, redirect_uri: str, state: str) -> str:
        """Build the URL to redirect the browser to in order to start an
        SSO login against `connection_selector` (an operator-assigned
        connection/organization identifier -- see the design doc's §3
        "Connection selection" note on why this is caller-supplied rather
        than resolved from an email domain in this pass)."""
        ...

    async def fetch_profile(self, *, code: str, redirect_uri: str) -> EnterpriseSsoProfile:
        """Exchange the callback's authorization `code` for a normalized
        profile. Raises ApiError on any failure (broker rejects the code,
        no profile returned, etc.) -- same failure-signaling contract
        oauth_providers.fetch_github_profile/fetch_google_profile use."""
        ...


def get_http_client() -> httpx.AsyncClient:
    """Overridable in tests -- same "swap a plain module function" pattern
    oauth_providers.get_http_client already uses, for the same reason
    (called from plain functions, not FastAPI route dependencies)."""
    return httpx.AsyncClient(timeout=10.0)


class WorkOSSsoClient:
    """Real implementation against WorkOS's documented SSO API shape.
    Never exercised against the real api.workos.com in this repo -- no
    WorkOS account exists in this environment; see the design doc's §6/§7
    for the same disclosed limitation oauth_providers' GitHub/Google
    clients already carry."""

    def authorization_url(self, *, connection_selector: str, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": settings.WORKOS_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "connection": connection_selector,
            "state": state,
        }
        return f"{WORKOS_AUTHORIZE_URL}?{urlencode(params)}"

    async def fetch_profile(self, *, code: str, redirect_uri: str) -> EnterpriseSsoProfile:
        async with get_http_client() as client:
            token_resp = await client.post(
                WORKOS_TOKEN_URL,
                data={
                    "client_id": settings.WORKOS_CLIENT_ID,
                    "client_secret": settings.WORKOS_API_KEY,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
            if token_resp.status_code != 200:
                raise ApiError(401, "enterprise_sso_failed", "SSO provider rejected the authorization code")
            payload = token_resp.json()
            profile = payload.get("profile")
            if not profile or not profile.get("id") or not profile.get("email"):
                raise ApiError(401, "enterprise_sso_failed", "SSO provider did not return a usable profile")
            return EnterpriseSsoProfile(
                provider_user_id=str(profile["id"]),
                email=profile["email"],
                organization_id=(
                    str(profile["organization_id"]) if profile.get("organization_id") else None
                ),
                connection_id=str(profile["connection_id"]) if profile.get("connection_id") else None,
            )


def get_enterprise_sso_client() -> EnterpriseSsoClient:
    """Factory selecting the configured backend (config.ENTERPRISE_SSO_PROVIDER).
    Overridable in tests via monkeypatch.setattr(enterprise_sso_client,
    "get_enterprise_sso_client", lambda: fake_client) -- same
    swap-a-module-function pattern get_http_client above uses, applied one
    layer up so tests exercise the router against a full fake
    EnterpriseSsoClient (FakeEnterpriseSsoClient in conftest.py) instead of
    a fake HTTP transport."""
    if settings.ENTERPRISE_SSO_PROVIDER == "workos":
        return WorkOSSsoClient()
    raise ApiError(500, "enterprise_sso_misconfigured", f"Unknown ENTERPRISE_SSO_PROVIDER: {settings.ENTERPRISE_SSO_PROVIDER}")
