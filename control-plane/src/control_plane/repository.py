"""Repository pattern: all raw DB access lives here, behind small,
purpose-built async methods. Routers and the usage policy layer never issue
SQLAlchemy queries directly — they call these methods.

This is also the layer that makes cross-tenant isolation structural rather
than a matter of remembering to filter: every sandbox-session lookup method
takes `account_id` and folds it directly into the WHERE clause, so it is not
possible to fetch or mutate a row by id alone. See `get_session_for_account`
and `list_sessions_for_account` below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from boxkite.audit import GENESIS_HASH, compute_row_hash

from .audit_chain import canonical_started_at
from .audit_chain_lock import get_exec_log_chain_lock
from .models_orm import (
    Account,
    AdminAccessLog,
    ApiKey,
    EmailVerificationToken,
    ExecLogEntry,
    McpConnection,
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthToken,
    PasswordResetToken,
    RefreshToken,
    RevokedPreviewToken,
    SandboxImage,
    SandboxSession,
    SandboxVolume,
    Secret,
    Snapshot,
    WebhookDelivery,
    WebhookSubscription,
    _new_uuid,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class AccountRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, *, email: str, password_hash: str) -> Account:
        # Normalize case here (not just in get_by_email) so the DB's unique
        # index on `email` actually enforces one account per mailbox --
        # most mail systems treat `User@example.com`/`user@example.com` as
        # the same address, and the exact-string unique constraint alone
        # doesn't catch that.
        account = Account(email=_normalize_email(email), password_hash=password_hash)
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def create_social(
        self, *, email: str, github_id: str | None = None, google_id: str | None = None
    ) -> Account:
        """Auto-register a new account from a GitHub/Google login with no
        password set (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §4). Callers
        must have already checked `get_by_email`/`get_by_github_id`/
        `get_by_google_id` to rule out the account-takeover-by-email-match
        case -- this method itself does not re-check, so it must only be
        called from the "no existing account at all" branch."""
        account = Account(
            email=_normalize_email(email), password_hash=None, github_id=github_id, google_id=google_id
        )
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def get_by_email(self, email: str) -> Account | None:
        result = await self.db.execute(select(Account).where(Account.email == _normalize_email(email)))
        return result.scalar_one_or_none()

    async def get_by_id(self, account_id: str) -> Account | None:
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        return result.scalar_one_or_none()

    async def get_by_github_id(self, github_id: str) -> Account | None:
        result = await self.db.execute(select(Account).where(Account.github_id == github_id))
        return result.scalar_one_or_none()

    async def get_by_google_id(self, google_id: str) -> Account | None:
        result = await self.db.execute(select(Account).where(Account.google_id == google_id))
        return result.scalar_one_or_none()

    async def get_by_sso_subject_id(self, sso_provider_user_id: str) -> Account | None:
        result = await self.db.execute(
            select(Account).where(Account.sso_provider_user_id == sso_provider_user_id)
        )
        return result.scalar_one_or_none()

    async def create_sso(
        self,
        *,
        email: str,
        sso_provider_user_id: str,
        sso_organization_id: str | None = None,
        sso_connection_id: str | None = None,
    ) -> Account:
        """Auto-register a new account from an enterprise SSO login with no
        password set (docs/ENTERPRISE-SSO-DESIGN.md §5). Same contract as
        `create_social` -- callers must have already checked `get_by_email`/
        `get_by_sso_subject_id` to rule out the account-takeover-by-email-match
        case; this method does not re-check."""
        account = Account(
            email=_normalize_email(email),
            password_hash=None,
            sso_provider_user_id=sso_provider_user_id,
            sso_organization_id=sso_organization_id,
            sso_connection_id=sso_connection_id,
        )
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def link_provider_identity(
        self, account: Account, *, github_id: str | None = None, google_id: str | None = None
    ) -> Account:
        """Links a verified GitHub/Google identity onto an existing account
        matched by email (routers/social_login.py's `_resolve_or_create_account`).
        Safe specifically because the caller only reaches here after the
        provider itself reports the email as verified for this identity --
        proof of current inbox control, the same proof password-reset flows
        rely on -- not just a claimed value an attacker could supply."""
        if github_id is not None:
            account.github_id = github_id
        if google_id is not None:
            account.google_id = google_id
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def update_password(self, *, account_id: str, password_hash: str) -> None:
        """Used by POST /v1/auth/password-reset/confirm. Does not touch
        anything else on the row -- the caller is separately responsible
        for invalidating outstanding refresh/reset tokens (see
        routers/auth.py)."""
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is not None:
            account.password_hash = password_hash
            await self.db.commit()

    async def mark_email_verified(self, account_id: str) -> None:
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is not None and account.email_verified_at is None:
            account.email_verified_at = _utcnow()
            await self.db.commit()

    # ── SCIM 2.0 provisioning via WorkOS Directory Sync (Phase 2 of issue
    # #126, docs/ENTERPRISE-SSO-DESIGN.md) -- routers/scim.py is the only
    # caller of everything below. ────────────────────────────────────────
    async def get_by_scim_directory_user_id(self, scim_directory_user_id: str) -> Account | None:
        result = await self.db.execute(
            select(Account).where(Account.scim_directory_user_id == scim_directory_user_id)
        )
        return result.scalar_one_or_none()

    async def create_scim(
        self, *, email: str, scim_directory_user_id: str, sso_organization_id: str | None = None
    ) -> Account:
        """Auto-register a new account "shell" from a `dsync.user.created`/
        `dsync.user.updated` webhook event, with no password set -- same
        contract as `create_sso`/`create_social`: the caller
        (routers/scim.py) must have already checked `get_by_email` to rule
        out the account-takeover-by-email-match case; this method does not
        re-check. Deliberately does NOT set `sso_provider_user_id` -- that
        column is only ever set by an actual interactive SSO login (Phase
        1), never by SCIM alone (see Account's own docstring on why the two
        WorkOS identifiers are kept distinct)."""
        account = Account(
            email=_normalize_email(email),
            password_hash=None,
            scim_directory_user_id=scim_directory_user_id,
            sso_organization_id=sso_organization_id,
        )
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def update_scim_profile(
        self, *, account_id: str, email: str, sso_organization_id: str | None
    ) -> None:
        """Applies a `dsync.user.updated` event's email/organization to an
        already-linked account. Conflict-safe on email: if the directory's
        new email now collides with a DIFFERENT existing account, this
        leaves the stored email untouched rather than raising or silently
        reassigning it -- a webhook handler has no browser to show a 409
        to, and letting one account's directory update silently steal
        another account's email would be a real cross-account bug, not
        just a cosmetic one."""
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is None:
            return
        normalized_email = _normalize_email(email)
        if normalized_email != account.email:
            conflict = await self.get_by_email(normalized_email)
            if conflict is None or conflict.id == account.id:
                account.email = normalized_email
        if sso_organization_id and sso_organization_id != account.sso_organization_id:
            account.sso_organization_id = sso_organization_id
        await self.db.commit()

    async def mark_scim_deactivated(self, account_id: str) -> None:
        """Sets `scim_deactivated_at` if not already set -- idempotent, so a
        redelivered/duplicate webhook event is a harmless no-op rather than
        overwriting an earlier, more accurate deactivation timestamp."""
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is not None and account.scim_deactivated_at is None:
            account.scim_deactivated_at = _utcnow()
            await self.db.commit()

    async def mark_scim_reactivated(self, account_id: str) -> None:
        """Clears `scim_deactivated_at` when a `dsync.user.updated` event
        reports the directory user is active again (an IT admin
        re-enabling someone they'd previously suspended) -- symmetric with
        `mark_scim_deactivated` above."""
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is not None and account.scim_deactivated_at is not None:
            account.scim_deactivated_at = None
            await self.db.commit()

    async def link_sso_identity(
        self,
        *,
        account_id: str,
        sso_provider_user_id: str,
        sso_organization_id: str | None,
        sso_connection_id: str | None,
    ) -> Account:
        """Attaches an interactive SSO identity to an account that was
        previously only SCIM-provisioned (routers/enterprise_sso.py's
        `_resolve_or_create_account` -- see that function's docstring for
        why an email match is trusted to auto-link ONLY in this specific
        case, unlike the GitHub/Google/generic-SSO email-collision
        refusal). Only ever called against an account this codebase has
        already verified has no password/github_id/google_id and a non-null
        `scim_directory_user_id` -- this method itself does not re-check."""
        result = await self.db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is None:
            raise ValueError(f"link_sso_identity: no account with id {account_id!r}")
        account.sso_provider_user_id = sso_provider_user_id
        if sso_organization_id:
            account.sso_organization_id = sso_organization_id
        if sso_connection_id:
            account.sso_connection_id = sso_connection_id
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def count_total(self) -> int:
        """Total account count -- backs the admin cluster-metrics endpoint's
        `total_accounts` field (docs/ADMIN-ROLE-DESIGN.md). No account_id
        scoping by design: this method is only ever called from the
        admin-gated route, never a normal account-scoped one."""
        result = await self.db.execute(select(func.count()).select_from(Account))
        return int(result.scalar_one())

    async def list_all(self, *, limit: int, offset: int) -> list[Account]:
        """Unscoped, paginated account listing -- ONLY for the admin
        cluster-metrics endpoint's per-account breakdown
        (docs/ADMIN-ROLE-DESIGN.md). Every other AccountRepository method
        above is single-row lookup by id/email; this is the one
        deliberate, admin-route-only exception to "never list across
        accounts," the same kind of narrow exception
        `SandboxSessionRepository.get_by_id_unscoped` already documents for
        its own callers."""
        result = await self.db.execute(
            select(Account).order_by(Account.created_at).limit(limit).offset(offset)
        )
        return list(result.scalars().all())


class ApiKeyRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self, *, account_id: str, name: str, prefix: str, key_hash: str, role: str = "admin"
    ) -> ApiKey:
        key = ApiKey(account_id=account_id, name=name, prefix=prefix, key_hash=key_hash, role=role)
        self.db.add(key)
        await self.db.commit()
        await self.db.refresh(key)
        return key

    async def list_for_account(self, account_id: str) -> list[ApiKey]:
        result = await self.db.execute(
            select(ApiKey).where(ApiKey.account_id == account_id).order_by(ApiKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None:
        """Look up a non-revoked key by its hash. Used on every API-key-authenticated request."""
        result = await self.db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def touch_last_used(self, key_id: str) -> None:
        """Stamp last_used_at = now on successful authentication. Best-effort
        bookkeeping -- callers should not fail the request if this fails."""
        result = await self.db.execute(select(ApiKey).where(ApiKey.id == key_id))
        key = result.scalar_one_or_none()
        if key is not None:
            key.last_used_at = _utcnow()
            await self.db.commit()

    async def get_by_id_for_account(self, *, key_id: str, account_id: str) -> ApiKey | None:
        """Fetch a key by id, scoped to `account_id` -- used to recover a
        takeover token's minting key's display `name` (GitHub issue #132
        design doc §5/§9 audit-identity threading) without trusting a raw
        `api_key_id` claim alone: a token forged/tampered to reference
        another account's key id simply finds nothing here, same structural
        cross-tenant guarantee `revoke` below already provides."""
        result = await self.db.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def revoke(self, *, account_id: str, key_id: str) -> bool:
        """Revoke a key, scoped to account_id — an account can only revoke its own keys."""
        result = await self.db.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.account_id == account_id)
        )
        key = result.scalar_one_or_none()
        if key is None or key.revoked_at is not None:
            return False
        key.revoked_at = _utcnow()
        await self.db.commit()
        return True


class SandboxSessionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self, *, session_id: str, account_id: str, pod_name: str | None, label: str | None = None
    ) -> SandboxSession:
        row = SandboxSession(id=session_id, account_id=account_id, pod_name=pod_name, label=label)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def delete_row(self, session_id: str) -> None:
        """Hard-delete a bookkeeping row (used to roll back a failed create)."""
        result = await self.db.execute(select(SandboxSession).where(SandboxSession.id == session_id))
        row = result.scalar_one_or_none()
        if row is not None:
            await self.db.delete(row)
            await self.db.commit()

    async def set_pod_name(self, session_id: str, pod_name: str | None) -> None:
        """Fill in the real pod_name once SandboxManager.create_session()
        returns -- used by the reserve-then-create pattern in
        UsagePolicy.create_session, where the row is inserted with
        pod_name=None to reserve a concurrency-limit slot *before* the slow
        K8s call runs, then updated after."""
        result = await self.db.execute(select(SandboxSession).where(SandboxSession.id == session_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.pod_name = pod_name
            await self.db.commit()

    async def get_for_account(self, *, session_id: str, account_id: str) -> SandboxSession | None:
        """Fetch a session scoped to account_id. Returns None for sessions
        owned by a different account — the caller cannot distinguish
        "doesn't exist" from "belongs to someone else", which is exactly the
        structural cross-tenant guarantee this table needs to provide."""
        result = await self.db.execute(
            select(SandboxSession).where(
                SandboxSession.id == session_id, SandboxSession.account_id == account_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_unscoped(self, session_id: str) -> SandboxSession | None:
        """Fetch a session by id alone, with NO account_id scoping.

        This is a deliberate, narrow exception to this module's own
        docstring guarantee ("not possible to fetch or mutate a row by id
        alone") -- used ONLY by the public, unauthenticated (token-signed,
        not API-key) preview-proxy route in routers/sandboxes.py
        (docs/NETWORK-INGRESS-DESIGN.md). That route has no account context
        to scope by: the caller of a shared preview link was never asked
        for an API key. Ownership there is enforced a different way instead
        -- the signed preview token itself is minted only by the
        API-key-authenticated, account-scoped `POST .../preview/{port}`
        route (which DOES use `get_for_account` above), and the token binds
        the exact session_id + port it was minted for, so a forged or
        reused-across-sessions token still fails signature/claim validation
        before this method is ever called. Do not use this method from any
        other route.
        """
        result = await self.db.execute(select(SandboxSession).where(SandboxSession.id == session_id))
        return result.scalar_one_or_none()

    async def list_for_account(self, *, account_id: str, active_only: bool = False) -> list[SandboxSession]:
        query = select(SandboxSession).where(SandboxSession.account_id == account_id)
        if active_only:
            query = query.where(SandboxSession.destroyed_at.is_(None))
        query = query.order_by(SandboxSession.created_at.desc())
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_active_for_account(self, account_id: str) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(SandboxSession)
            .where(SandboxSession.account_id == account_id, SandboxSession.destroyed_at.is_(None))
        )
        return int(result.scalar_one())

    async def count_active_total(self) -> int:
        """Cluster-wide active count across ALL accounts -- backs the
        global concurrent-sandbox cap, independent of any one account's
        own (much smaller) per-account cap."""
        result = await self.db.execute(
            select(func.count()).select_from(SandboxSession).where(SandboxSession.destroyed_at.is_(None))
        )
        return int(result.scalar_one())

    async def count_active_by_account(self) -> dict[str, int]:
        """Active-session count grouped by account_id, across ALL accounts
        -- backs the admin cluster-metrics endpoint's per-account breakdown
        (docs/ADMIN-ROLE-DESIGN.md). Admin-route-only, same "one deliberate
        unscoped exception" posture as AccountRepository.list_all above."""
        result = await self.db.execute(
            select(SandboxSession.account_id, func.count())
            .where(SandboxSession.destroyed_at.is_(None))
            .group_by(SandboxSession.account_id)
        )
        return {account_id: count for account_id, count in result.all()}

    async def sessions_created_since_all(self, *, since: datetime) -> list[SandboxSession]:
        """Same as sessions_created_since, but across ALL accounts, no
        account_id scoping -- backs the admin cluster-metrics endpoint's
        cluster-wide monthly-hours total (docs/ADMIN-ROLE-DESIGN.md).
        Admin-route-only."""
        result = await self.db.execute(
            select(SandboxSession).where(SandboxSession.created_at >= since)
        )
        return list(result.scalars().all())

    async def mark_destroyed(
        self, *, session_id: str, duration_seconds: float, reason: str
    ) -> None:
        result = await self.db.execute(select(SandboxSession).where(SandboxSession.id == session_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.destroyed_at = _utcnow()
        row.duration_seconds = duration_seconds
        row.destroyed_reason = reason
        await self.db.commit()

    async def sessions_created_since(self, *, account_id: str, since: datetime) -> list[SandboxSession]:
        """All sessions (active or destroyed) for this account with any
        overlap with the window starting at `since` — used for monthly
        usage accounting (see usage_policy.py)."""
        result = await self.db.execute(
            select(SandboxSession).where(
                SandboxSession.account_id == account_id,
                SandboxSession.created_at >= since,
            )
        )
        return list(result.scalars().all())

    async def list_active_older_than(self, *, cutoff: datetime) -> list[SandboxSession]:
        """All still-active sessions across every account created before
        `cutoff` — used by the reaper to enforce BOXKITE_MAX_SESSION_MINUTES."""
        result = await self.db.execute(
            select(SandboxSession).where(
                SandboxSession.destroyed_at.is_(None), SandboxSession.created_at <= cutoff
            )
        )
        return list(result.scalars().all())

    async def list_active_older_than_for_account(
        self, *, account_id: str, cutoff: datetime
    ) -> list[SandboxSession]:
        """Same as `list_active_older_than`, scoped to one account — used by
        the reaper to ALSO reap the demo-playground account's sessions on
        their own much shorter BOXKITE_DEMO_LIFETIME_MINUTES cutoff,
        independent of the global BOXKITE_MAX_SESSION_MINUTES cutoff every
        other account gets (see reaper.py). Without this, a demo session's
        bookkeeping row would sit "active" — still counting against
        BOXKITE_DEMO_MAX_CONCURRENT — for up to BOXKITE_MAX_SESSION_MINUTES
        even though its pod was already killed minutes earlier by its own
        K8s activeDeadlineSeconds."""
        result = await self.db.execute(
            select(SandboxSession).where(
                SandboxSession.destroyed_at.is_(None),
                SandboxSession.account_id == account_id,
                SandboxSession.created_at <= cutoff,
            )
        )
        return list(result.scalars().all())


class PreviewTokenRevocationRepository:
    """Denylist read/write for network-ingress preview-URL tokens
    (docs/NETWORK-INGRESS-DESIGN.md) -- see `RevokedPreviewToken`'s own
    docstring in models_orm.py for why revocation is a denylist rather than
    a delete, and how `expires_at` is derived.

    Deliberately not scoped by `account_id` the way every other repository
    in this module is: `is_revoked` is called from the public, unauthenticated
    proxy route (same trust-boundary shape as
    `SandboxSessionRepository.get_by_id_unscoped`), which has no account
    context to scope by. Ownership is enforced upstream instead -- the mint
    route only lets an account revoke a `jti` embedded in a token minted for
    a session it already owns (see `revoke_preview_url` in
    routers/sandboxes.py, which calls `_get_active_session_or_404` first,
    exactly like the mint route does).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def revoke(self, *, jti: str, session_id: str, port: int, expires_at: datetime) -> None:
        """Add `jti` to the denylist, idempotently (revoking an
        already-revoked or unknown jti is a harmless no-op, not an error --
        this route never needs to distinguish "was this ever a real,
        still-live token" from "already revoked", since both cases have the
        exact same desired end state: `jti` is now denylisted).

        Opportunistically purges rows whose own `expires_at` has already
        passed on every call -- cheap, bounded, and avoids this table
        growing without needing a separate reaper job (see
        RevokedPreviewToken's docstring for why that's safe)."""
        await self.db.execute(delete(RevokedPreviewToken).where(RevokedPreviewToken.expires_at < _utcnow()))
        existing = await self.db.execute(
            select(RevokedPreviewToken).where(RevokedPreviewToken.jti == jti)
        )
        if existing.scalar_one_or_none() is not None:
            await self.db.commit()
            return
        self.db.add(
            RevokedPreviewToken(
                jti=jti, session_id=session_id, port=port, expires_at=expires_at
            )
        )
        await self.db.commit()

    async def is_revoked(self, jti: str | None) -> bool:
        """A token minted before this feature existed (or one whose
        `create_preview_token` call somehow omitted a jti) has nothing to
        look up -- treat `jti=None` as "cannot be revoked", not as a lookup
        error, so this check never accidentally rejects an otherwise-valid
        token over a missing optional field."""
        if not jti:
            return False
        result = await self.db.execute(
            select(RevokedPreviewToken.jti).where(RevokedPreviewToken.jti == jti)
        )
        return result.scalar_one_or_none() is not None


class SecretRepository:
    """Read/write access for org-scoped secrets (docs/SECRETS-DESIGN.md).
    Follows `SandboxSessionRepository`/`SnapshotRepository`'s pattern
    exactly: every lookup takes `account_id` and folds it into the WHERE
    clause, so a secret belonging to a different account is structurally
    unreachable, never merely filtered out after the fact."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        account_id: str,
        name: str,
        ciphertext: str,
        nonce: str,
        wrapped_data_key: str,
        encryption_key_id: str,
        allowed_hosts: list[str],
        trust_tier: str | None = None,
    ) -> Secret:
        row = Secret(
            account_id=account_id,
            name=name,
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_data_key=wrapped_data_key,
            encryption_key_id=encryption_key_id,
            allowed_hosts=allowed_hosts,
            trust_tier=trust_tier,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_by_name_for_account(self, *, account_id: str, name: str) -> Secret | None:
        result = await self.db.execute(
            select(Secret).where(Secret.account_id == account_id, Secret.name == name)
        )
        return result.scalar_one_or_none()

    async def get_for_account(self, *, secret_id: str, account_id: str) -> Secret | None:
        """Fetch a secret scoped to account_id. Returns None for a secret
        owned by a different account -- same structural cross-tenant
        guarantee as every other repository in this module."""
        result = await self.db.execute(
            select(Secret).where(Secret.id == secret_id, Secret.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_for_account(self, account_id: str) -> list[Secret]:
        result = await self.db.execute(
            select(Secret).where(Secret.account_id == account_id).order_by(Secret.created_at.desc())
        )
        return list(result.scalars().all())

    async def resolve_names_for_account(
        self, *, account_id: str, names: list[str]
    ) -> dict[str, Secret]:
        """Resolve a list of names to rows, scoped to this account only --
        used at sandbox-session-create time to turn `secret_names` into
        real rows. A name with no matching row for this account is simply
        absent from the returned dict; the caller (routers/sandboxes.py)
        turns that into a 404, never leaking whether the name exists for
        some OTHER account."""
        if not names:
            return {}
        result = await self.db.execute(
            select(Secret).where(Secret.account_id == account_id, Secret.name.in_(names))
        )
        rows = result.scalars().all()
        return {row.name: row for row in rows}

    async def count_for_account(self, account_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(Secret).where(Secret.account_id == account_id)
        )
        return int(result.scalar_one())

    async def touch_last_used(self, secret_id: str) -> None:
        result = await self.db.execute(select(Secret).where(Secret.id == secret_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.last_used_at = _utcnow()
            await self.db.commit()

    async def delete(self, *, account_id: str, secret_id: str) -> bool:
        """Hard-delete, scoped to account_id -- an account can only delete
        its own secrets. Returns False (never distinguishable from "never
        existed") for a foreign or already-gone id."""
        result = await self.db.execute(
            select(Secret).where(Secret.id == secret_id, Secret.account_id == account_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self.db.delete(row)
        await self.db.commit()
        return True


class McpConnectionRepository:
    """Read/write access for org-scoped outbound-MCP connection grants
    (GitHub issues #116/#117, docs/OUTBOUND-MCP-DESIGN.md §3). Follows
    `SecretRepository`'s pattern exactly: every lookup takes `account_id`
    and folds it into the WHERE clause, so a connection belonging to a
    different account is structurally unreachable, never merely filtered
    out after the fact."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        account_id: str,
        label: str,
        catalog_id: str,
        host: str,
    ) -> McpConnection:
        row = McpConnection(account_id=account_id, label=label, catalog_id=catalog_id, host=host)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_by_label_for_account(self, *, account_id: str, label: str) -> McpConnection | None:
        result = await self.db.execute(
            select(McpConnection).where(
                McpConnection.account_id == account_id, McpConnection.label == label
            )
        )
        return result.scalar_one_or_none()

    async def list_for_account(self, account_id: str) -> list[McpConnection]:
        result = await self.db.execute(
            select(McpConnection)
            .where(McpConnection.account_id == account_id)
            .order_by(McpConnection.created_at)
        )
        return list(result.scalars().all())

    async def resolve_names_for_account(
        self, *, account_id: str, names: list[str]
    ) -> dict[str, McpConnection]:
        """Resolve a list of labels to rows, scoped to this account only --
        used at sandbox-session-create time to turn `mcp_connection_names`
        into real rows, the same shape `SecretRepository.
        resolve_names_for_account` already provides for `secret_names`. A
        label with no matching row for this account is simply absent from
        the returned dict; the caller (usage_policy.py) turns that into a
        404, never leaking whether the label exists for some OTHER
        account."""
        if not names:
            return {}
        result = await self.db.execute(
            select(McpConnection).where(
                McpConnection.account_id == account_id, McpConnection.label.in_(names)
            )
        )
        rows = result.scalars().all()
        return {row.label: row for row in rows}

    async def count_for_account(self, account_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(McpConnection).where(McpConnection.account_id == account_id)
        )
        return int(result.scalar_one())

    async def delete(self, *, account_id: str, connection_id: str) -> bool:
        """Hard-delete, scoped to account_id -- an account can only delete
        its own connections. Returns False (never distinguishable from
        "never existed") for a foreign or already-gone id."""
        result = await self.db.execute(
            select(McpConnection).where(
                McpConnection.id == connection_id, McpConnection.account_id == account_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self.db.delete(row)
        await self.db.commit()
        return True


class ExecLogEntryRepository:
    """Read/write access for the exec/file-op audit log
    (`docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3). `create` is the
    write side, called from `_log_exec_entry`; `list_for_session`/
    `count_for_session`/`list_after` back `GET .../log` and `GET .../watch`
    in routers/sandboxes.py. `list_across_accounts`/`count_across_accounts`
    back the admin-gated `GET /v1/admin/audit-log` (docs/ADMIN-ROLE-DESIGN.md,
    closing GitHub issue #140) -- the only methods on this repository that
    are NOT scoped to a single, already-authorized session_id, mirroring
    how AccountRepository.list_all/count_total are the only unscoped reads
    on that repository."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_across_accounts(
        self, *, account_id: str | None, limit: int, offset: int
    ) -> list[ExecLogEntry]:
        """Page through exec_log_entries across every session, newest
        first -- optionally narrowed to one account via `account_id`. Only
        reachable via the admin-gated route; every other read on this
        repository is scoped to a session_id a caller already owns."""
        query = select(ExecLogEntry)
        if account_id is not None:
            query = query.where(ExecLogEntry.account_id == account_id)
        query = (
            query.order_by(ExecLogEntry.started_at.desc(), ExecLogEntry.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_across_accounts(self, *, account_id: str | None) -> int:
        query = select(func.count()).select_from(ExecLogEntry)
        if account_id is not None:
            query = query.where(ExecLogEntry.account_id == account_id)
        result = await self.db.execute(query)
        return int(result.scalar_one())

    async def list_for_session(
        self, *, session_id: str, limit: int, offset: int
    ) -> list[ExecLogEntry]:
        """Page through a session's log, oldest first (so pagination is
        stable and matches the order operations actually happened in) --
        scoping to `session_id` alone is safe here because callers always
        resolve `session_id` -> row via `_get_active_session_or_404` (which
        is itself account-scoped) before calling this."""
        result = await self.db.execute(
            select(ExecLogEntry)
            .where(ExecLogEntry.session_id == session_id)
            .order_by(ExecLogEntry.started_at, ExecLogEntry.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_session(self, *, session_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(ExecLogEntry).where(ExecLogEntry.session_id == session_id)
        )
        return int(result.scalar_one())

    async def get_for_account(self, *, entry_id: str, account_id: str) -> ExecLogEntry | None:
        """Fetch a single row by id, scoped to `account_id` at the database
        layer -- the structural cross-tenant guarantee this module's
        docstring promises, applied here for `GET .../takeover-recordings/
        {entry_id}` (GitHub issue #133 replay route). A foreign or
        nonexistent `entry_id` both return None; the caller still must
        separately confirm `entry.session_id` matches the URL's session_id
        and that it's actually a `takeover_end` row with a recording
        pointer -- this method only proves account ownership."""
        result = await self.db.execute(
            select(ExecLogEntry).where(ExecLogEntry.id == entry_id, ExecLogEntry.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_after(
        self, *, session_id: str, after_id: str | None, limit: int
    ) -> list[ExecLogEntry]:
        """Rows newer than `after_id` (exclusive), oldest first -- backs the
        `/watch` SSE polling loop. `after_id` is `None` on a stream's first
        poll, returning the most recent `limit` rows as the initial batch so
        a newly-connected watcher doesn't have to wait for the *next* write
        to see anything."""
        base_query = select(ExecLogEntry).where(ExecLogEntry.session_id == session_id)
        if after_id is None:
            query = base_query.order_by(ExecLogEntry.started_at.desc(), ExecLogEntry.id.desc()).limit(limit)
            result = await self.db.execute(query)
            return list(reversed(result.scalars().all()))

        after_row = await self.db.execute(select(ExecLogEntry).where(ExecLogEntry.id == after_id))
        after = after_row.scalar_one_or_none()
        if after is None:
            # The cursor row is gone (shouldn't happen in practice -- rows are
            # never deleted except via session cascade-delete) -- fall back to
            # "no new rows" rather than re-sending the whole history.
            return []
        query = (
            base_query.where(
                (ExecLogEntry.started_at > after.started_at)
                | ((ExecLogEntry.started_at == after.started_at) & (ExecLogEntry.id > after.id))
            )
            .order_by(ExecLogEntry.started_at, ExecLogEntry.id)
            .limit(limit)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def _last_chained_row_hash(self, session_id: str) -> str:
        """The most recent *chained* row's hash for this session, or the
        genesis constant if none exists yet (either no prior rows, or only
        pre-hash-chaining legacy rows with row_hash IS NULL) --
        docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §4/§6. Covered by the existing
        `ix_exec_log_entries_session_started` index."""
        result = await self.db.execute(
            select(ExecLogEntry.row_hash)
            .where(ExecLogEntry.session_id == session_id, ExecLogEntry.row_hash.isnot(None))
            .order_by(ExecLogEntry.started_at.desc(), ExecLogEntry.id.desc())
            .limit(1)
        )
        row_hash = result.scalar_one_or_none()
        return row_hash if row_hash is not None else GENESIS_HASH

    async def create(
        self,
        *,
        session_id: str,
        account_id: str,
        source: str,
        operation: str,
        detail: dict,
        exit_code: int | None,
        output_truncated: str | None,
        started_at: datetime,
        duration_ms: int,
    ) -> ExecLogEntry:
        # Hash-chained tamper-evidence (GitHub issue #136,
        # docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §6): row_hash/prev_hash are
        # computed here and inserted in the same INSERT as the row itself
        # (never a follow-up UPDATE). Reading the session's most recent
        # chained row and inserting the new one is a read-then-write
        # sequence, not atomic on its own -- two genuinely concurrent writers
        # to the same session (e.g. an agent's /exec racing a human-takeover
        # periodic snapshot writer) could otherwise both read the same
        # prev_hash and each compute a row chaining from it, forking the
        # chain. get_exec_log_chain_lock serializes this per session_id so
        # that can't happen (see audit_chain_lock.py's own docstring for the
        # single-process-only caveat this carries).
        async with get_exec_log_chain_lock(session_id):
            row_id = _new_uuid()
            prev_hash = await self._last_chained_row_hash(session_id)
            canonical_fields = {
                "id": row_id,
                "session_id": session_id,
                "account_id": account_id,
                "source": source,
                "operation": operation,
                "detail": detail,
                "exit_code": exit_code,
                "output_truncated": output_truncated,
                "started_at": canonical_started_at(started_at),
                "duration_ms": duration_ms,
            }
            row_hash = compute_row_hash(prev_hash, canonical_fields)

            row = ExecLogEntry(
                id=row_id,
                session_id=session_id,
                account_id=account_id,
                source=source,
                operation=operation,
                detail=detail,
                exit_code=exit_code,
                output_truncated=output_truncated,
                started_at=started_at,
                duration_ms=duration_ms,
                row_hash=row_hash,
                prev_hash=prev_hash,
            )
            self.db.add(row)
            await self.db.commit()
            await self.db.refresh(row)
            return row

    # Grouping column for each `group_by` value GET /v1/usage/rollup accepts
    # (GitHub issue #162). `func.date(...)` truncates a timestamp to its
    # UTC calendar day and is supported the same way by both this project's
    # dev/test SQLite and production Postgres, so "day" grouping needs no
    # dialect-specific branch.
    _ROLLUP_GROUP_COLUMNS: dict[str, Any] = {
        "session": ExecLogEntry.session_id,
        "operation": ExecLogEntry.operation,
        "day": func.date(ExecLogEntry.started_at),
    }

    async def rollup_for_account(
        self,
        *,
        account_id: str,
        group_by: Literal["session", "day", "operation"],
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[str, int, int]], int, int, int]:
        """Duration/operation-count attribution over this account's own
        exec-log rows (GitHub issue #162's read-only rollup) -- account-scoped
        the same way every other read on this repository is, since the only
        caller (`routers/usage.py`) never accepts an `account_id` from the
        request itself.

        Returns `(groups, total_duration_ms, total_operation_count,
        group_count)`: `groups` is a page of `(group_key, duration_ms,
        operation_count)` tuples ordered by `duration_ms` descending;
        `total_duration_ms`/`total_operation_count` cover every matching row
        regardless of the page window; `group_count` is the number of
        distinct groups matching the filter, for pagination.
        """
        group_column = self._ROLLUP_GROUP_COLUMNS[group_by]
        filters = [ExecLogEntry.account_id == account_id]
        if start is not None:
            filters.append(ExecLogEntry.started_at >= start)
        if end is not None:
            filters.append(ExecLogEntry.started_at < end)

        totals_result = await self.db.execute(
            select(
                func.coalesce(func.sum(ExecLogEntry.duration_ms), 0),
                func.count(ExecLogEntry.id),
            ).where(*filters)
        )
        total_duration_ms, total_operation_count = totals_result.one()

        group_count_result = await self.db.execute(
            select(func.count(func.distinct(group_column))).where(*filters)
        )
        group_count = int(group_count_result.scalar_one())

        groups_result = await self.db.execute(
            select(
                group_column.label("group_key"),
                func.coalesce(func.sum(ExecLogEntry.duration_ms), 0).label("duration_ms"),
                func.count(ExecLogEntry.id).label("operation_count"),
            )
            .where(*filters)
            .group_by(group_column)
            .order_by(func.sum(ExecLogEntry.duration_ms).desc())
            .limit(limit)
            .offset(offset)
        )
        groups = [
            (str(row.group_key), int(row.duration_ms), int(row.operation_count))
            for row in groups_result.all()
        ]
        return groups, int(total_duration_ms), int(total_operation_count), group_count


class SnapshotRepository:
    """Read/write access for filesystem snapshots (docs/SNAPSHOT-DESIGN.md).
    Follows `SandboxSessionRepository`'s pattern exactly: every lookup takes
    `account_id` and folds it into the WHERE clause, so a snapshot belonging
    to a different account is structurally unreachable, not just filtered
    out after the fact."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        snapshot_id: str,
        account_id: str,
        session_id: str | None,
        label: str | None,
        storage_key_prefix: str,
        status: str = "pending",
    ) -> Snapshot:
        row = Snapshot(
            id=snapshot_id,
            account_id=account_id,
            session_id=session_id,
            label=label,
            storage_key_prefix=storage_key_prefix,
            status=status,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def mark_completed(self, *, snapshot_id: str, size_bytes: int) -> None:
        result = await self.db.execute(select(Snapshot).where(Snapshot.id == snapshot_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = "completed"
            row.size_bytes = size_bytes
            await self.db.commit()

    async def mark_failed(self, *, snapshot_id: str) -> None:
        result = await self.db.execute(select(Snapshot).where(Snapshot.id == snapshot_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = "failed"
            await self.db.commit()

    async def delete_row(self, snapshot_id: str) -> None:
        """Hard-delete a bookkeeping row (used to roll back a failed create,
        same pattern as SandboxSessionRepository.delete_row)."""
        result = await self.db.execute(select(Snapshot).where(Snapshot.id == snapshot_id))
        row = result.scalar_one_or_none()
        if row is not None:
            await self.db.delete(row)
            await self.db.commit()

    async def get_for_account(self, *, snapshot_id: str, account_id: str) -> Snapshot | None:
        """Fetch a snapshot scoped to account_id. Returns None for a
        snapshot owned by a different account -- see this module's
        docstring for why the caller cannot distinguish "doesn't exist"
        from "belongs to someone else"."""
        result = await self.db.execute(
            select(Snapshot).where(Snapshot.id == snapshot_id, Snapshot.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_for_session(self, *, session_id: str, account_id: str) -> list[Snapshot]:
        result = await self.db.execute(
            select(Snapshot)
            .where(
                Snapshot.session_id == session_id,
                Snapshot.account_id == account_id,
                Snapshot.deleted_at.is_(None),
            )
            .order_by(Snapshot.created_at.desc())
        )
        return list(result.scalars().all())

    async def count_active_for_account(self, account_id: str) -> int:
        """Non-deleted snapshot count -- backs BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT."""
        result = await self.db.execute(
            select(func.count())
            .select_from(Snapshot)
            .where(Snapshot.account_id == account_id, Snapshot.deleted_at.is_(None))
        )
        return int(result.scalar_one())

    async def mark_deleted(self, *, snapshot_id: str) -> None:
        result = await self.db.execute(select(Snapshot).where(Snapshot.id == snapshot_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.deleted_at = _utcnow()
            await self.db.commit()


class SandboxImageRepository:
    """Read/write access for declarative-builder custom images
    (docs/DECLARATIVE-BUILDER-DESIGN.md). Follows `SnapshotRepository`'s
    pattern exactly: every lookup takes `account_id` and folds it into the
    WHERE clause, so an image belonging to a different account is
    structurally unreachable, not just filtered out after the fact."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        image_id: str,
        account_id: str,
        label: str | None,
        base: str,
        python_packages: list[str],
        apt_packages: list[str],
        cache_key: str,
        status: str = "queued",
        npm_packages: list[str] | None = None,
    ) -> SandboxImage:
        row = SandboxImage(
            id=image_id,
            account_id=account_id,
            label=label,
            base=base,
            python_packages=python_packages,
            apt_packages=apt_packages,
            npm_packages=npm_packages or [],
            cache_key=cache_key,
            status=status,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_for_account(self, *, image_id: str, account_id: str) -> SandboxImage | None:
        """Fetch an image scoped to account_id. Returns None for an image
        owned by a different account -- see this module's docstring for why
        the caller cannot distinguish "doesn't exist" from "belongs to
        someone else"."""
        result = await self.db.execute(
            select(SandboxImage).where(SandboxImage.id == image_id, SandboxImage.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_for_account(self, *, account_id: str) -> list[SandboxImage]:
        result = await self.db.execute(
            select(SandboxImage)
            .where(SandboxImage.account_id == account_id, SandboxImage.deleted_at.is_(None))
            .order_by(SandboxImage.created_at.desc())
        )
        return list(result.scalars().all())

    async def count_active_for_account(self, account_id: str) -> int:
        """Non-deleted image count -- backs BOXKITE_MAX_IMAGES_PER_ACCOUNT."""
        result = await self.db.execute(
            select(func.count())
            .select_from(SandboxImage)
            .where(SandboxImage.account_id == account_id, SandboxImage.deleted_at.is_(None))
        )
        return int(result.scalar_one())

    async def count_in_flight_total(self) -> int:
        """Cluster-wide count of builds not yet resolved to a terminal
        status ("queued", "building", or "scanning"), across ALL accounts
        combined -- backs BOXKITE_GLOBAL_MAX_CONCURRENT_IMAGE_BUILDS.
        Deliberately NOT the same query as count_active_for_account (which
        also counts completed/failed/rejected non-deleted rows, the right
        definition for the per-account image-count cap, but the wrong one
        for a build-concurrency cap)."""
        result = await self.db.execute(
            select(func.count())
            .select_from(SandboxImage)
            .where(SandboxImage.status.in_(["queued", "building", "scanning"]))
        )
        return int(result.scalar_one())

    async def find_cached_completed(
        self, *, account_id: str, cache_key: str, not_before: datetime
    ) -> SandboxImage | None:
        """Most recent `completed` image for this account with a matching
        cache_key, created at or after `not_before` -- backs the 24h build
        cache (docs/DECLARATIVE-BUILDER-DESIGN.md's cache requirement).
        Deliberately scoped to `account_id` -- a cache hit never reuses
        another account's build, even for an identical package spec."""
        result = await self.db.execute(
            select(SandboxImage)
            .where(
                SandboxImage.account_id == account_id,
                SandboxImage.cache_key == cache_key,
                SandboxImage.status == "completed",
                SandboxImage.deleted_at.is_(None),
                SandboxImage.created_at >= not_before,
            )
            .order_by(SandboxImage.created_at.desc())
        )
        return result.scalars().first()

    async def mark_building(self, *, image_id: str) -> None:
        await self._set_status(image_id, status="building")

    async def mark_scanning(self, *, image_id: str) -> None:
        await self._set_status(image_id, status="scanning")

    async def mark_completed(
        self, *, image_id: str, digest: str, registry_ref: str, scan_result: dict
    ) -> None:
        result = await self.db.execute(select(SandboxImage).where(SandboxImage.id == image_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = "completed"
            row.digest = digest
            row.registry_ref = registry_ref
            row.scan_result = scan_result
            row.completed_at = _utcnow()
            await self.db.commit()

    async def mark_failed(self, *, image_id: str, failure_reason: str) -> None:
        await self._set_status(image_id, status="failed", failure_reason=failure_reason)

    async def mark_rejected(self, *, image_id: str, failure_reason: str, scan_result: dict | None = None) -> None:
        result = await self.db.execute(select(SandboxImage).where(SandboxImage.id == image_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = "rejected"
            row.failure_reason = failure_reason
            if scan_result is not None:
                row.scan_result = scan_result
            await self.db.commit()

    async def _set_status(self, image_id: str, *, status: str, failure_reason: str | None = None) -> None:
        result = await self.db.execute(select(SandboxImage).where(SandboxImage.id == image_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = status
            if failure_reason is not None:
                row.failure_reason = failure_reason
            await self.db.commit()

    async def mark_deleted(self, *, image_id: str) -> None:
        result = await self.db.execute(select(SandboxImage).where(SandboxImage.id == image_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.deleted_at = _utcnow()
            await self.db.commit()


class SandboxVolumeRepository:
    """Read/write access for independent PVC-backed volumes
    (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum). Follows
    SandboxImageRepository's exact pattern: every lookup takes account_id
    and folds it into the WHERE clause."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        volume_id: str,
        account_id: str,
        label: str | None,
        size_gb: float,
        status: str = "queued",
    ) -> SandboxVolume:
        row = SandboxVolume(id=volume_id, account_id=account_id, label=label, size_gb=size_gb, status=status)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_for_account(self, *, volume_id: str, account_id: str) -> SandboxVolume | None:
        result = await self.db.execute(
            select(SandboxVolume).where(SandboxVolume.id == volume_id, SandboxVolume.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_for_account(self, *, account_id: str) -> list[SandboxVolume]:
        result = await self.db.execute(
            select(SandboxVolume)
            .where(SandboxVolume.account_id == account_id, SandboxVolume.deleted_at.is_(None))
            .order_by(SandboxVolume.created_at.desc())
        )
        return list(result.scalars().all())

    async def count_active_for_account(self, account_id: str) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(SandboxVolume)
            .where(SandboxVolume.account_id == account_id, SandboxVolume.deleted_at.is_(None))
        )
        return int(result.scalar_one())

    async def mark_creating(self, *, volume_id: str) -> None:
        await self._set_status(volume_id, status="creating")

    async def mark_ready(self, *, volume_id: str, pvc_name: str) -> None:
        result = await self.db.execute(select(SandboxVolume).where(SandboxVolume.id == volume_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = "ready"
            row.pvc_name = pvc_name
            await self.db.commit()

    async def mark_failed(self, *, volume_id: str, failure_reason: str) -> None:
        await self._set_status(volume_id, status="failed", failure_reason=failure_reason)

    async def _set_status(self, volume_id: str, *, status: str, failure_reason: str | None = None) -> None:
        result = await self.db.execute(select(SandboxVolume).where(SandboxVolume.id == volume_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.status = status
            if failure_reason is not None:
                row.failure_reason = failure_reason
            await self.db.commit()

    async def mark_deleted(self, *, volume_id: str) -> None:
        result = await self.db.execute(select(SandboxVolume).where(SandboxVolume.id == volume_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.deleted_at = _utcnow()
            await self.db.commit()


class RefreshTokenRepository:
    """Read/write access for opt-in dashboard refresh tokens (issue #79).
    Lookup is by hash alone (like `ApiKeyRepository.get_active_by_hash`) --
    the hash itself is the full credential, so there is no account_id to
    additionally scope by until after it resolves."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, *, account_id: str, token_hash: str, expires_at: datetime) -> RefreshToken:
        row = RefreshToken(account_id=account_id, token_hash=token_hash, expires_at=expires_at)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Returns the row regardless of revoked/expired state -- callers
        (routers/auth.py's refresh handler) need to distinguish "never
        existed", "expired", and "already revoked" (a replay signal) from
        each other, so filtering here would destroy that distinction."""
        result = await self.db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        return result.scalar_one_or_none()

    async def revoke(self, *, token_id: str) -> None:
        result = await self.db.execute(select(RefreshToken).where(RefreshToken.id == token_id))
        row = result.scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = _utcnow()
            await self.db.commit()

    async def revoke_all_for_account(self, account_id: str) -> None:
        """Revoke every not-yet-revoked refresh token for an account --
        called on detected token reuse (suspected theft) and on a
        successful password reset (kill any session a previous holder of
        the password may still have)."""
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.account_id == account_id, RefreshToken.revoked_at.is_(None))
        )
        now = _utcnow()
        for row in result.scalars().all():
            row.revoked_at = now
        await self.db.commit()


class PasswordResetTokenRepository:
    """Read/write access for opt-in password-reset tokens (issue #79)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, *, account_id: str, token_hash: str, expires_at: datetime) -> PasswordResetToken:
        row = PasswordResetToken(account_id=account_id, token_hash=token_hash, expires_at=expires_at)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_active_by_hash(self, token_hash: str) -> PasswordResetToken | None:
        """Not-yet-used, not-yet-expired only -- POST .../confirm treats
        anything else (unknown hash, already used, expired) identically as
        an "invalid or expired token" 400, never distinguishing which."""
        result = await self.db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > _utcnow(),
            )
        )
        return result.scalar_one_or_none()

    async def mark_used(self, *, token_id: str) -> None:
        result = await self.db.execute(select(PasswordResetToken).where(PasswordResetToken.id == token_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.used_at = _utcnow()
            await self.db.commit()

    async def invalidate_active_for_account(self, account_id: str) -> None:
        """Stamp every other still-active reset token for this account as
        used, so at most one outstanding reset link/token is ever valid at
        once -- called both when a new reset is requested and after a
        successful confirm."""
        result = await self.db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.account_id == account_id,
                PasswordResetToken.used_at.is_(None),
            )
        )
        now = _utcnow()
        for row in result.scalars().all():
            row.used_at = now
        await self.db.commit()


class EmailVerificationTokenRepository:
    """Read/write access for opt-in email-verification tokens (issue #79).
    Follows `PasswordResetTokenRepository`'s exact pattern."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, *, account_id: str, token_hash: str, expires_at: datetime) -> EmailVerificationToken:
        row = EmailVerificationToken(account_id=account_id, token_hash=token_hash, expires_at=expires_at)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_active_by_hash(self, token_hash: str) -> EmailVerificationToken | None:
        result = await self.db.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == token_hash,
                EmailVerificationToken.used_at.is_(None),
                EmailVerificationToken.expires_at > _utcnow(),
            )
        )
        return result.scalar_one_or_none()

    async def mark_used(self, *, token_id: str) -> None:
        result = await self.db.execute(
            select(EmailVerificationToken).where(EmailVerificationToken.id == token_id)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.used_at = _utcnow()
            await self.db.commit()

    async def invalidate_active_for_account(self, account_id: str) -> None:
        result = await self.db.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.account_id == account_id,
                EmailVerificationToken.used_at.is_(None),
            )
        )
        now = _utcnow()
        for row in result.scalars().all():
            row.used_at = now
        await self.db.commit()


class AdminAccessLogRepository:
    """Write side of `AdminAccessLog` -- see that model's docstring. No
    read/query methods exist here yet (nothing in this codebase surfaces
    this log back through an API); add one only alongside a concrete need,
    same YAGNI posture the rest of this module holds."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record(self, *, admin_account_id: str, endpoint: str) -> None:
        row = AdminAccessLog(admin_account_id=admin_account_id, endpoint=endpoint)
        self.db.add(row)
        await self.db.commit()


class OAuthClientRepository:
    """Read/write access for `OAuthClient` -- MCP clients that have
    dynamically self-registered via `POST /oauth/register`
    (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, *, client_id: str, client_name: str, redirect_uris: list[str]) -> OAuthClient:
        row = OAuthClient(client_id=client_id, client_name=client_name, redirect_uris=redirect_uris)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_by_client_id(self, client_id: str) -> OAuthClient | None:
        result = await self.db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
        return result.scalar_one_or_none()


class OAuthAuthorizationCodeRepository:
    """Read/write access for `OAuthAuthorizationCode` -- one row per
    in-flight `GET /oauth/authorize` grant. See that model's docstring for
    the single-use/short-TTL contract this repository enforces."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        code: str,
        client_id: str,
        account_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str | None,
        expires_at: datetime,
    ) -> OAuthAuthorizationCode:
        row = OAuthAuthorizationCode(
            code=code,
            client_id=client_id,
            account_id=account_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            expires_at=expires_at,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_valid_by_code(self, code: str) -> OAuthAuthorizationCode | None:
        """Fetch the code only if it is still unconsumed and unexpired --
        the two conditions `POST /oauth/token` must check before exchanging
        it. A caller that finds nothing here should return a generic
        `invalid_grant` (RFC 6749 §5.2), never distinguish "doesn't exist"
        from "expired" from "already consumed"."""
        result = await self.db.execute(
            select(OAuthAuthorizationCode).where(
                OAuthAuthorizationCode.code == code,
                OAuthAuthorizationCode.consumed_at.is_(None),
                OAuthAuthorizationCode.expires_at > _utcnow(),
            )
        )
        return result.scalar_one_or_none()

    async def mark_consumed(self, *, code_id: str) -> bool:
        """Atomically mark the code consumed, returning True only if this
        call is the one that actually flipped it.

        Must be a single conditional `UPDATE ... WHERE consumed_at IS NULL`,
        not a SELECT-then-write -- two concurrent exchanges of the same code
        (a leaked code, or a client retrying after a timeout) would otherwise
        both read `consumed_at IS NULL` before either commits, and both go
        on to mint a token pair from what's supposed to be a single-use
        code. The caller must only issue tokens when this returns True."""
        result = await self.db.execute(
            update(OAuthAuthorizationCode)
            .where(
                OAuthAuthorizationCode.id == code_id,
                OAuthAuthorizationCode.consumed_at.is_(None),
            )
            .values(consumed_at=_utcnow())
        )
        await self.db.commit()
        return result.rowcount == 1


class OAuthTokenRepository:
    """Read/write access for `OAuthToken` -- issued refresh tokens. See that
    model's docstring for the rotation/reuse-detection contract."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self, *, client_id: str, account_id: str, refresh_token_hash: str, rotated_from: str | None = None
    ) -> OAuthToken:
        row = OAuthToken(
            client_id=client_id,
            account_id=account_id,
            refresh_token_hash=refresh_token_hash,
            rotated_from=rotated_from,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_by_hash(self, refresh_token_hash: str) -> OAuthToken | None:
        """Fetch by hash regardless of revoked status -- reuse detection
        needs to see an already-revoked row too, to distinguish "never
        issued"/unknown token (RFC 6749 §5.2 invalid_grant, nothing more to
        do) from "this exact token was already rotated away" (theft signal,
        revoke the whole family -- see `revoke_family`)."""
        result = await self.db.execute(
            select(OAuthToken).where(OAuthToken.refresh_token_hash == refresh_token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke(self, *, token_id: str) -> bool:
        """Atomically revoke, returning True only if this call is the one
        that actually flipped `revoked_at`.

        Must be a single conditional `UPDATE ... WHERE revoked_at IS NULL`,
        not a SELECT-then-write -- two concurrent refresh_token grants
        presenting the same not-yet-rotated token (the actual theft
        scenario reuse detection exists to catch) would otherwise both read
        `revoked_at IS NULL` before either commits, and both mint an
        independent valid token pair from a single refresh token. The
        caller must only issue a new token pair when this returns True; a
        False return means another request rotated this token first and
        the caller should treat it as reuse (see `revoke_family`)."""
        result = await self.db.execute(
            update(OAuthToken)
            .where(OAuthToken.id == token_id, OAuthToken.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        await self.db.commit()
        return result.rowcount == 1

    async def revoke_family(self, *, token_id: str) -> None:
        """Revoke this token AND every descendant produced by rotating it
        forward (walking `rotated_from` in reverse) -- used on refresh-token
        reuse detection, where an attacker or the legitimate caller may have
        already rotated the presented (stale) token forward at least once
        more. Killing only the presented row would leave that later
        descendant token still valid, defeating the point of reuse
        detection (OAuth 2.1's recommended response to detected refresh
        token replay)."""
        pending = [token_id]
        while pending:
            current_id = pending.pop()
            await self.db.execute(
                update(OAuthToken)
                .where(OAuthToken.id == current_id, OAuthToken.revoked_at.is_(None))
                .values(revoked_at=_utcnow())
            )
            children = await self.db.execute(
                select(OAuthToken.id).where(OAuthToken.rotated_from == current_id)
            )
            pending.extend(row[0] for row in children.all())
        await self.db.commit()


class WebhookSubscriptionRepository:
    """Read/write access for webhook registrations (docs/WEBHOOKS-DESIGN.md).
    Follows `SandboxImageRepository`'s exact pattern: every lookup takes
    `account_id` and folds it into the WHERE clause, so a subscription
    belonging to a different account is structurally unreachable, not just
    filtered out after the fact."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        account_id: str,
        url: str,
        description: str | None,
        event_types: list[str],
        ciphertext: str,
        nonce: str,
        wrapped_data_key: str,
        encryption_key_id: str,
        payload_format: str = "boxkite_v1",
        hec_token_ciphertext: str | None = None,
        hec_token_nonce: str | None = None,
        hec_token_wrapped_data_key: str | None = None,
        hec_token_encryption_key_id: str | None = None,
    ) -> WebhookSubscription:
        row = WebhookSubscription(
            account_id=account_id,
            url=url,
            description=description,
            event_types=event_types,
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_data_key=wrapped_data_key,
            encryption_key_id=encryption_key_id,
            payload_format=payload_format,
            hec_token_ciphertext=hec_token_ciphertext,
            hec_token_nonce=hec_token_nonce,
            hec_token_wrapped_data_key=hec_token_wrapped_data_key,
            hec_token_encryption_key_id=hec_token_encryption_key_id,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_for_account(
        self, *, subscription_id: str, account_id: str
    ) -> WebhookSubscription | None:
        """Fetch a subscription scoped to account_id. Returns None for a
        subscription owned by a different account -- same structural
        cross-tenant guarantee as every other repository in this module."""
        result = await self.db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id, WebhookSubscription.account_id == account_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_account(self, account_id: str) -> list[WebhookSubscription]:
        result = await self.db.execute(
            select(WebhookSubscription)
            .where(WebhookSubscription.account_id == account_id)
            .order_by(WebhookSubscription.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_active_for_account_and_event(
        self, *, account_id: str, event_type: str
    ) -> list[WebhookSubscription]:
        """Active subscriptions for this account whose `event_types` list
        contains `event_type` -- used at event-fire time
        (`webhooks.enqueue_event`). Per-account opt-in by construction: an
        account with zero subscriptions (the default for every account)
        gets an empty list back and nothing is ever enqueued for it, so
        this feature has no meaningful per-account or global cost until an
        account explicitly registers a webhook."""
        result = await self.db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.account_id == account_id,
                WebhookSubscription.is_active.is_(True),
            )
        )
        rows = result.scalars().all()
        return [row for row in rows if event_type in row.event_types]

    async def count_for_account(self, account_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(WebhookSubscription).where(WebhookSubscription.account_id == account_id)
        )
        return int(result.scalar_one())

    async def touch_last_triggered(self, subscription_id: str) -> None:
        result = await self.db.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == subscription_id)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.last_triggered_at = _utcnow()
            await self.db.commit()

    async def delete(self, *, account_id: str, subscription_id: str) -> bool:
        """Hard-delete, scoped to account_id -- an account can only delete
        its own subscriptions. Returns False (never distinguishable from
        "never existed") for a foreign or already-gone id."""
        result = await self.db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id, WebhookSubscription.account_id == account_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self.db.delete(row)
        await self.db.commit()
        return True


class WebhookDeliveryRepository:
    """Read/write access for webhook delivery attempts
    (docs/WEBHOOKS-DESIGN.md). Unlike every other repository in this module,
    `list_due` is deliberately UNSCOPED by account -- it backs the
    background delivery worker (`webhook_delivery.py`), which processes
    every account's due deliveries in one polling loop, the same
    "one deliberate, worker-only exception" posture
    `SandboxSessionRepository.list_active_older_than` already documents for
    the reaper."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        subscription_id: str,
        account_id: str,
        event_type: str,
        payload: dict,
    ) -> WebhookDelivery:
        row = WebhookDelivery(
            subscription_id=subscription_id,
            account_id=account_id,
            event_type=event_type,
            payload=payload,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def list_due(self, *, now: datetime, limit: int) -> list[WebhookDelivery]:
        """Pending deliveries whose next_attempt_at has arrived, across ALL
        accounts/subscriptions -- see this class's docstring for why this
        one method is deliberately unscoped."""
        result = await self.db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_for_subscription(
        self, *, subscription_id: str, account_id: str, limit: int, offset: int
    ) -> list[WebhookDelivery]:
        """Scoped by account_id even though subscription_id alone would
        already narrow the rows to one subscription -- defense in depth,
        same reasoning `ExecLogEntryRepository.list_for_session` documents:
        callers resolve subscription_id -> ownership via
        `WebhookSubscriptionRepository.get_for_account` first, but scoping
        here too means a future caller that forgets that check still can't
        leak another account's delivery rows."""
        result = await self.db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.subscription_id == subscription_id, WebhookDelivery.account_id == account_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def mark_delivered(self, *, delivery_id: str, response_status_code: int, response_body: str) -> None:
        result = await self.db.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.status = "delivered"
        row.attempt_count += 1
        row.last_attempt_at = _utcnow()
        row.delivered_at = _utcnow()
        row.response_status_code = response_status_code
        row.response_body_truncated = response_body
        await self.db.commit()

    async def record_failed_attempt(
        self,
        *,
        delivery_id: str,
        next_attempt_at: datetime | None,
        response_status_code: int | None,
        response_body: str | None,
        failure_reason: str,
        exhausted: bool,
    ) -> None:
        """Records one failed attempt. If `exhausted` is True (attempt_count
        has reached BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS), marks the row
        `failed` terminally; otherwise schedules the next retry at
        `next_attempt_at` and leaves status as `pending`."""
        result = await self.db.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.attempt_count += 1
        row.last_attempt_at = _utcnow()
        row.response_status_code = response_status_code
        row.response_body_truncated = response_body
        row.failure_reason = failure_reason
        if exhausted:
            row.status = "failed"
        else:
            row.next_attempt_at = next_attempt_at
        await self.db.commit()
