"""Account signup and login, plus the opt-in dashboard-auth flows added for
issue #79: refresh-token rotation, password reset, and email verification.

Signup/login remain unconditional -- the v1 baseline this module already
shipped. The three additions below are each gated by their own settings
flag, off by default, per this repo's standard new-attack-surface
convention (BOXKITE_IMAGE_BUILDER_ENABLED, BOXKITE_VOLUMES_ENABLED,
enable_git_tools, ...):

- `BOXKITE_REFRESH_TOKENS_ENABLED` -- `POST /refresh`, `POST /logout`. When
  disabled, `TokenResponse.refresh_token` stays null and behavior is
  unchanged from before this issue: an access token simply expires and the
  caller re-authenticates via `POST /login`. When enabled, every
  `POST /refresh` call revokes the presented token and mints a brand new
  one (rotation, not reuse) -- presenting an already-revoked token again is
  treated as a replay/theft signal and revokes every refresh token on the
  account as a precaution (see `RefreshTokenRepository.revoke_all_for_account`).
- `BOXKITE_PASSWORD_RESET_ENABLED` -- `POST /password-reset/request`,
  `POST /password-reset/confirm`. Email delivery is stubbed behind
  `EmailSender` (see `email_sender.py`) -- this repo has no real mail
  transport to wire up -- but the token generation/validation/password-update
  logic here is real and fully covered by tests. `request` always returns
  the same response whether or not the email is registered (same
  anti-enumeration posture `/login`'s identical error already has). A
  successful `confirm` also revokes every outstanding refresh token for the
  account (if refresh tokens are enabled), since a password reset is
  exactly the situation where an existing session might be compromised.
- `BOXKITE_EMAIL_VERIFICATION_ENABLED` -- `POST /verify-email`,
  `POST /resend-verification`. Purely informational today:
  `Account.email_verified_at` is surfaced on `AccountOut` but no route
  gates access on it -- enforcing that is a deliberate, separate follow-up,
  so flipping this flag on doesn't retroactively lock out every
  pre-existing account (which all have `email_verified_at = NULL`).

All four new endpoints get their own rate-limit bucket (see config.py),
separate from `BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE`'s signup/login bucket.

OAuth/SSO login is explicitly NOT covered here -- tracked as a separate,
lower-priority follow-up per issue #79's acceptance criteria.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_user, get_email_sender_dep
from ..email_sender import EmailSender
from ..errors import ApiError
from ..models_orm import Account
from ..rate_limit import enforce_rate_limit
from ..repository import (
    AccountRepository,
    EmailVerificationTokenRepository,
    PasswordResetTokenRepository,
    RefreshTokenRepository,
)
from ..schemas import (
    AccountOut,
    EmailVerificationConfirmRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequestRequest,
    RefreshTokenRequest,
    SignupRequest,
    TokenResponse,
)
from ..security import (
    create_access_token,
    generate_secure_token,
    hash_password,
    hash_secret,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _require_flag(enabled: bool, feature_name: str) -> None:
    """Same 404-not-403 discipline `routers/images.py` uses for
    `BOXKITE_IMAGE_BUILDER_ENABLED` -- a deployment that hasn't opted into
    a feature exposes no functional trace of it beyond the bare route
    existing in the OpenAPI schema."""
    if not enabled:
        raise ApiError(404, "feature_disabled", f"{feature_name} is not enabled on this deployment.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reject_if_scim_deactivated(account: Account) -> None:
    """Blocks minting a NEW credential (login, refresh) for an account SCIM
    (Directory Sync, routers/scim.py) has deactivated. Checked here in
    addition to -- not instead of -- deps.py's ongoing-request gate: this
    gives an explicit, actionable error at the point of authentication,
    while deps.py is what actually revokes an ALREADY-issued JWT/API key on
    its very next use (the load-bearing property; a token minted before
    deactivation must stop working immediately, not just stop being
    reissuable)."""
    if account.scim_deactivated_at is not None:
        raise ApiError(
            403,
            "account_deactivated",
            "This account has been deactivated by your organization's administrator",
        )


def _is_expired(expires_at: datetime) -> bool:
    """SQLite (used in tests and zero-config local dev) round-trips
    `DateTime(timezone=True)` columns as naive datetimes -- comparing that
    directly against an aware `_utcnow()` raises TypeError. Every value this
    module writes is UTC to begin with (see `_utcnow()`'s use at every
    `create(...)` call site), so a naive value read back is always UTC too;
    normalize before comparing rather than assuming the caller already did."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= _utcnow()


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    description=(
        "Creates a new account from an email + password. Returns a short-lived "
        "session token for the dashboard UI — this is NOT the credential used "
        "for sandbox management; create a separate API key via POST /v1/api-keys "
        "for that."
    ),
)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    email_sender: EmailSender = Depends(get_email_sender_dep),
) -> TokenResponse:
    await enforce_rate_limit(request, bucket="signup", response=response)

    accounts = AccountRepository(db)
    if await accounts.get_by_email(str(body.email)) is not None:
        raise ApiError(409, "email_taken", "An account with this email already exists")

    account = await accounts.create(email=str(body.email), password_hash=hash_password(body.password))

    if settings.BOXKITE_EMAIL_VERIFICATION_ENABLED:
        await _send_verification_email_best_effort(db, account, email_sender)

    return await _issue_token_response(db, account)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in",
    description="Exchanges email + password for a short-lived dashboard session token.",
)
async def login(
    body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    await enforce_rate_limit(request, bucket="login", response=response)

    accounts = AccountRepository(db)
    account = await accounts.get_by_email(str(body.email))
    # Deliberately identical error for "no such account" and "wrong password"
    # so login failures never confirm whether an email is registered.
    if account is None:
        raise ApiError(401, "invalid_credentials", "Incorrect email or password")
    if account.password_hash is None:
        # A social-login-only account (GitHub/Google, see
        # docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §4.1) -- distinct,
        # actionable error rather than a generic "invalid credentials" or a
        # crash from passing None to verify_password.
        raise ApiError(
            401,
            "no_password_set",
            "This account has no password set -- sign in with GitHub or Google instead",
        )
    if not verify_password(body.password, account.password_hash):
        raise ApiError(401, "invalid_credentials", "Incorrect email or password")
    _reject_if_scim_deactivated(account)

    return await _issue_token_response(db, account)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate a refresh token for a new access token",
    description=(
        "Opt-in (BOXKITE_REFRESH_TOKENS_ENABLED). Exchanges a still-valid refresh token "
        "for a brand new access_token + refresh_token pair, revoking the presented one in "
        "the same request. Presenting an already-revoked token is treated as a replay "
        "signal and revokes every refresh token on the account."
    ),
)
async def refresh(
    body: RefreshTokenRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    _require_flag(settings.BOXKITE_REFRESH_TOKENS_ENABLED, "Refresh-token rotation")
    await enforce_rate_limit(
        request,
        bucket="refresh",
        limit=settings.BOXKITE_REFRESH_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    tokens = RefreshTokenRepository(db)
    row = await tokens.get_by_hash(hash_secret(body.refresh_token))
    if row is None or _is_expired(row.expires_at):
        raise ApiError(401, "invalid_refresh_token", "Refresh token is invalid or has expired")
    if row.revoked_at is not None:
        # The same raw token being presented twice means it was rotated out
        # (or explicitly logged out) once already -- a legitimate client
        # never does this, so treat it as evidence the token leaked and
        # revoke every refresh token on the account as a precaution.
        await tokens.revoke_all_for_account(row.account_id)
        raise ApiError(
            401,
            "refresh_token_reused",
            "This refresh token has already been used. All sessions for this account "
            "have been revoked as a precaution -- please log in again.",
        )

    await tokens.revoke(token_id=row.id)

    accounts = AccountRepository(db)
    account = await accounts.get_by_id(row.account_id)
    if account is None:
        raise ApiError(401, "invalid_refresh_token", "Account for this refresh token no longer exists")
    _reject_if_scim_deactivated(account)

    return await _issue_token_response(db, account)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token",
    description="Opt-in (BOXKITE_REFRESH_TOKENS_ENABLED). Revokes one refresh token immediately.",
)
async def logout(body: LogoutRequest, request: Request, db: AsyncSession = Depends(get_db)) -> None:
    _require_flag(settings.BOXKITE_REFRESH_TOKENS_ENABLED, "Refresh-token rotation")
    await enforce_rate_limit(request, bucket="logout", limit=settings.BOXKITE_REFRESH_RATE_LIMIT_PER_MINUTE)

    tokens = RefreshTokenRepository(db)
    row = await tokens.get_by_hash(hash_secret(body.refresh_token))
    # Revoking an unknown/already-revoked token is a silent no-op -- logout
    # must never leak whether a given refresh token string is/was valid.
    if row is not None and row.revoked_at is None:
        await tokens.revoke(token_id=row.id)
    return None


@router.post(
    "/password-reset/request",
    response_model=MessageResponse,
    summary="Request a password reset email",
    description=(
        "Opt-in (BOXKITE_PASSWORD_RESET_ENABLED). Always returns the same response whether "
        "or not the email is registered, so this endpoint cannot be used to enumerate accounts."
    ),
)
async def request_password_reset(
    body: PasswordResetRequestRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    email_sender: EmailSender = Depends(get_email_sender_dep),
) -> MessageResponse:
    _require_flag(settings.BOXKITE_PASSWORD_RESET_ENABLED, "Password reset")
    await enforce_rate_limit(
        request,
        bucket="password_reset_request",
        limit=settings.BOXKITE_PASSWORD_RESET_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    accounts = AccountRepository(db)
    account = await accounts.get_by_email(str(body.email))
    if account is not None:
        tokens = PasswordResetTokenRepository(db)
        await tokens.invalidate_active_for_account(account.id)
        raw_token, token_hash = generate_secure_token()
        expires_at = _utcnow() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_TTL_MINUTES)
        await tokens.create(account_id=account.id, token_hash=token_hash, expires_at=expires_at)
        await _send_password_reset_email_best_effort(email_sender, account.email, raw_token)

    return MessageResponse(message="If an account with that email exists, a password reset link has been sent.")


@router.post(
    "/password-reset/confirm",
    response_model=MessageResponse,
    summary="Confirm a password reset",
    description=(
        "Opt-in (BOXKITE_PASSWORD_RESET_ENABLED). Consumes a password-reset token minted by "
        "POST /password-reset/request and sets a new password. Also revokes every outstanding "
        "refresh token for the account, if refresh tokens are enabled."
    ),
)
async def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    _require_flag(settings.BOXKITE_PASSWORD_RESET_ENABLED, "Password reset")
    await enforce_rate_limit(
        request,
        bucket="password_reset_confirm",
        limit=settings.BOXKITE_PASSWORD_RESET_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    tokens = PasswordResetTokenRepository(db)
    row = await tokens.get_active_by_hash(hash_secret(body.token))
    if row is None:
        raise ApiError(400, "invalid_or_expired_token", "This password reset link is invalid or has expired.")

    accounts = AccountRepository(db)
    await accounts.update_password(account_id=row.account_id, password_hash=hash_password(body.new_password))
    await tokens.mark_used(token_id=row.id)
    await tokens.invalidate_active_for_account(row.account_id)

    if settings.BOXKITE_REFRESH_TOKENS_ENABLED:
        await RefreshTokenRepository(db).revoke_all_for_account(row.account_id)

    return MessageResponse(message="Password has been reset. Please log in with your new password.")


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    summary="Confirm email verification",
    description="Opt-in (BOXKITE_EMAIL_VERIFICATION_ENABLED).",
)
async def verify_email(
    body: EmailVerificationConfirmRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    _require_flag(settings.BOXKITE_EMAIL_VERIFICATION_ENABLED, "Email verification")
    await enforce_rate_limit(
        request,
        bucket="email_verify",
        limit=settings.BOXKITE_EMAIL_VERIFICATION_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    tokens = EmailVerificationTokenRepository(db)
    row = await tokens.get_active_by_hash(hash_secret(body.token))
    if row is None:
        raise ApiError(400, "invalid_or_expired_token", "This verification link is invalid or has expired.")

    await AccountRepository(db).mark_email_verified(row.account_id)
    await tokens.mark_used(token_id=row.id)
    await tokens.invalidate_active_for_account(row.account_id)

    return MessageResponse(message="Email verified.")


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    summary="Resend the verification email",
    description="Opt-in (BOXKITE_EMAIL_VERIFICATION_ENABLED). Requires a dashboard session token.",
)
async def resend_verification(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_account: Account = Depends(get_current_user),
    email_sender: EmailSender = Depends(get_email_sender_dep),
) -> MessageResponse:
    _require_flag(settings.BOXKITE_EMAIL_VERIFICATION_ENABLED, "Email verification")
    await enforce_rate_limit(
        request,
        bucket="email_verify_resend",
        subject=str(current_account.id),
        limit=settings.BOXKITE_EMAIL_VERIFICATION_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    if current_account.email_verified_at is not None:
        return MessageResponse(message="Email is already verified.")

    await _send_verification_email_best_effort(db, current_account, email_sender)
    return MessageResponse(message="Verification email sent.")


async def issue_refresh_token(db: AsyncSession, account_id: str) -> str:
    """Mints and persists a new refresh token for `account_id`, returning
    the raw (unhashed) value. Shared with routers/social_login.py's
    `_finish_login` -- OAuth login mints one exactly like password login
    does, so a dashboard session's expiry doesn't depend on which login
    method was used."""
    raw_token, token_hash = generate_secure_token()
    expires_at = _utcnow() + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    await RefreshTokenRepository(db).create(account_id=account_id, token_hash=token_hash, expires_at=expires_at)
    return raw_token


async def _issue_token_response(db: AsyncSession, account: Account) -> TokenResponse:
    token, expires_in = create_access_token(account_id=account.id, email=account.email)

    refresh_token_raw: str | None = None
    if settings.BOXKITE_REFRESH_TOKENS_ENABLED:
        refresh_token_raw = await issue_refresh_token(db, account.id)

    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        refresh_token=refresh_token_raw,
        account=AccountOut.model_validate(account),
    )


async def _send_password_reset_email_best_effort(email_sender: EmailSender, to_email: str, raw_token: str) -> None:
    """Email delivery must never turn into a 500, and must never let the
    caller distinguish "email sent" from "email delivery failed" -- either
    would leak information (or an outage signal) through an
    otherwise-anonymous endpoint."""
    try:
        await email_sender.send_password_reset_email(to_email=to_email, reset_token=raw_token)
    except Exception:
        logger.exception("[control-plane] password-reset email delivery failed")


async def _send_verification_email_best_effort(
    db: AsyncSession, account: Account, email_sender: EmailSender
) -> None:
    tokens = EmailVerificationTokenRepository(db)
    await tokens.invalidate_active_for_account(account.id)
    raw_token, token_hash = generate_secure_token()
    expires_at = _utcnow() + timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_TTL_HOURS)
    await tokens.create(account_id=account.id, token_hash=token_hash, expires_at=expires_at)
    try:
        await email_sender.send_verification_email(to_email=account.email, verification_token=raw_token)
    except Exception:
        logger.exception("[control-plane] verification email delivery failed")
