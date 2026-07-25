"""Account identity via API key -- the same trust boundary
`get_current_account_via_api_key` already grants for /v1/sandboxes and
/v1/usage, exposed as a small "who am I" lookup for `boxkite whoami`.

Also hosts the JWT-authenticated, read-only mirrors of the API-key routes
(`/me`, `/sandboxes`, `/usage`) added for the browser dashboard: a browser
session authenticates via login (JWT), not by pasting a long-lived API key
into browser JS, but `/v1/sandboxes` and `/v1/usage` only ever accepted an
API key. Rather than widen those routes' auth requirement -- which would
blur the deliberate two-credential-type boundary documented in deps.py --
these mirrors resolve the account from the dashboard JWT
(`get_current_user`) instead and return the identical response shapes.
Strictly additive and read-only, with one deliberate exception: no
JWT-authenticated way to create or destroy a sandbox exists directly on
this router, but `POST /sandbox-create-token` (GitHub issue #221) mints a
short-lived, single-use token a JWT-authenticated session can redeem at
`POST /v1/sandboxes` -- see `security.py`'s `create_sandbox_create_token`
and `deps.py`'s `get_current_account_via_api_key_or_sandbox_create_token`.
"""

from __future__ import annotations

from typing import Literal

from boxkite.command_whitelist import _compile_patterns, _normalize_rules
from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key, get_current_user, get_usage_policy
from ..errors import ApiError
from ..models_orm import Account
from ..repository import SandboxSessionRepository
from ..routers.sandboxes import _to_out
from ..routers.social_login import _require_github_enabled, _require_google_enabled
from ..schemas import (
    AccountLinkStartResponse,
    AccountOut,
    AllowedCommandsRequest,
    AllowedCommandsResponse,
    SandboxCreateTokenResponse,
    SandboxSessionOut,
    UsageSummary,
)
from ..security import create_account_link_intent_token, create_sandbox_create_token
from ..usage_policy import UsagePolicy

router = APIRouter(prefix="/v1/account", tags=["account"])


def _validate_allowed_commands_request(body: AllowedCommandsRequest) -> list:
    """Fail closed at write time -- unlike command_whitelist.py's own
    runtime behavior of silently skipping an invalid pattern, a rule a user
    is persisting should be rejected outright if it won't actually compile,
    so they get immediate feedback instead of a silently-inert rule."""
    if len(body.rules) > settings.BOXKITE_MAX_ALLOWLIST_RULES:
        raise ApiError(
            400,
            "too_many_rules",
            f"At most {settings.BOXKITE_MAX_ALLOWLIST_RULES} allowlist rules are allowed per account.",
        )

    raw_rules = [r if isinstance(r, str) else r.model_dump() for r in body.rules]
    for rule in raw_rules:
        if isinstance(rule, dict):
            for pattern in (*rule.get("args_allow", []), *rule.get("args_deny", [])):
                if len(pattern) > settings.BOXKITE_MAX_ALLOWLIST_PATTERN_LENGTH:
                    raise ApiError(
                        400,
                        "pattern_too_long",
                        f"Argument patterns must be at most "
                        f"{settings.BOXKITE_MAX_ALLOWLIST_PATTERN_LENGTH} characters "
                        f"(command {rule['command']!r}).",
                    )

    for rule in raw_rules:
        if not isinstance(rule, dict):
            continue
        for kind in ("args_allow", "args_deny"):
            patterns = rule.get(kind) or []
            if len(_compile_patterns(patterns)) != len(patterns):
                raise ApiError(
                    400,
                    "invalid_pattern",
                    f"One or more {kind} patterns for command {rule['command']!r} "
                    "are not valid regular expressions.",
                )

    # Confirms the whole payload normalizes cleanly end to end (e.g. no
    # entry silently produces zero rules) using the exact same parser
    # enforcement will use later, so write-time and enforcement-time
    # agree on what's valid.
    if not _normalize_rules(raw_rules):
        raise ApiError(400, "invalid_rules", "No valid allowlist rules were found in the request.")

    return raw_rules


@router.get(
    "",
    response_model=AccountOut,
    summary="Get the authenticated account",
    description=(
        "Returns the account identity for the API key used to authenticate -- "
        "email, id, and when it was created. Requires an API key, not a "
        "dashboard session token, same as every other /v1/sandboxes route."
    ),
)
async def get_account(account: Account = Depends(get_current_account_via_api_key)) -> AccountOut:
    return AccountOut.model_validate(account)


@router.get(
    "/me",
    response_model=AccountOut,
    summary="Get the authenticated account (dashboard JWT)",
    description=(
        "Same response shape as GET /v1/account, but resolves the account "
        "from a dashboard session JWT (POST /v1/auth/login) instead of an "
        "API key -- for the browser dashboard, which authenticates via "
        "login rather than by holding a long-lived API key in browser JS."
    ),
)
async def get_account_me(account: Account = Depends(get_current_user)) -> AccountOut:
    return AccountOut.model_validate(account)


@router.post(
    "/link/{provider}/start",
    response_model=AccountLinkStartResponse,
    summary="Start linking a GitHub/Google identity to your account",
    description=(
        "Mints a short-lived, single-purpose token proving the current dashboard session asked "
        "to link <provider>. The caller navigates the browser (not a fetch) to "
        "GET /v1/auth/{provider}/start?link_token=<this>&next=... to complete it -- a top-level "
        "redirect can't carry this endpoint's own Authorization header, so the link intent has "
        "to be proven a different way. On success, the OAuth callback links the identity onto "
        "THIS account specifically (not whichever account happens to share the profile's email -- "
        "see routers/social_login.py's `_link_provider_to_account`)."
    ),
)
async def start_link_provider(
    provider: Literal["github", "google"],
    account: Account = Depends(get_current_user),
) -> AccountLinkStartResponse:
    if provider == "github":
        _require_github_enabled()
    else:
        _require_google_enabled()
    return AccountLinkStartResponse(link_token=create_account_link_intent_token(account_id=account.id, provider=provider))


@router.delete(
    "/link/{provider}",
    status_code=204,
    summary="Unlink a GitHub/Google identity from your account",
    description=(
        "Refuses with 400 last_login_method if this is the account's only way to log in "
        "(no password, no other linked provider, no enterprise SSO) -- unlinking it would "
        "permanently lock the account out."
    ),
)
async def unlink_provider(
    provider: Literal["github", "google"],
    account: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    id_field = "github_id" if provider == "github" else "google_id"
    other_provider_field = "google_id" if provider == "github" else "github_id"
    if getattr(account, id_field) is None:
        raise ApiError(400, "not_linked", f"No {provider.capitalize()} identity is linked to your account.")
    remaining_login_methods = (
        account.password_hash is not None,
        getattr(account, other_provider_field) is not None,
        account.sso_provider_user_id is not None,
    )
    if not any(remaining_login_methods):
        raise ApiError(
            400,
            "last_login_method",
            f"Can't unlink {provider.capitalize()} -- it's your only way to log in. "
            f"Set a password or link another provider first.",
        )
    setattr(account, id_field, None)
    db.add(account)
    await db.commit()
    return Response(status_code=204)


@router.get(
    "/sandboxes",
    response_model=list[SandboxSessionOut],
    summary="List your sandbox sessions (dashboard JWT)",
    description=(
        "Same response shape and `active_only` query param as GET "
        "/v1/sandboxes, but resolves the account from a dashboard session "
        "JWT instead of an API key. Read-only: this mirror has no "
        "JWT-authenticated create or destroy route -- use the CLI or API "
        "with an API key to manage sandboxes."
    ),
)
async def list_account_sandboxes(
    active_only: bool = False,
    account: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SandboxSessionOut]:
    rows = await SandboxSessionRepository(db).list_for_account(account_id=account.id, active_only=active_only)
    return [_to_out(r) for r in rows]


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Check current usage against fair-use limits (dashboard JWT)",
    description=(
        "Same response shape as GET /v1/usage, but resolves the account "
        "from a dashboard session JWT instead of an API key."
    ),
)
async def get_account_usage(
    account: Account = Depends(get_current_user),
    policy: UsagePolicy = Depends(get_usage_policy),
    db: AsyncSession = Depends(get_db),
) -> UsageSummary:
    active_count = await SandboxSessionRepository(db).count_active_for_account(account.id)
    hours_used = await policy.monthly_hours_used(account.id)
    return UsageSummary(
        monthly_sandbox_hours_used=round(hours_used, 4),
        monthly_sandbox_hours_limit=settings.BOXKITE_FREE_MONTHLY_SANDBOX_HOURS,
        concurrent_sandboxes=active_count,
        concurrent_sandboxes_limit=settings.BOXKITE_MAX_CONCURRENT_SANDBOXES,
    )


@router.post(
    "/sandbox-create-token",
    response_model=SandboxCreateTokenResponse,
    summary="Mint a short-lived, single-use token for POST /v1/sandboxes",
    description=(
        f"Mints a short-lived (default {settings.BOXKITE_SANDBOX_CREATE_TOKEN_TTL_SECONDS}s, "
        "BOXKITE_SANDBOX_CREATE_TOKEN_TTL_SECONDS), single-use token scoped to the "
        "authenticated account. Pass the returned `token` as the `Authorization: Bearer` "
        "credential on `POST /v1/sandboxes` immediately after minting it -- it is consumed "
        "on first use and expires quickly even if never redeemed. Lets a dashboard session "
        "(authenticated here via a normal login JWT) create a sandbox without the user ever "
        "pasting a long-lived API key into the browser (GitHub issue #221). Every other "
        "`/v1/sandboxes/*` route still requires a real API key -- this token is only "
        "accepted by the create route."
    ),
)
async def mint_sandbox_create_token(account: Account = Depends(get_current_user)) -> SandboxCreateTokenResponse:
    token, expires_at = create_sandbox_create_token(
        account_id=account.id, ttl_seconds=settings.BOXKITE_SANDBOX_CREATE_TOKEN_TTL_SECONDS
    )
    return SandboxCreateTokenResponse(token=token, expires_at=expires_at)


@router.get(
    "/allowed-commands",
    response_model=AllowedCommandsResponse,
    summary="Get your custom command allowlist for hosted exec",
    description=(
        "Returns the account's persisted command allowlist for "
        "POST /v1/sandboxes/{id}/exec, or an empty list if none is set "
        "(unrestricted -- the default for every account). This is NOT a "
        "sandbox-escape boundary: allowing a general-purpose interpreter "
        "(python3, bash, node) permits arbitrary code through it. It's an "
        "opt-in guardrail layered on top of, not a replacement for, pod "
        "isolation."
    ),
)
async def get_allowed_commands(
    account: Account = Depends(get_current_account_via_api_key),
) -> AllowedCommandsResponse:
    return AllowedCommandsResponse(rules=account.custom_allowed_commands or [])


@router.put(
    "/allowed-commands",
    response_model=AllowedCommandsResponse,
    summary="Set your custom command allowlist for hosted exec",
    description=(
        "Replaces the account's command allowlist enforced on every future "
        "POST /v1/sandboxes/{id}/exec call. Each rule is either a plain "
        "command name (unconstrained) or {command, args_allow?, args_deny?} "
        "with Python regexes matched against the joined argument string. "
        "Rejects the request outright if any pattern fails to compile, "
        f"exceeds {settings.BOXKITE_MAX_ALLOWLIST_PATTERN_LENGTH} characters, or the rule "
        f"count exceeds {settings.BOXKITE_MAX_ALLOWLIST_RULES}. Use DELETE to clear back to "
        "unrestricted."
    ),
)
async def set_allowed_commands(
    body: AllowedCommandsRequest,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> AllowedCommandsResponse:
    rules = _validate_allowed_commands_request(body)
    account.custom_allowed_commands = rules
    db.add(account)
    await db.commit()
    return AllowedCommandsResponse(rules=rules)


@router.delete(
    "/allowed-commands",
    status_code=204,
    summary="Clear your custom command allowlist (back to unrestricted)",
)
async def clear_allowed_commands(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    account.custom_allowed_commands = None
    db.add(account)
    await db.commit()
    return Response(status_code=204)
