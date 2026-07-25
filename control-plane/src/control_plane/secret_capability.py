"""Short-lived, session-scoped capability tokens for the secrets broker
(docs/SECRETS-DESIGN.md section 4).

A THIRD credential type, distinct from both existing ones (see deps.py's
module docstring for the first two): a dashboard JWT authenticates a human
user, a long-lived API key authenticates an account for the /v1/sandboxes
API. This token authenticates neither -- it authenticates *one sandbox
session*, for the narrow purpose of letting that session's sidecar resolve
the plaintext value of a secret it was explicitly granted, and nothing else.

Minted once, by the control plane, at session-create time (never re-minted,
never refreshed) and handed to `SandboxManager.create_session` alongside the
non-sensitive metadata (secret names, allowed_hosts) that already crosses
the manager-to-sidecar `/configure` call. Per the design doc's section 4:
this token, not the resolved secret value itself, is the only thing that
crosses that (TLS-protected, but still worth minimizing) plaintext-adjacent
hop -- worst case on a leaked token is impersonating that one session's own
already-granted secret access for the token's TTL, no worse in kind than the
plaintext SIDECAR_AUTH_TOKEN this project already accepts on that same path.

Bound to `session_id` at both issuance AND validation time (not just
issuance) -- the design doc's cross-tenant-isolation section calls this out
explicitly: a capability token must be unusable against any other session's
secrets even if somehow replayed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from .config import settings

CAPABILITY_TOKEN_TYPE = "secret_capability"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_capability_token(
    *, account_id: str, session_id: str, secret_names: list[str]
) -> str:
    """Mint a token scoped to exactly this (account, session, secret set)
    triple. `secret_names` is the session's full grant list -- the internal
    resolve endpoint additionally checks the requested name is a member of
    this list (see routers/internal_secrets.py), so a compromised sidecar
    can't use this token to fetch a secret the session was never granted
    even though the token itself is otherwise valid."""
    ttl = timedelta(seconds=settings.SECRETS_CAPABILITY_TOKEN_TTL_SECONDS)
    expires = _now() + ttl
    payload = {
        "type": CAPABILITY_TOKEN_TYPE,
        "account_id": account_id,
        "session_id": session_id,
        "secret_names": sorted(set(secret_names)),
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


class InvalidCapabilityToken(Exception):
    pass


def decode_capability_token(token: str, *, expected_session_id: str) -> dict[str, Any]:
    """Decode and validate a capability token, INCLUDING that it is bound to
    `expected_session_id` -- the caller must always pass the session_id the
    request itself claims to be for (e.g. a path/body parameter), never
    trust the token's own embedded session_id alone as "whichever session
    this is for". Raises InvalidCapabilityToken on any failure -- expired,
    malformed, wrong type, or wrong session -- so callers have exactly one
    failure mode to handle (a 404, per the design doc's "never distinguish
    not-granted from doesn't-exist" posture)."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidCapabilityToken(str(exc)) from exc

    if payload.get("type") != CAPABILITY_TOKEN_TYPE:
        raise InvalidCapabilityToken("not a secret capability token")
    if payload.get("session_id") != expected_session_id:
        raise InvalidCapabilityToken("token is not bound to this session")
    return payload
