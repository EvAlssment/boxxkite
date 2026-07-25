"""Control-plane configuration, entirely env-var driven — no hardcoded values.

Fair-use limits are deliberately named and documented as usage limits, never
as pricing tiers or plan names — see the module docstring in `__init__.py`.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # ── Persistence ──────────────────────────────────────────────────────
    # Defaults to a local SQLite file for zero-config local dev. Production
    # deployments must set a real Postgres DSN (this org's standard store —
    # see CLAUDE.md/README for the shared-Postgres pattern used elsewhere in
    # the stack). Async SQLAlchemy dialect prefixes are required:
    #   postgresql+asyncpg://user:pass@host:5432/boxkite_control_plane
    # The validator below auto-upgrades a bare `postgres://`/`postgresql://`
    # (no `+driver` suffix) to `postgresql+asyncpg://` -- every managed
    # Postgres provider's own connection-string property (Render, Heroku,
    # Railway, ...) hands back the bare scheme, and create_async_engine()
    # (db.py) raises immediately on it otherwise. A DSN that already names a
    # driver (including a non-asyncpg one) is left untouched.
    DATABASE_URL: str = "sqlite+aiosqlite:///./control_plane.db"

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        for bare_scheme in ("postgresql://", "postgres://"):
            if value.startswith(bare_scheme):
                return "postgresql+asyncpg://" + value[len(bare_scheme) :]
        return value

    # ── Environment ──────────────────────────────────────────────────────
    # Gates the JWT_SECRET fail-fast check below. Defaults to "development"
    # so zero-config local dev keeps working; any real deployment must set
    # this to something other than "development"/"dev"/"test"/"testing"
    # (case-insensitive) for the placeholder-secret check to actually fail
    # startup instead of just warning.
    ENVIRONMENT: str = "development"

    # ── API docs ─────────────────────────────────────────────────────────
    # Swagger UI (/docs), ReDoc (/redoc), and the raw OpenAPI schema
    # (/openapi.json) advertise every route/param/schema shape on this
    # multi-tenant API. Auth is enforced identically whether or not these
    # are served, so this isn't an authorization control -- but the full
    # surface shouldn't be trivially discoverable to anyone browsing the
    # URL before an intentional public launch of the docs. Left unset
    # (None) by default: enabled automatically in dev/test ENVIRONMENT (see
    # `is_dev_environment` below) for zero-config local iteration, disabled
    # everywhere else. Set explicitly to override either direction
    # regardless of ENVIRONMENT.
    ENABLE_API_DOCS: bool | None = None

    # ── Observability ────────────────────────────────────────────────────
    # Prometheus exposition at GET /metrics (request counts + latency
    # histogram). On by default; 404s when disabled, same opt-out convention
    # as ENABLE_API_DOCS. Labels use matched route *templates*, so cardinality
    # stays bounded, but operators fronting a public ingress should still scope
    # /metrics to their scrape network.
    BOXKITE_METRICS_ENABLED: bool = True
    # Emit logs as structured JSON. None (default) = auto: JSON in a real
    # deployment, human-readable text in a dev/test ENVIRONMENT. Set explicitly
    # to force either format.
    BOXKITE_JSON_LOGS: bool | None = None

    # ── Auth ─────────────────────────────────────────────────────────────
    # No default in any real deployment: startup fails fast if this is left
    # at the placeholder value while ENVIRONMENT is not one of the dev/test
    # values above (see main.py's startup check). A default is still
    # provided here so `pydantic-settings` doesn't require it for tooling
    # that only imports this module (e.g. Alembic-style scripts).
    JWT_SECRET: str = "insecure-dev-secret-change-me-32-bytes-minimum"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 30

    # ── Refresh tokens, password reset, email verification (issue #79) ──
    # All three are new credential paths / new attack surface, so all three
    # follow this repo's standard convention (BOXKITE_IMAGE_BUILDER_ENABLED,
    # BOXKITE_VOLUMES_ENABLED, enable_git_tools, ...): off by default, an
    # explicit opt-in required. See routers/auth.py's module docstring for
    # the full behavior of each flag.
    BOXKITE_REFRESH_TOKENS_ENABLED: bool = False
    # Long-lived on purpose (a refresh token's whole point is to avoid
    # re-entering a password every ACCESS_TOKEN_TTL_MINUTES) but still
    # rotated on every use and revocable -- see RefreshToken's docstring.
    REFRESH_TOKEN_TTL_DAYS: int = 30
    BOXKITE_PASSWORD_RESET_ENABLED: bool = False
    PASSWORD_RESET_TOKEN_TTL_MINUTES: int = 30
    BOXKITE_EMAIL_VERIFICATION_ENABLED: bool = False
    EMAIL_VERIFICATION_TOKEN_TTL_HOURS: int = 24
    # Separate, deliberately low rate-limit buckets -- these routes are
    # unauthenticated (password-reset request) or low-frequency-by-design
    # (refresh, verify), so they get their own conservative ceilings rather
    # than inheriting BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE's signup/login
    # sizing. Keyed per-IP (request/confirm are pre-auth) except refresh,
    # which is keyed by the account the presented token resolves to.
    BOXKITE_PASSWORD_RESET_RATE_LIMIT_PER_MINUTE: int = 5
    BOXKITE_EMAIL_VERIFICATION_RATE_LIMIT_PER_MINUTE: int = 5
    BOXKITE_REFRESH_RATE_LIMIT_PER_MINUTE: int = 20

    # API keys are presented as `bxk_live_<random>` so a leaked key is
    # greppable in logs/history and instantly identifiable as a boxkite
    # control-plane credential.
    API_KEY_PREFIX: str = "bxk_live"

    # How long an organization invite token stays redeemable (org/team,
    # issue #225). One week by default -- long enough to be practical,
    # short enough that a leaked invite link doesn't linger indefinitely.
    BOXKITE_ORG_INVITE_TTL_HOURS: int = 168

    # ── MCP OAuth 2.1 (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md) ────────
    # Default-enabled following the dedicated security review GitHub issue
    # #114 asked for (PKCE downgrade, redirect_uri validation, refresh-
    # token-reuse-detection correctness under concurrency, the
    # email-collision account-takeover vector) -- see §7 of the design doc
    # for the review's findings and fixes. Stands up a full OAuth
    # authorization server (Dynamic Client Registration, a consent
    # screen, token issuance). hosted_mcp.py's /mcp/ endpoint accepts the
    # existing static API key regardless of this flag; this only adds the
    # second, OAuth-issued token path on top.
    BOXKITE_MCP_OAUTH_ENABLED: bool = True
    # Short TTL -- the access token itself can't be individually revoked
    # before expiry (it's a self-contained JWT, same tradeoff the existing
    # dashboard access token already accepts); only the refresh token is
    # revocable. Keep this short so a leaked access token has a small
    # window, and rely on refresh-token rotation for the actual revocation
    # story.
    BOXKITE_MCP_ACCESS_TOKEN_TTL_MINUTES: int = 15
    # Sliding -- successfully rotating a refresh token resets this window.
    # Reuse of an already-rotated refresh token revokes the whole chain
    # (see OAuthToken.rotated_from), per OAuth 2.1's own recommendation.
    BOXKITE_MCP_REFRESH_TOKEN_TTL_DAYS: int = 30
    # How long an OAuthAuthorizationCode/a DCR-flow login session cookie
    # stays valid -- both are meant to be consumed within one browser
    # round trip, not held onto.
    BOXKITE_MCP_AUTH_CODE_TTL_SECONDS: int = 60
    BOXKITE_MCP_LOGIN_SESSION_TTL_MINUTES: int = 15
    # POST /oauth/register is intentionally unauthenticated (RFC 7591's
    # whole point is letting a client register itself with no prior
    # relationship) -- its own, deliberately conservative rate-limit
    # bucket, same rationale BOXKITE_IMAGE_BUILD_RATE_LIMIT_PER_MINUTE
    # already has for a different open-to-anyone-ish endpoint.
    BOXKITE_OAUTH_DCR_RATE_LIMIT_PER_MINUTE: int = 10

    # ── GitHub/Google social login (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md) ──
    # Default-enabled following the same GitHub issue #114 security review
    # as BOXKITE_MCP_OAUTH_ENABLED above (the login-CSRF/session-fixation
    # fix in particular applies here -- see security.py's
    # create_social_login_state_token docstring). Each provider is still
    # simply inactive (its login button doesn't render, its routes 404)
    # until BOTH its client id and secret are set below -- flipping this
    # flag alone does nothing without operator-provisioned credentials.
    # Those must be created by an operator on
    # github.com/settings/developers and Google Cloud Console's
    # Credentials page respectively -- this cannot be provisioned by this
    # codebase or an agent working in it; it requires the operator's own
    # account action, same "N operator-reviewed choices" posture
    # BOXKITE_BASE_IMAGE_REFS already has for a different feature.
    BOXKITE_SOCIAL_LOGIN_ENABLED: bool = True
    GITHUB_OAUTH_CLIENT_ID: str = ""
    GITHUB_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""

    @property
    def github_oauth_configured(self) -> bool:
        return bool(self.GITHUB_OAUTH_CLIENT_ID and self.GITHUB_OAUTH_CLIENT_SECRET)

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.GOOGLE_OAUTH_CLIENT_ID and self.GOOGLE_OAUTH_CLIENT_SECRET)

    # ── Enterprise SSO (docs/ENTERPRISE-SSO-DESIGN.md, issue #126 Phase 1) ──
    # Off by default, same "new auth surface stays opt-in pending review"
    # convention as BOXKITE_SOCIAL_LOGIN_ENABLED/BOXKITE_MCP_OAUTH_ENABLED.
    # Phase 1 (SAML/OIDC login via a hosted-SSO-as-a-service broker) only --
    # SCIM provisioning is a separate, later phase, not gated by this flag.
    BOXKITE_ENTERPRISE_SSO_ENABLED: bool = False
    # Which hosted-SSO-as-a-service backend `enterprise_sso_client.
    # get_enterprise_sso_client` constructs. "workos" is the only backend
    # implemented in this pass -- mirrors SECRETS_KMS_BACKEND/
    # SNAPSHOT_STORAGE_BACKEND's "named backend behind one settings string"
    # pattern so a future from-scratch SAML backend (e.g. "saml") can be
    # added as a second named value without changing this setting's shape.
    ENTERPRISE_SSO_PROVIDER: str = "workos"
    WORKOS_API_KEY: str = ""
    WORKOS_CLIENT_ID: str = ""

    @property
    def enterprise_sso_configured(self) -> bool:
        if self.ENTERPRISE_SSO_PROVIDER == "workos":
            return bool(self.WORKOS_API_KEY and self.WORKOS_CLIENT_ID)
        return False

    # ── SCIM 2.0 provisioning via WorkOS Directory Sync (Phase 2 of issue
    # #126, docs/ENTERPRISE-SSO-DESIGN.md) ───────────────────────────────
    # Off by default, same "new auth/provisioning surface stays opt-in
    # pending review" convention as BOXKITE_ENTERPRISE_SSO_ENABLED/
    # BOXKITE_SOCIAL_LOGIN_ENABLED/BOXKITE_MCP_OAUTH_ENABLED. Deliberately
    # its OWN flag, independent of BOXKITE_ENTERPRISE_SSO_ENABLED -- the
    # design doc's Phase 1 section is explicit that SCIM is "a separate,
    # later phase, not gated by this flag," so an operator can run
    # interactive SSO login without SCIM provisioning, or (less commonly)
    # SCIM provisioning without the interactive login route, rather than
    # the two being force-coupled.
    BOXKITE_SCIM_PROVISIONING_ENABLED: bool = False
    # The WorkOS Directory Sync webhook endpoint's own signing secret --
    # NOT the same credential as WORKOS_API_KEY/WORKOS_CLIENT_ID above.
    # Those authenticate this control-plane as an OAuth-style client
    # calling OUT to WorkOS's /sso/token endpoint (Phase 1); this secret
    # instead lets this control-plane verify an INBOUND webhook actually
    # came from WorkOS (a per-webhook-endpoint value an operator copies
    # from the WorkOS dashboard when registering the webhook URL). Left
    # empty by default -- POST /v1/auth/sso/scim-webhook 404s until BOTH
    # BOXKITE_SCIM_PROVISIONING_ENABLED and this are set, mirroring
    # github_oauth_configured's "both must be set" contract.
    WORKOS_WEBHOOK_SECRET: str = ""
    # Replay-protection window for the WorkOS-Signature header's `t=`
    # (epoch-milliseconds) field -- a delivery whose timestamp is further
    # than this from "now" (either direction) is rejected even with an
    # otherwise-valid signature. 180s matches WorkOS's own SDKs' default
    # tolerance (their Ruby SDK defaults to 180s; other docs describe a
    # 3-5 minute range) -- see docs/ENTERPRISE-SSO-DESIGN.md's SCIM
    # section for the citation.
    SCIM_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS: int = 180
    # A real WorkOS Directory User event is a few KB at most (see this
    # payload shape in docs/ENTERPRISE-SSO-DESIGN.md's SCIM section) --
    # 256 KiB gives generous headroom while still turning a wildly
    # oversized delivery (accidental or hostile) into a fast 413 instead of
    # spending a HMAC computation and a full JSON parse on it.
    SCIM_WEBHOOK_MAX_BODY_BYTES: int = 262_144

    @property
    def scim_provisioning_configured(self) -> bool:
        return bool(self.WORKOS_WEBHOOK_SECRET)

    # Its own, conservative rate-limit bucket -- this is an unauthenticated
    # (signature-authenticated, not API-key) endpoint that can create/
    # deactivate Account rows, so it gets the same "own bucket, don't
    # inherit a shared default" treatment BOXKITE_OAUTH_DCR_RATE_LIMIT_PER_MINUTE/
    # BOXKITE_WEBHOOK_RATE_LIMIT_PER_MINUTE already have. Sized higher than
    # those two since a real directory sync can legitimately fire many
    # user events in a short burst (e.g. an initial bulk sync of an
    # existing IdP directory).
    BOXKITE_SCIM_WEBHOOK_RATE_LIMIT_PER_MINUTE: int = 60

    # How long the signed `state` JWT round-tripped through the hosted SSO
    # broker's redirect stays valid -- same CSRF-defense role and TTL choice
    # as SOCIAL_LOGIN_STATE_TTL_SECONDS (security.py), just its own named
    # setting since the two flows are otherwise independent.
    ENTERPRISE_SSO_STATE_TTL_SECONDS: int = 600

    # Public, externally-reachable base URL of THIS control-plane deployment
    # (no trailing slash), e.g. "https://api.your-boxkite-host.example.com".
    # Used only to build an absolute preview URL in
    # POST /v1/sandboxes/{id}/preview/{port}'s response
    # (docs/NETWORK-INGRESS-DESIGN.md) -- left empty by default, in which
    # case that route returns a path-only URL and the caller is responsible
    # for prefixing their own known base URL.
    BOXKITE_PUBLIC_URL: str = ""

    # The dashboard's own origin (site/app/dashboard/, a separate Next.js
    # deployment from this control-plane) -- used by routers/social_login.py
    # to allow a standalone GitHub/Google login to redirect back into
    # `{BOXKITE_DASHBOARD_URL}/dashboard/oauth-callback` after the provider
    # approves, instead of returning a raw TokenResponse JSON body with
    # nowhere to land it. Left empty by default (routes 404/fall back to the
    # raw-JSON response), since this is a real, exact-match allowlisted
    # redirect target -- an operator must explicitly configure their own
    # dashboard's real origin, never a caller-supplied value.
    BOXKITE_DASHBOARD_URL: str = ""

    # ── Fair-use limits (NOT pricing tiers — see module docstring) ──────
    # Rolling calendar-month cap on total sandbox-hours consumed per account.
    BOXKITE_FREE_MONTHLY_SANDBOX_HOURS: float = 20.0
    # Hard per-session wall-clock cap; the reaper (see reaper.py) tears down
    # any session that exceeds this, regardless of activity.
    BOXKITE_MAX_SESSION_MINUTES: int = 30
    # Max sessions with no destroyed_at (i.e. still "live") per account.
    BOXKITE_MAX_CONCURRENT_SANDBOXES: int = 2
    # Cluster-wide ceiling across ALL accounts combined, independent of the
    # per-account cap above. Without this, enough accounts hitting their own
    # (much smaller) per-account cap could still collectively exhaust node
    # capacity -- each sandbox pod requests ~192Mi/75m (sandbox + sidecar
    # containers, see deploy/pod-template.yaml), so this default is a
    # deliberately conservative fraction of a small node pool's real
    # capacity, not a hard ceiling on what the cluster can technically run.
    # Raise it once real usage and node pool size justify it.
    BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES: int = 20
    # Largest size an account may request without operator override -- see
    # SandboxManager.create_session's size param ("small"/"medium"/"large").
    # Ordered small < medium < large; requesting a size above this ceiling
    # raises a 429 in usage_policy.py before SandboxManager is ever called.
    BOXKITE_MAX_SANDBOX_SIZE: str = "medium"
    # Ceiling on the per-account storage_gb override passed through to
    # SandboxManager.create_session, mirroring
    # resource_config.max_volume_size_limit_gi()'s own default so this
    # control-plane-level cap doesn't diverge from the manager's.
    BOXKITE_MAX_SANDBOX_STORAGE_GB: float = 20.0

    BOXKITE_MAX_ALLOWLIST_RULES: int = 50
    BOXKITE_MAX_ALLOWLIST_PATTERN_LENGTH: int = 200

    # ── Abuse resistance ─────────────────────────────────────────────────
    # "memory" (default): in-memory, per-process sliding-window limiter --
    # correct and fully self-contained for a SINGLE control-plane replica,
    # but each additional replica silently multiplies the effective ceiling
    # (replica count * limit), since state isn't shared. "postgres": a
    # shared, cross-replica fixed-window limiter backed by this service's
    # own database (rate_limit.py's PostgresRateLimiter / models_orm.py's
    # RateLimitWindow table) -- reuses DATABASE_URL, no new infra to run.
    # Any deployment running more than one control-plane replica MUST set
    # this to "postgres"; it is not auto-detected (a single process has no
    # way to know how many replicas exist).
    BOXKITE_RATE_LIMIT_BACKEND: str = "memory"
    # In-memory, per-process sliding-window cap on /v1/auth/signup and
    # /v1/auth/login requests per source IP. See BOXKITE_RATE_LIMIT_BACKEND
    # above for the multi-replica caveat.
    BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE: int = 10
    # Same "memory" vs "postgres" choice as BOXKITE_RATE_LIMIT_BACKEND, but
    # for usage_policy.py's create_session count-check-then-reserve critical
    # section instead of rate limiting. "memory" (default): a process-local
    # asyncio.Lock -- correct for a single replica, but each additional
    # replica can independently observe the same "below cap" count before
    # any of them commits its reservation row, letting
    # BOXKITE_MAX_CONCURRENT_SANDBOXES/BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES
    # be exceeded cluster-wide. "postgres": a cross-replica mutex via this
    # service's own database (pg_advisory_xact_lock -- see
    # usage_policy.py's PostgresSessionLock), serializing that same critical
    # section across every replica. Postgres-only (advisory locks aren't
    # portable to SQLite, unlike BOXKITE_RATE_LIMIT_BACKEND's counter-table
    # approach) -- any multi-replica deployment on real Postgres MUST set
    # this to "postgres"; it is not auto-detected.
    BOXKITE_USAGE_LOCK_BACKEND: str = "memory"
    # In-memory, per-process sliding-window cap on sandbox exec/file-op
    # requests (/exec, /files, /files/view, /files/str-replace), keyed per
    # account rather than per-IP since these routes are already
    # API-key-authenticated. Higher than the auth limit since legitimate
    # agent usage issues many of these per minute.
    BOXKITE_SANDBOX_RATE_LIMIT_PER_MINUTE: int = 120
    # Separate, much lower cap on sandbox *lifecycle* requests (create,
    # destroy) -- these trigger real Kubernetes pod create/delete calls
    # against the shared cluster, unlike the higher-volume exec/file-op
    # bucket above, so they need a tighter ceiling of their own.
    BOXKITE_SANDBOX_LIFECYCLE_RATE_LIMIT_PER_MINUTE: int = 20
    # A separate, lower bucket for snapshot create/restore/delete -- these
    # are potentially large storage-side copy operations (a full workspace
    # copy), heavier than either bucket above, per
    # docs/SNAPSHOT-DESIGN.md's security section ("treat them as a distinct
    # bucket rather than silently inheriting the exec/file-op limit").
    BOXKITE_SNAPSHOT_RATE_LIMIT_PER_MINUTE: int = 10
    # Separate bucket for proxied preview-URL requests
    # (docs/NETWORK-INGRESS-DESIGN.md) -- these are public, unauthenticated
    # (token-authenticated, not API-key) requests keyed per session_id
    # rather than per account, and a single page load can fan out into many
    # asset requests, so this is intentionally higher than the exec/file-op
    # bucket.
    BOXKITE_PREVIEW_RATE_LIMIT_PER_MINUTE: int = 300

    # ── Snapshots (filesystem snapshot/restore, docs/SNAPSHOT-DESIGN.md) ──
    # Per-account cap on the number of non-deleted snapshots that may exist
    # at once -- an unbounded number of full filesystem copies per account
    # is a storage-cost and quota gap, not just an API nicety (see the
    # design doc's API section on POST .../snapshots). Exceeding this
    # returns a 429 `snapshot_limit_reached` before any storage-side copy
    # is attempted.
    BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT: int = 10

    # ── Snapshot storage credential (distinct from the sidecar's own) ────
    # The control plane's storage-side copy for snapshot create/restore
    # needs its own credential, per the design doc's security section --
    # "don't reuse the sidecar's broader storage credential for this". This
    # mirrors STORAGE_BACKEND/STORAGE_S3_*/AZURE_STORAGE_* (see
    # src/boxkite/manager.py and sidecar/main.py) but with a SNAPSHOT_
    # prefix so the two credential sets are configured, rotated, and scoped
    # independently -- see docs/CONFIGURATION.md for the recommended
    # least-privilege IAM policy shape.
    SNAPSHOT_STORAGE_BACKEND: str = "s3"  # "s3" or "azure"
    SNAPSHOT_STORAGE_S3_BUCKET: str = ""
    SNAPSHOT_STORAGE_S3_REGION: str = "us-east-1"
    SNAPSHOT_STORAGE_S3_ENDPOINT: str = ""
    SNAPSHOT_STORAGE_S3_ACCESS_KEY_ID: str = ""
    SNAPSHOT_STORAGE_S3_SECRET_ACCESS_KEY: str = ""
    SNAPSHOT_STORAGE_S3_SESSION_TOKEN: str = ""
    # Preserve the same SSE-KMS settings normal session sync uploads get
    # (src/boxkite/manager.py's STORAGE_S3_KMS_KEY_ID/
    # STORAGE_S3_BUCKET_KEY_ENABLED) -- a copy without explicit
    # ServerSideEncryption/SSEKMSKeyId args can silently produce an
    # unencrypted or default-key copy, which would be a real encryption-at-
    # rest regression hiding inside a "just a copy" operation.
    SNAPSHOT_STORAGE_S3_KMS_KEY_ID: str = ""
    SNAPSHOT_STORAGE_S3_BUCKET_KEY_ENABLED: bool = True
    SNAPSHOT_STORAGE_AZURE_ACCOUNT_URL: str = ""
    SNAPSHOT_STORAGE_AZURE_CONNECTION_STRING: str = ""
    SNAPSHOT_STORAGE_AZURE_CONTAINER: str = "boxkite-sandbox"

    # ── Secrets broker (docs/SECRETS-DESIGN.md) ─────────────────────────
    # KMS backend for envelope-encrypting Secret.ciphertext at rest.
    # "local" (default) uses SECRETS_LOCAL_DEV_KMS_KEY, a clearly-marked
    # dev-only symmetric wrapping key (see secrets_kms.py's
    # LocalDevSecretsKmsClient docstring) -- NOT a real KMS, and never
    # appropriate for a production deployment holding real credentials.
    # "aws" uses a real AWS KMS key (SECRETS_KMS_KEY_ID) via
    # AwsKmsSecretsClient; "azure" (AzureKeyVaultSecretsKmsClient) and "gcp"
    # (GcpCloudKmsSecretsKmsClient) reuse the same SECRETS_KMS_KEY_ID setting
    # for their own cloud's key identifier (a Key Vault key URL / a Cloud KMS
    # CryptoKey resource name, respectively) rather than a per-cloud
    # setting name. Deliberately a SEPARATE key from
    # SNAPSHOT_STORAGE_S3_KMS_KEY_ID above -- different blast radius,
    # different IAM policy, no reason to couple them.
    SECRETS_KMS_BACKEND: str = "local"
    SECRETS_KMS_KEY_ID: str = ""
    SECRETS_KMS_AWS_REGION: str = "us-east-1"
    # Base64-encoded 32-byte (AES-256) local wrapping key. Left empty by
    # default for zero-config local dev (an ephemeral, process-local key is
    # generated instead -- see LocalDevSecretsKmsClient) -- set this for
    # anything beyond a single local session so secrets survive a restart.
    SECRETS_LOCAL_DEV_KMS_KEY: str = ""
    # Escape hatch: permit the non-production "local" secrets-KMS backend to
    # run with a production-like ENVIRONMENT. Off by default so a real
    # deployment that forgot to configure a real KMS fails fast at startup
    # (see main.py's startup check) rather than silently protecting
    # Secret.ciphertext with a local dev key -- which, with an unset
    # SECRETS_LOCAL_DEV_KMS_KEY, is ephemeral and not decryptable after a
    # restart (see secrets_kms.LocalDevSecretsKmsClient).
    BOXKITE_ALLOW_INSECURE_LOCAL_KMS: bool = False
    # TTL for the per-session secret-capability token minted at session
    # create time (secret_capability.py) -- long enough to cover a whole
    # session's lifetime (bounded anyway by BOXKITE_MAX_SESSION_MINUTES),
    # short enough that a leaked token doesn't stay usable indefinitely.
    SECRETS_CAPABILITY_TOKEN_TTL_SECONDS: int = 30 * 60
    # Per-account cap on the number of non-deleted secrets -- mirrors
    # BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT's rationale (unbounded rows per
    # account is a quota gap, not just an API nicety).
    BOXKITE_MAX_SECRETS_PER_ACCOUNT: int = 50

    # ── Human takeover (docs/SANDBOX-OBSERVABILITY-DESIGN.md) ───────────
    # TTL for the short-lived, single-use token
    # `POST /v1/sandboxes/{id}/takeover-token` mints just before a browser
    # client (dashboard, JS SDK) opens `WS .../takeover` -- replaces putting
    # the long-lived API key itself on the WebSocket URL as `?api_key=...`.
    # Deliberately short: this token only needs to survive the moment
    # between minting it and completing the WS upgrade, not a whole
    # takeover session's lifetime (unlike SECRETS_CAPABILITY_TOKEN_TTL_SECONDS
    # above, which does need to cover a full session).
    BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS: int = 30
    # TTL for the short-lived, single-use token
    # `POST /v1/account/sandbox-create-token` mints for a JWT-authenticated
    # dashboard session just before it calls `POST /v1/sandboxes` (GitHub
    # issue #221) -- same short-lived reasoning as
    # BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS above (only needs to survive the
    # moment between minting and the create-sandbox call, not a whole
    # session's lifetime), just a slightly longer window since it's a plain
    # POST rather than a WS upgrade racing a browser's own connect timeout.
    BOXKITE_SANDBOX_CREATE_TOKEN_TTL_SECONDS: int = 90
    # Full-duplex PTY session recording (GitHub issue #133, pty_recording.py)
    # -- off by default, same "new capability stays opt-in" convention as
    # BOXKITE_IMAGE_BUILDER_ENABLED/BOXKITE_AGENT_PTY_ENABLED: this durably
    # persists an asciicast-format replay of everything printed AND typed
    # during a takeover session (redacted on a best-effort, shape-based
    # basis -- see pty_recording.py's module docstring for exactly what
    # that does and doesn't catch) to object storage, a meaningfully bigger
    # data-retention footprint than the existing periodic typed-input-only
    # snapshot, so a deployment must opt in explicitly rather than have it
    # silently start writing new sensitive-content blobs to its configured
    # SNAPSHOT_STORAGE_* bucket.
    BOXKITE_TAKEOVER_RECORDING_ENABLED: bool = False

    # ── GUI/remote-desktop takeover (GitHub issue #184,
    # docs/GUI-COMPUTER-USE-SCOPING.md) ──────────────────────────────────
    # TTL for the short-lived, single-use token
    # `POST /v1/sandboxes/{id}/desktop-token` mints, same reasoning as
    # BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS above (this token only needs to
    # survive the moment between minting and completing the `WS .../desktop`
    # upgrade).
    BOXKITE_DESKTOP_TOKEN_TTL_SECONDS: int = 30
    # The control-plane-side kill switch for `WS .../desktop` and
    # `POST .../desktop-token` -- independent of the sidecar's own
    # BOXKITE_DESKTOP_ENABLED env var baked into pods. Both routes 404/close
    # when this is unset, so an operator can disable the feature at the API
    # layer even if some pods happen to have the sidecar flag on. Off by
    # default, same "new capability stays opt-in" convention as
    # BOXKITE_TAKEOVER_RECORDING_ENABLED/BOXKITE_IMAGE_BUILDER_ENABLED.
    BOXKITE_DESKTOP_ENABLED: bool = False
    # The control-plane-side kill switch for the /lsp/* routes -- independent
    # of the sidecar's own BOXKITE_LSP_ENABLED env var baked into pods. Routes
    # 404 when this is unset, so an operator can disable the feature at the
    # API layer even if some pods happen to have the sidecar flag on. Off by
    # default, same "new capability stays opt-in" convention as
    # BOXKITE_DESKTOP_ENABLED/BOXKITE_TAKEOVER_RECORDING_ENABLED.
    BOXKITE_LSP_ENABLED: bool = False
    # The control plane's own externally-reachable base URL, e.g.
    # "https://api.example.com" -- handed to the sidecar (via /configure,
    # never baked into a pod image) so it knows where to call
    # POST /internal/secrets/resolve when a session was granted any
    # secrets. Empty by default: a session requesting secret_names when
    # this is unset fails fast with a clear 500 rather than silently
    # minting a capability token the sidecar has nowhere to redeem.
    SECRETS_CONTROL_PLANE_URL: str = ""
    # NOTE: the control-plane-proxied path (POST /v1/sandboxes/{id}/http-request)
    # is covered by the existing sandbox_ops rate-limit bucket
    # (_enforce_sandbox_rate_limit) -- see routers/sandboxes.py. The sidecar's
    # own POST /http-request has no rate limit of its own, matching the
    # existing /exec posture: a direct SandboxManager.http_request() embedding
    # (bypassing the control-plane proxy) is unrate-limited here, same as
    # direct exec() calls are today. See SECRETS-DESIGN.md §5.

    # ── Declarative builder (docs/DECLARATIVE-BUILDER-DESIGN.md) ─────────
    # This is an explicitly opt-in, strictly isolated path -- see
    # image_builder.py's module docstring for the security boundary. It
    # does NOT change any default sandbox's security posture: session pods
    # never gain a package manager back, and building runs in a separate,
    # one-shot builder job/isolation boundary, never inside a live session
    # pod. Off by default (BOXKITE_IMAGE_BUILDER_ENABLED=false) so a
    # deployment must explicitly turn this feature on.
    BOXKITE_IMAGE_BUILDER_ENABLED: bool = False
    # Per-account cap on non-deleted custom images -- image builds are new
    # infrastructure cost (compute for the build, storage for N images,
    # scanning throughput), same rationale as
    # BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT.
    BOXKITE_MAX_IMAGES_PER_ACCOUNT: int = 10
    # A build request whose (base, packages) spec matches an already-
    # `completed` image for the SAME account, built within this many hours,
    # reuses that image's digest instead of re-running the build -- the
    # design doc's "24h cache" requirement.
    BOXKITE_IMAGE_BUILD_CACHE_HOURS: float = 24.0
    # Image builds are the heaviest operation this service exposes (an
    # actual container build plus a vulnerability scan) -- its own,
    # deliberately conservative rate-limit bucket, per the design doc's
    # security section ("must be its own rate-limit bucket, sized far more
    # conservatively than sandbox_ops").
    BOXKITE_IMAGE_BUILD_RATE_LIMIT_PER_MINUTE: int = 3
    # Cluster-wide ceiling on simultaneously in-flight builds (status
    # "queued" or "building") across ALL accounts combined -- mirrors
    # BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES's rationale: enough accounts
    # each near their own per-account cap (BOXKITE_MAX_IMAGES_PER_ACCOUNT)
    # could otherwise still collectively spawn an unbounded number of
    # simultaneous Kaniko build Jobs. Enforced in routers/images.py before
    # a new build is dispatched.
    BOXKITE_GLOBAL_MAX_CONCURRENT_IMAGE_BUILDS: int = 5
    # Resource requests/limits applied to the Kaniko builder container
    # (image_builder.py's build_job_spec, mirrored in
    # deploy/image-builder-job.yaml) -- an actual container build run
    # against caller-supplied, untrusted package names/versions is real
    # compute that needs the same request/limit discipline
    # resource_config.py already applies to every sandbox/sidecar container.
    BOXKITE_IMAGE_BUILD_CPU_REQUEST: str = "500m"
    BOXKITE_IMAGE_BUILD_CPU_LIMIT: str = "2"
    BOXKITE_IMAGE_BUILD_MEMORY_REQUEST: str = "1Gi"
    BOXKITE_IMAGE_BUILD_MEMORY_LIMIT: str = "4Gi"
    # Wall-clock cap on the build Job's pod (Kubernetes activeDeadlineSeconds)
    # -- a pathological package's build-time hook (e.g. a malicious sdist's
    # setup.py) has no other bound on how long it can run once resource
    # limits alone don't stop a CPU-bound or I/O-bound (not memory-bound)
    # hang.
    BOXKITE_IMAGE_BUILD_TIMEOUT_SECONDS: int = 1200
    # Registry path prefix images are pushed under, namespaced per-account
    # as `{prefix}/{account_id}/{image_id}` -- see SandboxImage.registry_ref
    # and image_builder.py's KanikoJobBuildRunner.
    BOXKITE_IMAGE_REGISTRY_PREFIX: str = "registry.internal/boxkite-images"
    # Namespace the builder Job/ConfigMap are created in. Same env var and
    # same default as `src/boxkite/manager.py`'s SANDBOX_NAMESPACE
    # (`os.environ.get("SANDBOX_NAMESPACE", "default")`) so a self-hoster
    # sets one value and both the session pods and the builder Jobs land
    # in the same namespace -- deploy/rbac.yaml's `sandbox-manager-role`
    # (the control plane's own ServiceAccount, not the builder Job's --
    # see deploy/image-builder-rbac.yaml) is namespace-scoped, so the
    # builder Job must live wherever that Role is bound.
    SANDBOX_NAMESPACE: str = "default"
    # Ceiling on how long KanikoJobBuildRunner.run_build waits for a build
    # Job to reach succeeded/failed before giving up and reporting the
    # build itself as failed (and deleting the Job) -- a build that hangs
    # (e.g. a wedged package-registry connection inside the isolated
    # builder egress) must not tie up the background dispatch task
    # (image_builder.dispatch_build) forever. 900s (15 min) is a generous
    # ceiling for a Kaniko build+push of the small, pinned package sets
    # this feature allows; tune per your registry's actual latency.
    BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS: float = 900.0
    # How often run_build polls the build Job's status while waiting for
    # it to finish. Deliberately not sub-second -- this is a background
    # dispatch task, not a request/response path a caller is blocked on,
    # so there's no latency pressure to poll aggressively and add load to
    # the K8s API server.
    BOXKITE_IMAGE_BUILD_POLL_INTERVAL_SECONDS: float = 3.0
    # CVE severities that block a build from reaching `status: completed`
    # (see image_builder.py's scan-gate policy). A scan that finds any of
    # these severities present rejects the build (`status: rejected`)
    # rather than silently promoting it.
    BOXKITE_IMAGE_SCAN_BLOCK_SEVERITIES_RAW: str = "critical,high"

    @property
    def BOXKITE_IMAGE_SCAN_BLOCK_SEVERITIES(self) -> list[str]:
        return [s.strip().lower() for s in self.BOXKITE_IMAGE_SCAN_BLOCK_SEVERITIES_RAW.split(",") if s.strip()]

    # Whether `KanikoJobBuildRunner` fails a build CLOSED when the
    # vulnerability scanner (Trivy) itself couldn't run -- binary missing,
    # timeout, or output that couldn't be parsed -- as opposed to failing
    # OPEN (logging a loud warning and letting the build reach `completed`
    # with an unscanned `scan_result`). Defaults to `True` (fail closed):
    # this whole feature's threat model (docs/DECLARATIVE-BUILDER-DESIGN.md,
    # SECURITY.md's declarative-builder section) already treats "no real
    # scan ran" as the single concrete gap blocking production use, so a
    # scanner that silently can't run is exactly the same class of problem
    # `scan_result: dict = {}` used to be -- this setting exists so a
    # self-hoster who has deliberately decided to accept that risk (e.g.
    # rolling out Trivy separately, or in an environment where it's known to
    # be unavailable) can opt out explicitly, rather than that decision
    # being made implicitly by an exception being swallowed.
    BOXKITE_IMAGE_SCAN_REQUIRED: bool = True
    # Wall-clock cap on one `trivy image` invocation -- mirrors
    # BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS's rationale: a scan that hangs
    # (e.g. a wedged vulnerability-DB download) must not tie up the
    # background dispatch task forever.
    BOXKITE_IMAGE_SCAN_TIMEOUT_SECONDS: float = 300.0

    # Digest-pinned image each pre-approved `base` enum value (schemas.py's
    # SandboxImageBuildRequest.base) resolves to -- the FROM line the
    # declarative builder layers python_packages/apt_packages on top of
    # (image_builder.py's render_dockerfile). Each entry here is itself an
    # operator-reviewed, centrally-baked image (mirroring SANDBOX_IMAGE's own
    # env-var-configurable-but-operator-controlled pattern in
    # src/boxkite/manager.py) -- this is still "N reviewed bases," never a
    # caller-supplied `FROM arbitrary-registry/whatever`. Format:
    # "name=ref,name=ref". Keep in sync with schemas.py's `base` Literal --
    # every literal value must have an entry here or a build request against
    # it fails fast rather than resolving to nothing.
    BOXKITE_BASE_IMAGE_REFS_RAW: str = (
        "boxkite-default=ghcr.io/evalssment/boxkite-sandbox:latest,"
        "boxkite-minimal=ghcr.io/evalssment/boxkite-sandbox-minimal:latest,"
        "boxkite-node=ghcr.io/evalssment/boxkite-sandbox-node:latest,"
        "boxkite-go=ghcr.io/evalssment/boxkite-sandbox-go:latest,"
        "boxkite-nextjs=ghcr.io/evalssment/boxkite-sandbox-nextjs:latest,"
        "boxkite-rust=ghcr.io/evalssment/boxkite-sandbox-rust:latest"
    )

    @property
    def BOXKITE_BASE_IMAGE_REFS(self) -> dict[str, str]:
        refs: dict[str, str] = {}
        for entry in self.BOXKITE_BASE_IMAGE_REFS_RAW.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, ref = entry.partition("=")
            refs[name.strip()] = ref.strip()
        return refs

    # ── Curated outbound-MCP catalog (GitHub issues #116/#117,
    # docs/OUTBOUND-MCP-DESIGN.md §4) ───────────────────────────────────
    # Mirrors BOXKITE_BASE_IMAGE_REFS_RAW's exact pattern immediately
    # above, applied to hostnames instead of image digests: a small,
    # boxkite-maintained allowlist a POST /v1/mcp-connections request's
    # `catalog_id` must resolve against -- never a caller-supplied
    # hostname. Format: "name=host,name=host". Keep in sync with
    # schemas.py's McpCatalogId Literal -- every literal value must have
    # an entry here or connection creation fails fast (see
    # mcp_catalog.UnknownMcpCatalogEntryError) rather than resolving to
    # nothing, the same drift guard image_builder.py:132-136 already
    # enforces for BOXKITE_BASE_IMAGE_REFS/schemas.py's `base` Literal.
    #
    # NOTE: this pass only wires a resolved catalog host into a session's
    # per-pod NetworkPolicy egress allowlist (issue #74's existing
    # mechanism, unioned with secret_grants) -- there is no MCP-proxy
    # transport yet (docs/OUTBOUND-MCP-DESIGN.md §6), so granting a
    # connection does not yet let an agent actually speak MCP to it.
    BOXKITE_MCP_CATALOG_RAW: str = (
        "slack=mcp.slack.com,"
        "notion=mcp.notion.com,"
        "linear=mcp.linear.app,"
        "github=api.githubcopilot.com"
    )

    @property
    def BOXKITE_MCP_CATALOG(self) -> dict[str, str]:
        catalog: dict[str, str] = {}
        for entry in self.BOXKITE_MCP_CATALOG_RAW.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, host = entry.partition("=")
            catalog[name.strip()] = host.strip()
        return catalog

    # Per-account cap on the number of non-deleted MCP connection grants --
    # mirrors BOXKITE_MAX_SECRETS_PER_ACCOUNT's rationale (unbounded rows
    # per account is a quota gap, not just an API nicety).
    BOXKITE_MAX_MCP_CONNECTIONS_PER_ACCOUNT: int = 50

    # ── Independent Storage Volumes (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's
    # Volume addendum) ────────────────────────────────────────────────────
    # Off by default, same rationale as BOXKITE_IMAGE_BUILDER_ENABLED: a
    # genuinely new capability (dynamic PVC provisioning per account), not
    # a variant of anything already reviewed. volume_builder.py's
    # K8sVolumeProvisioner.provision/deprovision are now implemented
    # against a real CoreV1Api (create-and-poll-for-Bound, delete), but
    # have never been exercised against a LIVE cluster in this repo (there
    # is no live Kubernetes API in CI here) -- only unit-tested with a
    # mocked CoreV1Api. Security-review the Volumes feature end to end
    # before flipping this to default-on against real multi-tenant traffic
    # (see docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's status header).
    BOXKITE_VOLUMES_ENABLED: bool = False
    # Per-account cap on non-deleted volumes -- each is real, billable (in
    # the fair-use sense) cluster storage, same rationale as
    # BOXKITE_MAX_IMAGES_PER_ACCOUNT.
    BOXKITE_MAX_VOLUMES_PER_ACCOUNT: int = 10
    # StorageClass a volume's PersistentVolumeClaim requests -- operator-
    # configurable since the right default varies by cluster (EBS gp3 on
    # EKS, pd-ssd on GKE, etc.), never a caller-supplied value.
    BOXKITE_VOLUME_STORAGE_CLASS: str = "standard"
    # Wall-clock cap on how long K8sVolumeProvisioner.provision polls
    # waiting for a PVC to reach phase=Bound before giving up and reporting
    # the volume "failed" -- a StorageClass with no available capacity (or
    # a misconfigured one) would otherwise poll forever. Deliberately
    # longer than a pod-ready timeout: dynamic volume provisioning
    # (especially cross-AZ EBS/PD) can legitimately take longer than a pod
    # scheduling+startup does.
    BOXKITE_VOLUME_PROVISION_TIMEOUT_SECONDS: int = 120

    # ── Background enforcement ───────────────────────────────────────────
    # How often the reaper task scans for sessions that have exceeded
    # BOXKITE_MAX_SESSION_MINUTES and tears them down server-side.
    BOXKITE_SESSION_REAPER_INTERVAL_SECONDS: int = 60

    # ── Admin role (docs/ADMIN-ROLE-DESIGN.md) ──────────────────────────
    # Hard cap on the per-account breakdown rows GET /v1/admin/metrics
    # returns, independent of how many accounts actually exist -- an
    # aggregation endpoint should never become an unbounded full-table
    # dump as the account base grows. Callers needing more paginate via
    # the same `limit`/`offset` the route accepts.
    BOXKITE_ADMIN_METRICS_MAX_ACCOUNTS: int = 500
    # Same rationale, applied to GET /v1/admin/audit-log's page size
    # (closing GitHub issue #140) -- exec_log_entries grows far faster than
    # the accounts table, so this cap matters even more here.
    BOXKITE_ADMIN_AUDIT_LOG_MAX_LIMIT: int = 500

    # ── Webhooks (docs/WEBHOOKS-DESIGN.md) ──────────────────────────────
    # Deliberately NO global on/off flag (unlike BOXKITE_IMAGE_BUILDER_ENABLED/
    # BOXKITE_VOLUMES_ENABLED above) -- this feature is per-account opt-in by
    # construction: an event is only ever enqueued for delivery if the firing
    # account has itself registered an active webhook subscription for that
    # event type (see webhooks.py's enqueue_event). An account that has
    # never called POST /v1/webhooks incurs zero behavior change and zero
    # outbound HTTP calls, so there is nothing for a global flag to gate.
    # Per-account cap on registered subscriptions -- same rationale as
    # BOXKITE_MAX_SECRETS_PER_ACCOUNT (unbounded rows per account is a quota
    # gap, not just an API nicety).
    BOXKITE_MAX_WEBHOOKS_PER_ACCOUNT: int = 20
    # HTTP timeout for a single delivery attempt against a caller-registered,
    # untrusted external URL -- deliberately its own client/timeout, never
    # shared with the manager<->sidecar httpx client (see webhook_delivery.py).
    BOXKITE_WEBHOOK_DELIVERY_TIMEOUT_SECONDS: int = 10
    # A delivery not yet successful after this many attempts (initial +
    # retries) is marked "failed" permanently -- no dead-letter queue or
    # manual replay mechanism in this first cut.
    BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS: int = 6
    # Exponential backoff base: attempt N waits
    # min(BASE * 2**(N-1), BOXKITE_WEBHOOK_RETRY_MAX_SECONDS) after the
    # previous attempt fails. Default schedule: 30s, 60s, 120s, 240s, 480s.
    BOXKITE_WEBHOOK_RETRY_BASE_SECONDS: int = 30
    BOXKITE_WEBHOOK_RETRY_MAX_SECONDS: int = 3600
    # How often the delivery worker polls for due WebhookDelivery rows --
    # mirrors BOXKITE_SESSION_REAPER_INTERVAL_SECONDS's "simple poll loop,
    # not a new pub/sub system" posture.
    BOXKITE_WEBHOOK_WORKER_INTERVAL_SECONDS: int = 5
    # Cap on how many due deliveries one worker pass processes -- an
    # aggregation-style poll loop should never become an unbounded
    # full-table scan as delivery volume grows, same rationale as
    # BOXKITE_ADMIN_METRICS_MAX_ACCOUNTS.
    BOXKITE_WEBHOOK_WORKER_BATCH_LIMIT: int = 100
    # Rate-limit bucket for POST /v1/webhooks specifically -- registering a
    # webhook is infrequent, low-volume caller activity, so this is
    # deliberately much lower than sandbox_ops, mirroring the image-build/
    # snapshot buckets' "own, more conservative bucket" rationale.
    BOXKITE_WEBHOOK_RATE_LIMIT_PER_MINUTE: int = 10

    # ── Public demo playground (issue #103) ──────────────────────────────
    # Off by default, same "new public-facing surface stays opt-in"
    # convention as BOXKITE_IMAGE_BUILDER_ENABLED/BOXKITE_VOLUMES_ENABLED --
    # this is the ONLY unauthenticated route in this API that triggers real
    # sandbox creation, so a self-hosted operator must explicitly opt in;
    # the hosted deployment sets this true via env var. See
    # routers/demo_playground.py's module docstring for the full design.
    BOXKITE_DEMO_PLAYGROUND_ENABLED: bool = False
    # A small, SEPARATE global concurrency ceiling for demo sessions only --
    # enforced against the well-known internal demo Account's own active-
    # session count (demo_account.py), never against
    # BOXKITE_MAX_CONCURRENT_SANDBOXES/BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES
    # directly. Deliberately small: demo traffic must never be able to eat
    # into the headroom real (signed-up) accounts rely on for the global
    # cap, and unlike a real account, every anonymous visitor shares this
    # one ceiling.
    BOXKITE_DEMO_MAX_CONCURRENT: int = 3
    # Hard wall-clock cap on a demo sandbox's lifetime, in both directions:
    # passed as SandboxManager.create_session's lifetime_minutes (which
    # becomes the pod's own K8s activeDeadlineSeconds -- a real,
    # kubelet-enforced kill, not just bookkeeping) AND used as the reaper's
    # cutoff for demo-account sessions specifically (see reaper.py), so a
    # visitor who closes the tab without the frontend's best-effort DELETE
    # ever firing still has their bookkeeping row (and the capacity slot it
    # holds) freed quickly -- not stuck at the much longer
    # BOXKITE_MAX_SESSION_MINUTES every real account gets.
    BOXKITE_DEMO_LIFETIME_MINUTES: int = 4
    # Per-source-IP sliding-window cap on demo playground requests --
    # unlike every other rate-limit bucket in this file, this one protects
    # an UNAUTHENTICATED route, so it's keyed by IP (enforce_rate_limit's
    # default when no `subject` is given), same as BOXKITE_AUTH_RATE_LIMIT_
    # PER_MINUTE's signup/login bucket.
    BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE: int = 3
    # Ceiling passed as SandboxManager.execute's `timeout` for every demo
    # /exec call -- deliberately short and non-negotiable (the caller's own
    # requested timeout, if any, is ignored) since this is public,
    # unauthenticated compute.
    BOXKITE_DEMO_EXEC_TIMEOUT_SECONDS: int = 10

    # ── Networking ───────────────────────────────────────────────────────
    CONTROL_PLANE_PORT: int = 8090

    # ── CORS ─────────────────────────────────────────────────────────────
    # Comma-separated origins allowed to read this API's responses from
    # browser JS (e.g. a signup/dashboard site). Auth here is Bearer-token
    # only, never cookies, so this isn't a CSRF control -- it's a
    # response-confidentiality one: an unscoped "*" would let ANY origin
    # read responses for a caller that presents a valid key, including one
    # that got hold of a key through some channel other than this API
    # (e.g. copy-pasted into another tool's browser JS). Empty by default
    # so a fresh deployment has no browser client until explicitly
    # configured -- server-side/SDK callers are never subject to CORS at
    # all, so this only affects browser-based clients.
    CORS_ALLOWED_ORIGINS_RAW: str = ""

    @property
    def CORS_ALLOWED_ORIGINS(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS_RAW.split(",") if o.strip()]

    @property
    def is_dev_environment(self) -> bool:
        return self.ENVIRONMENT.lower() in {"development", "dev", "test", "testing"}

    @property
    def api_docs_enabled(self) -> bool:
        if self.ENABLE_API_DOCS is not None:
            return self.ENABLE_API_DOCS
        return self.is_dev_environment


settings = Settings()
