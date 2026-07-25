"""Shared helpers for the `/oauth/*`-scoped login-session cookie set by
`routers/oauth.py`'s consent screen and read back by both `routers/oauth.py`
and `routers/social_login.py` (the GitHub/Google callback's `next`-driven
resume path) -- docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.4.

Split out from `routers/oauth.py` itself so `routers/social_login.py`
doesn't need to import a sibling router's internals.
"""

from __future__ import annotations

import jwt
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .repository import AccountRepository
from .security import decode_oauth_login_session_token

OAUTH_LOGIN_SESSION_COOKIE = "boxkite_oauth_session"


def set_login_session_cookie(response: Response, *, token: str, ttl_seconds: int) -> None:
    response.set_cookie(
        OAUTH_LOGIN_SESSION_COOKIE,
        token,
        max_age=ttl_seconds,
        httponly=True,
        secure=not settings.is_dev_environment,
        samesite="lax",
        path="/oauth",
    )


async def account_from_login_cookie(request: Request, db: AsyncSession):
    """Resolves the `/oauth/*`-scoped login-session cookie back to an
    Account, or None if there is no cookie, it's invalid/expired, or the
    account no longer exists. Also returns None for a SCIM (Directory
    Sync)-deactivated account -- this cookie is itself an already-issued
    credential (minted by `authorize_login`, or by `_finish_login` for the
    GitHub/Google/enterprise-SSO callbacks), and both of this function's
    callers (`GET /oauth/authorize`, `POST /oauth/authorize/decide`)
    already treat a None return as "not logged in, show the login page /
    redirect to it" -- reusing that existing fallback is simpler and safer
    than adding a second, differently-shaped failure mode to either call
    site for what is functionally the same "this session is no longer
    valid" outcome. Token exchange at `/oauth/token` independently rejects
    a deactivated account's authorization code (see routers/oauth.py's
    `_account_deactivated`), so this is defense in depth, not the only
    place this is enforced."""
    token = request.cookies.get(OAUTH_LOGIN_SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = decode_oauth_login_session_token(token)
    except jwt.PyJWTError:
        return None
    account = await AccountRepository(db).get_by_id(str(payload.get("sub", "")))
    if account is not None and account.scim_deactivated_at is not None:
        return None
    return account
