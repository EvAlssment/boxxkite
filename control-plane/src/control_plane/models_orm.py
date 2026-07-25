"""SQLAlchemy ORM models — the source of truth for the control-plane schema.

Four tables:

- `accounts` — one row per signed-up user (email + password hash).
- `api_keys` — long-lived credentials scoped to an account; only a hash of
  the raw key is ever persisted (see `security.py:hash_api_key`).
- `sandbox_sessions` — usage-accounting rows only (account_id, created_at,
  destroyed_at, duration). The actual sandbox pod state is NOT duplicated
  here — that stays owned entirely by SandboxManager/K8s, per the task's
  explicit instruction. `sandbox_session_id` is the same UUID the control
  plane passes to `SandboxManager.create_session(session_id=...)`, so this
  table is purely bookkeeping for ownership + fair-use accounting.
- `exec_log_entries` — durable audit-log rows, one per exec/file operation
  against a sandbox session (agent-issued or, later, human-takeover-issued).
  See `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3 for the full design
  rationale; written from `routers/sandboxes.py`'s shared `_log_exec_entry`
  helper, never constructed ad hoc elsewhere.
- `snapshots` — filesystem snapshot metadata (docs/SNAPSHOT-DESIGN.md).
  `session_id` is nullable and ON DELETE SET NULL (not CASCADE) because a
  snapshot must outlive the session it was taken from -- the whole point of
  the feature is "save my files, come back after the session is gone".
  The actual snapshotted bytes live in blob storage under
  `storage_key_prefix`; this row is bookkeeping + the account-scoping
  boundary, mirroring `sandbox_sessions`'s own division of labor.
- `sandbox_images` — declarative-builder custom image metadata
  (docs/DECLARATIVE-BUILDER-DESIGN.md). One row per build request. The
  actual image bytes live in a registry, namespaced
  `boxkite-images/{account_id}/{image_id}`, never referenced by anything
  outside this row until `status == "completed"` and `digest` is set --
  see `image_builder.py` for the build/scan pipeline and
  `routers/images.py` for the ownership-scoping API.
- `oauth_clients` / `oauth_authorization_codes` / `oauth_tokens` — the MCP
  OAuth 2.1 authorization server (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md
  §3). See each model's own docstring for its role in the
  DCR/authorize/token flow.
- `revoked_preview_tokens` — a denylist of individually-revoked network-
  ingress preview-URL tokens (docs/NETWORK-INGRESS-DESIGN.md). See
  `RevokedPreviewToken`'s own docstring below for why this is a denylist
  rather than a delete, and how it stays bounded in size without a
  separate reaper job.
- `webhook_subscriptions` / `webhook_deliveries` — outbound webhook
  registration + delivery (docs/WEBHOOKS-DESIGN.md). A subscription is a
  caller-registered `(url, event_types)` pair plus an envelope-encrypted
  signing secret (same `secrets_kms.py` primitive `Secret.ciphertext`
  already uses -- the raw secret is shown to the caller exactly once, at
  creation time, and re-derived only at delivery time to compute the
  HMAC signature). A delivery is one attempt-tracked row per fired event
  per matching subscription -- `webhook_delivery.py`'s background worker
  polls for due rows and retries with backoff, independent of the request
  that fired the event (fire-and-forget from the caller's perspective,
  same "never fail the underlying operation" posture as `AuditSink`).
- `refresh_tokens`, `password_reset_tokens`, `email_verification_tokens` —
  the three opt-in dashboard-auth credential tables added for issue #79
  (refresh-token rotation, password reset, email verification). All three
  follow the exact same shape as `api_keys`: only a SHA-256 digest of the
  raw token is ever persisted (`token_hash`, see `security.py`), the raw
  value is handed to the caller/email exactly once, and every lookup is by
  hash, never by account_id + guesswork. See `routers/auth.py` for the
  gating flags (`BOXKITE_REFRESH_TOKENS_ENABLED`,
  `BOXKITE_PASSWORD_RESET_ENABLED`, `BOXKITE_EMAIL_VERIFICATION_ENABLED`)
  and `repository.py` for the account-scoped "revoke all" operations used
  on password change and reuse detection.

(This list has not been kept exhaustively in sync with every table added
since -- `secrets`, `admin_access_log`, and `sandbox_volumes` also exist;
treat the class definitions below as the actual source of truth, this
docstring as an index into the ones with the most non-obvious design
rationale.)

IDs and timestamps are stored as portable String/DateTime columns (not
Postgres-only UUID/TIMESTAMPTZ types) so the exact same models work against
SQLite in tests and Postgres in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    # Nullable: a social-login-only account (GitHub/Google, see github_id/
    # google_id below) has no password at all. `/v1/auth/login` must check
    # this is not None before attempting verify_password -- see
    # docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §4.1.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # Either, neither (password-only), or both may be set -- see
    # docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §4.2. Deliberately no
    # unique constraint tying the two together: an account can link both
    # providers independently over time.
    github_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    google_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    # Enterprise SSO (docs/ENTERPRISE-SSO-DESIGN.md, issue #126 Phase 1) --
    # same "either, neither, or alongside a password/github_id/google_id"
    # posture as the two columns above. `sso_provider_user_id` is the
    # hosted broker's own profile id (unique per IdP-side identity) --
    # what get_by_sso_subject_id/login matching keys off, never email
    # alone. `sso_organization_id`/`sso_connection_id` are captured now for
    # a future admin/SCIM path to consume (design doc §5); nothing reads
    # them yet.
    sso_provider_user_id: Mapped[str | None] = mapped_column(
        String(191), nullable=True, unique=True, index=True
    )
    sso_organization_id: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    sso_connection_id: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    # SCIM 2.0 provisioning via WorkOS Directory Sync (Phase 2 of issue #126,
    # docs/ENTERPRISE-SSO-DESIGN.md). `scim_directory_user_id` is WorkOS's
    # OWN "directory_user_..." id -- a genuinely distinct WorkOS resource
    # from `sso_provider_user_id` above ("prof_..."), not the same
    # identifier reused. WorkOS Directory Sync (SCIM-side admin
    # provisioning) and WorkOS SSO (interactive IdP login) are two separate
    # products in WorkOS's own API, each minting its own id for what is
    # conceptually the same human -- confirmed against WorkOS's public
    # Directory User object reference before adding this column, rather
    # than assumed. An account can have one, both, or neither set: SCIM
    # alone provisions an account "shell" (email + org, no password, no
    # sso_provider_user_id) that a person later links to their own
    # interactive SSO identity the first time they actually log in (see
    # routers/enterprise_sso.py's `_resolve_or_create_account` for that
    # linking exception -- the one deliberate case in this codebase where
    # an email match IS trusted to auto-link, because the assertion comes
    # from the same admin-controlled IdP that provisioned this row via
    # SCIM in the first place, not an unrelated third party).
    scim_directory_user_id: Mapped[str | None] = mapped_column(
        String(191), nullable=True, unique=True, index=True
    )
    # Set (to "now") when a `dsync.user.updated` event reports
    # state != "active" (WorkOS's own legal values are "active"/"inactive"/
    # "suspended" -- anything not "active" is treated as deactivated here),
    # or when a `dsync.user.deleted` event fires for a directory user this
    # account is linked to. NULL means "not deactivated" -- the default for
    # every account, including every account that predates this feature.
    # This is the actual enforcement point: deps.py's API-key and JWT
    # resolution paths, and routers/auth.py's login/refresh, all reject a
    # request once this is set, so deactivation revokes access on the VERY
    # NEXT authenticated request -- not just at a future login attempt --
    # closing the gap the codebase's pre-existing, purely-informational
    # `email_verified_at` deliberately leaves open for a different feature.
    scim_deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional per-account override for hosted /v1/sandboxes/{id}/exec's
    # command allowlist (see command_whitelist.py for the rule format this
    # stores verbatim). NULL/empty means unrestricted -- today's behavior,
    # unchanged for every account that hasn't opted in. This is NOT a
    # sandbox-escape boundary (see SECURITY.md) -- it's an opt-in guardrail
    # layered on top of, not a replacement for, pod isolation.
    custom_allowed_commands: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # NULL means "not verified yet" -- the default for every account,
    # including every account created before BOXKITE_EMAIL_VERIFICATION_ENABLED
    # existed. Set once by POST /v1/auth/verify-email. Deliberately
    # informational only today: no route currently checks this column to
    # gate access (see routers/auth.py's module docstring for why enforcing
    # it is left as an explicit follow-up rather than silently done here).
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cross-account visibility grant (docs/ADMIN-ROLE-DESIGN.md) -- gates
    # GET /v1/admin/metrics and any future cross-account view. Deliberately
    # NOT settable through any API route: there is no self-serve "make me
    # admin" endpoint, by design, to close off privilege-escalation via this
    # column. Grant it only by direct operator action against the database
    # (see docs/ADMIN-ROLE-DESIGN.md's "Granting admin" section for the
    # exact runbook step), the same "operator-controlled, not caller-
    # controlled" posture SECRETS_KMS_BACKEND/ENVIRONMENT already have.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    sandbox_sessions: Mapped[list["SandboxSession"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Displayable, non-secret prefix (e.g. "bxk_live_ab12cd34") for UI/log
    # identification. Never sufficient on its own to authenticate.
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 hex digest of the full raw key. The raw key itself is never
    # stored or logged anywhere — see security.py.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # Permission role for this key within its account -- "admin" (default,
    # preserves this project's original behavior: every key can do
    # everything, including WS /takeover) or "member" (everything except
    # initiating a takeover session; see security.py's
    # can_initiate_takeover and docs/SANDBOX-OBSERVABILITY-DESIGN.md). This
    # is the RBAC unit this codebase actually has: there is no separate
    # "account member" concept distinct from an API key, so a key's role is
    # the closest existing extension point for "who within an account may
    # do X" rather than inventing a new user/membership model. New keys
    # default to "admin" so existing integrations are unaffected until an
    # account deliberately mints a restricted "member" key.
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship(back_populates="api_keys")


class SandboxSession(Base):
    """Usage-accounting row for one sandbox session.

    Does NOT own sandbox pod state (that's SandboxManager/K8s's job) — this
    table exists purely so the control plane can enforce fair-use limits and
    ownership without querying Kubernetes for accounting.
    """

    __tablename__ = "sandbox_sessions"
    __table_args__ = (
        Index("ix_sandbox_sessions_account_active", "account_id", "destroyed_at"),
    )

    # Same UUID string passed to SandboxManager.create_session(session_id=...).
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pod_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Caller-supplied label for their own reference (see SandboxCreateRequest);
    # never sent to SandboxManager/the sandbox itself.
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Populated when destroyed_at is set; kept as a materialized column so
    # monthly-usage aggregation doesn't need to recompute it from timestamps
    # on every query. Active (not-yet-destroyed) sessions are accounted for
    # by computing elapsed time on the fly instead (see usage_policy.py).
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Set by the reaper when it tears a session down for exceeding
    # BOXKITE_MAX_SESSION_MINUTES, vs. a caller-initiated DELETE. Purely
    # informational.
    destroyed_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    account: Mapped["Account"] = relationship(back_populates="sandbox_sessions")


class McpConnection(Base):
    """Org-scoped outbound-MCP connection grant (GitHub issues #116/#117,
    docs/OUTBOUND-MCP-DESIGN.md §3). Models a session's access to one
    curated MCP catalog entry (mcp_catalog.py) -- the same
    (account_id, label) -> allowed-host shape `Secret` below already has,
    applied to a fixed, boxkite-reviewed catalog hostname instead of a
    caller-supplied one.

    `host` is resolved from the curated catalog at creation time via
    `mcp_catalog.resolve_catalog_host` and recorded here, the same
    "resolve once, store the resolved value" pattern `Secret.allowed_hosts`
    follows for its own (caller-supplied) hosts.

    Deliberately has no credential field: third-party OAuth credential
    handling for MCP catalog entries is an explicit open question this
    feature does not solve (docs/OUTBOUND-MCP-DESIGN.md §4/§7) -- this row
    only carries enough metadata to widen a session's network-layer egress
    allowlist (see secrets_network_policy.py's collect_allowed_hosts), not
    to let an agent actually speak MCP to the destination.

    Hard-deleted, same rationale as `Secret` below (a soft-delete marker
    would conflict with the (account_id, label) uniqueness constraint on
    recreation under the same label).
    """

    __tablename__ = "mcp_connections"
    __table_args__ = (
        UniqueConstraint("account_id", "label", name="uq_mcp_connections_account_label"),
        Index("ix_mcp_connections_account_created", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    catalog_id: Mapped[str] = mapped_column(String(100), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class Secret(Base):
    """Org-scoped secret for the proxy-substitution secrets broker
    (docs/SECRETS-DESIGN.md). The raw value is NEVER stored -- only an
    envelope-encrypted ciphertext (see secrets_kms.py) plus the metadata
    needed to decrypt and re-wrap it (`nonce`, `wrapped_data_key`,
    `encryption_key_id`).

    `allowed_hosts` is required at the schema level (non-nullable, no
    default) -- an unscoped secret usable against any destination defeats
    the entire point of this feature, per the design doc's §3.

    Hard-deleted (unlike Snapshot's soft-delete pattern above) -- a secret
    row carries no data worth retaining post-deletion the way a snapshot's
    accounting metadata is, and a soft-delete marker would conflict with the
    (account_id, name) uniqueness constraint on any attempt to recreate a
    secret under the same name after deleting it. `DELETE /v1/secrets/{id}`
    removes the row outright.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_secrets_account_name"),
        Index("ix_secrets_account_created", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    ciphertext: Mapped[str] = mapped_column(String, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    wrapped_data_key: Mapped[str] = mapped_column(String, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(String(200), nullable=False)
    allowed_hosts: Mapped[list] = mapped_column(JSON, nullable=False)
    # Nullable: only meaningful for wallet/private-key-style secrets
    # (docs/WALLET-SECRETS-DESIGN.md §3/§6/§11) -- an ordinary API-key-style
    # secret (a Stripe key, etc.) has no trust tier and leaves this unset.
    # When set, only "testnet" is accepted today (see routers/secrets.py's
    # creation-time validation) -- "mainnet" is refused outright since the
    # session-scoped signing mechanism §4b of that doc requires for a
    # mainnet-tier grant doesn't exist yet; accepting the label without the
    # enforcement mechanism it implies would be worse than not offering it.
    trust_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class RateLimitWindow(Base):
    """Fixed-window request counter backing `rate_limit.py`'s
    `PostgresRateLimiter` — the shared-store alternative to the default
    single-process, in-memory sliding-window limiter, for deployments
    running more than one control-plane replica (see rate_limit.py's module
    docstring for why the in-memory limiter alone silently multiplies the
    effective ceiling by replica count in that case).

    `key` mirrors the in-memory limiter's own key shape
    (`"{bucket}:{subject-or-ip}"`); `window_start` is the current window's
    floor(now / window_seconds) * window_seconds, so one row exists per
    key per window rather than one row per request. This is a coarser
    fixed-window counter, not the in-memory limiter's exact sliding window —
    a caller can burst up to ~2x the configured limit right at a window
    boundary, an accepted tradeoff standard to fixed-window limiters. This
    table defends against sustained brute-force/abuse volume across
    replicas, not exact-window precision.
    """

    __tablename__ = "rate_limit_windows"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RevokedPreviewToken(Base):
    """Denylist row for one revoked network-ingress preview-URL token
    (docs/NETWORK-INGRESS-DESIGN.md's former "no revocation before expiry"
    limitation, closed by this table).

    Preview tokens are stateless, signed JWTs (`security.create_preview_token`)
    -- there is nothing to delete server-side to invalidate one early, since
    the token itself carries everything needed to prove validity. Revocation
    is therefore a denylist: `jti` is the token's own unique id (a fresh
    UUID minted into every preview token's payload), and the proxy route
    (`routers/sandboxes.py`'s `proxy_sandbox_preview`) rejects any token
    whose `jti` has a row here, in addition to the signature/expiry/claim
    checks it already does.

    A row here is looked up by `jti` alone, never by `session_id`/`port` --
    those two columns exist for observability (so an operator can see what
    was revoked and for which sandbox) and are not part of the revocation
    check itself, which only cares about the specific token.

    `expires_at` bounds how long a revocation row needs to live: since the
    revoker only supplies the `jti` (not the original token, which may
    already be unknown/discarded by the time revocation is wanted), this
    table cannot know the token's *actual* expiry. Instead it stores the
    most conservative safe upper bound -- `revoked_at +
    SANDBOX_PREVIEW_MAX_TTL_SECONDS` -- which is guaranteed to be at or
    after the real token's own `exp` claim (since any token being revoked
    must have been minted at or before the revocation call, and no preview
    token's TTL exceeds SANDBOX_PREVIEW_MAX_TTL_SECONDS). A row can be safely
    purged once its own `expires_at` has passed, because the underlying JWT
    will have already independently expired by then regardless of whether
    this row still exists (`repository.py`'s
    `PreviewTokenRevocationRepository.revoke` opportunistically purges
    expired rows on every write, so this table doesn't grow unboundedly
    without needing a separate reaper job/infra).
    """

    __tablename__ = "revoked_preview_tokens"
    __table_args__ = (Index("ix_revoked_preview_tokens_expires_at", "expires_at"),)

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdminAccessLog(Base):
    """One row per call to any `/v1/admin/*` route (docs/ADMIN-ROLE-DESIGN.md)
    -- the accountability side of the admin-role concept: cross-account
    visibility is new, genuinely sensitive surface (see `Account.is_admin`'s
    docstring), so every access is durably logged, independent of whatever
    the operator's own infrastructure logging captures. Written before the
    route's handler runs (see `deps.get_current_admin_account`), so even a
    handler that raises after authorization still leaves a record that the
    admin account reached this far.
    """

    __tablename__ = "admin_access_log"
    __table_args__ = (
        Index("ix_admin_access_log_admin_created", "admin_account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    admin_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ExecLogEntry(Base):
    """One row per exec/file operation against a sandbox session — the
    durable audit trail described in
    `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3.

    Written by `routers/sandboxes.py`'s shared `_log_exec_entry` helper right
    after each operation succeeds, never by a route handler directly. `source`
    distinguishes agent-issued operations from a future human-takeover
    session — both write to this same table so the audit trail is a single,
    complete record regardless of who issued the command (see the design
    doc's "no fine-grained RBAC yet" section on why this logging is
    load-bearing, not a nice-to-have).
    """

    __tablename__ = "exec_log_entries"
    __table_args__ = (
        Index("ix_exec_log_entries_session_started", "session_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sandbox_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "agent" | "human_takeover" — see the design doc's §2/§4 on why the
    # latter is non-negotiable once takeover ships.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # "exec" | "file_create" | "view" | "str_replace" | "ls" | "glob" | "grep"
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    # Operation-specific info (command string, path, pattern, ...), truncated
    # to SANDBOX_FILE_CONTENT_MAX_LENGTH the same way request payloads are
    # capped elsewhere in this service (see schemas.py).
    detail: Mapped[dict] = mapped_column(JSON, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # First N characters of stdout/stderr (or equivalent operation output),
    # same cap philosophy as `detail` above. NULL for operations with no
    # meaningful output to capture (e.g. ls/glob just return structured
    # matches, already present in `detail`).
    output_truncated: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Hash-chained tamper-evidence (GitHub issue #136,
    # docs/TAMPER-EVIDENT-AUDIT-DESIGN.md). Nullable, additive columns --
    # rows written before this feature shipped keep NULL in both; chain
    # coverage begins at the first row with a non-NULL row_hash. Computed
    # and written in the same INSERT as the row itself
    # (ExecLogEntryRepository.create), never a follow-up UPDATE. See
    # control_plane.audit_chain / boxkite.audit for the shared hash formula
    # and verifier both audit surfaces (this table and the self-hosted
    # SQLiteAuditSink) run through.
    row_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Snapshot(Base):
    """Filesystem snapshot metadata (docs/SNAPSHOT-DESIGN.md).

    Every query against this table MUST be scoped by `account_id` at the
    database layer (see `SnapshotRepository`), the same structural
    cross-tenant guarantee `SandboxSession` already provides -- this is
    called out as the single highest-severity risk in the design doc's
    security section.
    """

    __tablename__ = "snapshots"
    __table_args__ = (
        Index("ix_snapshots_account_created", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable + SET NULL (not CASCADE): a snapshot must outlive the session
    # it was taken from -- destroying the source session must never destroy
    # the snapshot's row or its underlying storage objects.
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sandbox_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Namespaced by account_id (`snapshots/{account_id}/{snapshot_id}`) so a
    # bug in the DB-layer authorization check isn't the only thing standing
    # between two tenants' snapshot data -- see the design doc's security
    # section.
    storage_key_prefix: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "pending" (copy in flight) | "completed" | "failed"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # Soft-delete marker, same pattern as SandboxSession.destroyed_at --
    # DELETE .../snapshots/{id} sets this AND deletes the underlying
    # storage objects; it never merely hides the row.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SandboxImage(Base):
    """Declarative-builder custom image metadata
    (docs/DECLARATIVE-BUILDER-DESIGN.md). Every query against this table
    MUST be scoped by `account_id` (see `SandboxImageRepository`), the same
    structural cross-tenant guarantee `Snapshot` provides -- a foreign
    `image_id` must 404, never leak existence.

    `status` progresses queued -> building -> scanning -> completed, or
    failed/rejected at any stage. A build that fails its vulnerability-scan
    gate is `rejected`, never silently promoted to `completed` -- see the
    design doc's section 3.

    `cache_key` is a deterministic hash of (base, sorted python_packages,
    sorted apt_packages, sorted npm_packages) -- see `image_builder.cache_key_for`. A new build
    request whose cache_key matches an already-`completed` image for the
    SAME account, created within BOXKITE_IMAGE_BUILD_CACHE_HOURS, reuses
    that image's digest/registry_ref instead of re-running the build
    (the design doc's "24h cache" requirement). Cache reuse is scoped to
    the requesting account only -- it never reads or reuses another
    account's build, even for an identical spec, so a build cache hit can
    never leak one account's package list/build artifact to another.
    """

    __tablename__ = "sandbox_images"
    __table_args__ = (
        Index("ix_sandbox_images_account_created", "account_id", "created_at"),
        Index("ix_sandbox_images_account_cache_key", "account_id", "cache_key", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Legal values today: "boxkite-default", "boxkite-minimal" -- see
    # schemas.py:SandboxImageBuildRequest.base. Stored as a plain string
    # (not a DB enum) so adding a future legal base doesn't need a schema
    # migration, but the *set* of legal values is still enforced entirely
    # at the Pydantic layer, never here.
    base: Mapped[str] = mapped_column(String(64), nullable=False)
    python_packages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    apt_packages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    npm_packages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "queued" | "building" | "scanning" | "completed" | "failed" | "rejected"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    # Immutable digest, e.g. "sha256:9f2c...". Set only once status ==
    # "completed" -- a pod is only ever created from `registry_ref`, which
    # embeds this digest, never a mutable tag (design doc section 5).
    digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # "registry.internal/boxkite-images/{account_id}/{image_id}@{digest}" --
    # namespaced by account_id so a bug in the DB-layer authorization check
    # isn't the only thing standing between two accounts' custom images,
    # same rationale as Snapshot.storage_key_prefix above.
    registry_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    scan_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft-delete marker, same pattern as Snapshot.deleted_at. DELETE
    # .../images/{id} sets this AND removes the registry object; it never
    # merely hides the row. Already-running sandboxes referencing this
    # image_id's digest keep running (they resolved the digest at create
    # time) -- deleting the control plane's record does not retroactively
    # kill a pod, per the design doc's explicit DELETE section.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class OAuthClient(Base):
    """One row per MCP client that has dynamically registered itself via
    `POST /oauth/register` (RFC 7591). See
    docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.1.

    Always `client_type == "public"` -- MCP clients are treated as public
    clients per RFC 8252/native-app guidance, so no `client_secret` is ever
    issued or stored here; PKCE (mandatory, S256-only) is the confidentiality
    mechanism instead. `redirect_uris` is matched exactly (no wildcarding)
    against the caller-supplied `redirect_uri` at both `/oauth/authorize`
    and `/oauth/token`.
    """

    __tablename__ = "oauth_clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False)
    client_type: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class OAuthAuthorizationCode(Base):
    """One row per in-flight `GET /oauth/authorize` grant -- single-use,
    short TTL (`BOXKITE_MCP_AUTH_CODE_TTL_SECONDS`). See
    docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.1/§3.2.

    `account_id` is set once the resource owner approves on the consent
    screen. `consumed_at` is set atomically on exchange at `/oauth/token` --
    a second exchange attempt against the same code must fail, the same
    single-use property `docs/NETWORK-INGRESS-DESIGN.md`'s preview tokens
    already have for a different flow. `code_challenge_method` is always
    `"S256"` -- plain is rejected at the API layer, never stored.
    """

    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (Index("ix_oauth_auth_codes_expires_at", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(16), nullable=False, default="S256")
    scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class OAuthToken(Base):
    """One row per issued refresh token (access tokens are stateless JWTs,
    never stored here -- see docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.3).

    `refresh_token_hash` uses the same `hash_secret` (SHA-256) helper API
    keys already use -- the raw refresh token is never persisted. `rotated_from`
    chains each rotation back to the token it replaced, so a reuse-detection
    hit (a caller presenting an already-rotated refresh token) can revoke the
    entire chain, not just the current row, per OAuth 2.1's own
    recommendation on refresh-token-theft response.
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (Index("ix_oauth_tokens_account_created", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("oauth_tokens.id", ondelete="SET NULL"), nullable=True
    )


class SandboxVolume(Base):
    """Independent PVC-backed block-storage volume
    (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum) --
    E2B's `e2b.Volume` equivalent, NOT the FUSE object-storage mount the
    rest of that doc scopes (see the addendum for why these are two
    different things). Same cross-tenant-404 pattern as SandboxImage/
    Snapshot: every query MUST be scoped by `account_id`.

    `status` progresses queued -> creating -> ready, or failed at any
    stage. `pvc_name` is set only once status == "ready" -- a sandbox can
    only mount a volume by referencing its PVC name, and a pod is only
    ever created from a volume whose PVC actually exists (never a
    still-provisioning or failed one).
    """

    __tablename__ = "sandbox_volumes"
    __table_args__ = (Index("ix_sandbox_volumes_account_created", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_gb: Mapped[float] = mapped_column(Float, nullable=False)
    # "queued" | "creating" | "ready" | "failed" | "deleting"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    # Set only once status == "ready" -- the PersistentVolumeClaim name a
    # sandbox pod spec references directly, namespaced per account/volume
    # the same way SandboxImage.registry_ref is.
    pvc_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # Soft-delete marker, same pattern as SandboxImage.deleted_at. DELETE
    # .../volumes/{id} sets this AND deletes the underlying PVC; a sandbox
    # pod already mounting this volume keeps running (Kubernetes itself
    # keeps a PVC bound to a running pod alive until the pod is gone), same
    # "control-plane record deletion isn't retroactive" rule as images.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class WebhookSubscription(Base):
    """Outbound webhook registration (docs/WEBHOOKS-DESIGN.md). Follows
    `SandboxImageRepository`'s exact cross-tenant pattern: every lookup
    takes `account_id` and folds it into the WHERE clause.

    The signing secret is envelope-encrypted at rest using the exact same
    `secrets_kms.py` primitive `Secret.ciphertext`/`nonce`/`wrapped_data_key`/
    `encryption_key_id` already use -- a webhook signing secret is exactly
    as sensitive as an org secret (whoever holds it can forge a delivery a
    receiver would trust), so it gets the same at-rest protection, not a
    weaker one. The raw secret is returned to the caller exactly once, in
    the create response, and re-derived only inside `webhook_delivery.py`
    to compute each delivery's HMAC signature -- never returned by any GET.

    `event_types` is a JSON list of event-type strings (see
    `webhooks.py`'s `WEBHOOK_EVENT_TYPES` for the fixed set this ships
    with -- "sandbox.created"/"sandbox.destroyed"/"audit_log.entry", the
    last one added for SIEM/audit-log export, GitHub issue #125).
    `is_active` lets a caller pause delivery without losing the
    registration/secret; deleted subscriptions are hard-deleted (same
    rationale as `Secret` -- no residual data worth keeping, and a
    soft-delete marker would complicate re-creating a subscription for the
    same URL).

    `payload_format` selects the body shape `webhook_delivery.py` sends:
    `"boxkite_v1"` (default, the envelope documented above) or
    `"splunk_hec"` (docs/WEBHOOKS-DESIGN.md's audit-log-export addendum) --
    the latter wraps the same event envelope in a Splunk HTTP Event
    Collector-shaped body (`{"time", "host", "source", "sourcetype",
    "event"}`) so it can be POSTed directly at a Splunk HEC endpoint. The
    `hec_token_*` columns are an OPTIONAL destination credential (the
    receiver's own HEC token, sent back as `Authorization: Splunk <token>`
    on delivery) -- not this project's own signing secret, and not
    required for `payload_format="splunk_hec"` to work, but supported for
    receivers that require it. It gets the exact same envelope-encryption
    treatment as `ciphertext`/`nonce`/`wrapped_data_key`/`encryption_key_id`
    above, for the same reason: a destination ingestion token is exactly
    as sensitive as an org secret or this subscription's own signing
    secret, and is never returned by any GET.
    """

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("ix_webhook_subscriptions_account_created", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    event_types: Mapped[list] = mapped_column(JSON, nullable=False)
    ciphertext: Mapped[str] = mapped_column(String, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    wrapped_data_key: Mapped[str] = mapped_column(String, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payload_format: Mapped[str] = mapped_column(String(32), nullable=False, default="boxkite_v1")
    hec_token_ciphertext: Mapped[str | None] = mapped_column(String, nullable=True)
    hec_token_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hec_token_wrapped_data_key: Mapped[str | None] = mapped_column(String, nullable=True)
    hec_token_encryption_key_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class WebhookDelivery(Base):
    """One row per fired-event-x-matching-subscription delivery attempt
    (docs/WEBHOOKS-DESIGN.md). Written once when the event is enqueued
    (`status="pending"`), then updated in place by
    `webhook_delivery.py`'s background worker on each attempt -- never
    re-created per retry, so `attempt_count`/`next_attempt_at` track a
    single delivery's full retry history.

    `status` progresses pending -> delivered, or pending -> failed once
    `attempt_count` reaches `BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS` without
    a 2xx response. `account_id` is denormalized from the parent
    subscription (not just reachable via a join) so this table can be
    queried/scoped the same account-first way every other table in this
    module is, without requiring a join for the common case.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_subscription_created", "subscription_id", "created_at"),
        Index("ix_webhook_deliveries_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    subscription_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # The full JSON event envelope (see webhooks.py:build_event_payload) --
    # stored so a retry re-sends byte-identical content and so delivery
    # history is inspectable after the fact.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # "pending" | "delivered" | "failed"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # First N characters only -- same truncation philosophy as
    # ExecLogEntry.output_truncated; a webhook receiver's response body is
    # caller-controlled-sized data ending up in this database.
    response_body_truncated: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    """Opt-in dashboard-JWT refresh token (issue #79), gated by
    `BOXKITE_REFRESH_TOKENS_ENABLED` (off by default -- see
    routers/auth.py). Follows `ApiKey`'s exact credential-storage shape:
    only `token_hash` (SHA-256 of the raw token) is ever persisted, the raw
    value is returned to the caller exactly once, at issuance.

    Rotation, not reuse: `POST /v1/auth/refresh` always revokes the
    presented token and mints a brand new one in the same request --
    `revoked_at` being already set when a token is presented again is
    therefore a replay signal (the same raw token used twice), not a normal
    state, and is treated as evidence of token theft -- see
    `RefreshTokenRepository.revoke_all_for_account`, called from
    routers/auth.py's refresh handler on exactly this condition.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_account_created", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class PasswordResetToken(Base):
    """Opt-in password-reset token (issue #79), gated by
    `BOXKITE_PASSWORD_RESET_ENABLED` (off by default -- see
    routers/auth.py). Same credential-storage shape as `ApiKey`/
    `RefreshToken`: only `token_hash` is ever persisted; the raw token is
    handed to `EmailSender` exactly once and never logged or echoed back
    over the API (see `email_sender.py`).

    `used_at` (not a boolean) records both "was this token redeemed" and,
    incidentally, when -- set by `POST /v1/auth/password-reset/confirm` on
    success, and also stamped on any other still-active token for the same
    account at that point (`PasswordResetTokenRepository.invalidate_active_for_account`)
    so at most one outstanding reset link is ever valid for an account.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (Index("ix_password_reset_tokens_account_created", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class EmailVerificationToken(Base):
    """Opt-in email-verification token (issue #79), gated by
    `BOXKITE_EMAIL_VERIFICATION_ENABLED` (off by default -- see
    routers/auth.py). Same credential-storage shape as `PasswordResetToken`
    above -- only `token_hash` is persisted, `used_at` marks redemption."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (Index("ix_email_verification_tokens_account_created", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship()


class IdempotencyKey(Base):
    """Caches the response of a POST that carried an `Idempotency-Key` header so
    a client retry after a network blip replays the original outcome instead of
    creating a duplicate resource — the Stripe idempotency pattern (see
    idempotency.py, the ASGI middleware that reads/writes these rows). Only
    requests that actually send the header ever touch this table; ordinary
    traffic is unaffected.

    `scope_hash` is a SHA-256 of the key + caller identity + path, so the same
    key from a different account/route can't collide. `response_status` is NULL
    while the original request is still in flight (a concurrent retry then gets
    409); once set, the row is a completed, replayable result.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_created_at", "created_at"),)

    scope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    response_media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Organization(Base):
    """A basic team/organization: a named group an account creates and adds
    other accounts to (see OrganizationMember). Deliberately minimal for now —
    sandbox ownership is still keyed on `account_id`; this adds the org/team
    *entity* and membership so shared-ownership scoping can build on it later
    without another schema migration. No billing concepts (fair-use only)."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationMember(Base):
    """One account's membership in one organization, with a coarse role
    (`owner`/`admin`/`member`). Unique per (organization, account)."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "account_id", name="uq_org_member"),
        Index("ix_organization_members_account", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="members")
    account: Mapped["Account"] = relationship()


class OrganizationInvite(Base):
    """A pending invitation to join an organization (org/team, issue #225).
    Lets an owner/admin invite someone by email even before that person has a
    boxkite account. Only a SHA-256 hash of the single-use token is stored
    (`token_hash`, same posture as api_keys / email_verification_tokens); the
    raw token is returned to the inviter exactly once. Redeemed via
    `POST /v1/organizations/accept-invite`, which requires the accepting
    account's email to match `email`."""

    __tablename__ = "organization_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    invited_by_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship()
