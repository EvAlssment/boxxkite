"""Pydantic request/response models. Docstrings and field descriptions here
flow straight into the generated OpenAPI/Swagger/ReDoc docs (see main.py),
so they're written for that audience, not just as internal type hints.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from boxkite.manager import REQUEST_TIMEOUT as _MANAGER_SIDECAR_REQUEST_TIMEOUT

# SandboxManager's HTTP client to the sidecar (REQUEST_TIMEOUT) applies to
# every call, including /exec. An exec `timeout` above that ceiling would
# httpx.ReadTimeout on the manager side before the sidecar's own timeout
# ever fires, leaking an orphaned sidecar-side process (see
# _is_retryable_sidecar_error, which doesn't treat ReadTimeout as
# retryable). Cap well below it rather than growing per-call timeouts,
# since the manager's HTTP client is shared/pooled per pod, not
# constructed per request.
SANDBOX_EXEC_MAX_TIMEOUT_SECONDS = _MANAGER_SIDECAR_REQUEST_TIMEOUT - 20

# Ceiling on file-write payload sizes (file-create content, str-replace
# old_str/new_str). Kept in sync with the sidecar's own FileCreateRequest/
# StrReplaceRequest field limits (sidecar/main.py) so a request isn't
# accepted at this layer only to be rejected once it reaches the sidecar.
SANDBOX_FILE_CONTENT_MAX_LENGTH = 10 * 1024 * 1024  # 10MB of characters

# Kept in sync with the sidecar's own clamp on grep's max_matches
# (`max(1, min(req.max_matches, 5000))` in sidecar/main.py) so a request
# isn't accepted at this layer only to be silently re-clamped at the
# sidecar.
SANDBOX_GREP_MAX_MATCHES_CEILING = 5000


# ── Auth ─────────────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200, description="Minimum 8 characters.")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    created_at: datetime
    email_verified_at: datetime | None = Field(
        default=None,
        description=(
            "Set once the account has confirmed its email via POST /v1/auth/verify-email. "
            "NULL for every account created before email verification existed, and for any "
            "account created while BOXKITE_EMAIL_VERIFICATION_ENABLED is false. Informational "
            "only today -- no route currently requires this to be set."
        ),
    )
    github_id: str | None = Field(default=None, description="Set once a GitHub identity is linked, via /v1/auth/github or POST /v1/account/link/github/start.")
    google_id: str | None = Field(default=None, description="Set once a Google identity is linked, via /v1/auth/google or POST /v1/account/link/google/start.")


class AccountLinkStartResponse(BaseModel):
    link_token: str = Field(
        description=(
            "Short-lived, single-purpose token proving the current dashboard session asked to "
            "link this provider. Pass as ?link_token= on a top-level navigation to "
            "GET /v1/auth/{provider}/start -- a redirect can't carry this endpoint's own "
            "Authorization header, so the linking intent has to be proven a different way."
        )
    )


class AllowedCommandRule(BaseModel):
    """One constrained allowlist entry -- see boxkite.command_whitelist's
    module docstring for the full semantics (args_allow/args_deny are
    Python regexes matched against the joined argument string)."""

    command: str = Field(min_length=1, max_length=200)
    args_allow: list[str] = Field(default_factory=list)
    args_deny: list[str] = Field(default_factory=list)


class AllowedCommandsRequest(BaseModel):
    """Body for PUT /v1/account/allowed-commands.

    Each entry is either a plain command-name string (unconstrained) or an
    AllowedCommandRule (command name plus optional arg regexes) -- the same
    format boxkite.command_whitelist.validate_command_whitelist already
    accepts. An empty list clears the account back to unrestricted -- use
    DELETE for that instead, this is rejected to avoid an easy-to-miss no-op
    PUT (see routers/account.py).
    """

    rules: list[str | AllowedCommandRule] = Field(min_length=1)


class AllowedCommandsResponse(BaseModel):
    rules: list[str | AllowedCommandRule] = Field(default_factory=list)


class TokenResponse(BaseModel):
    """A short-lived JWT for the dashboard UI.

    This token authenticates as a *user*, not an API key — it is never
    accepted by the /v1/sandboxes routes, which require an API key (see
    deps.py). `refresh_token` is only populated when
    `BOXKITE_REFRESH_TOKENS_ENABLED` is true (see routers/auth.py) --
    otherwise it's null and the caller must re-authenticate via
    /v1/auth/login once `access_token` expires, same as before issue #79.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until access_token expires.")
    refresh_token: str | None = Field(
        default=None,
        description=(
            "Opt-in, long-lived rotating credential -- exchange it via POST /v1/auth/refresh "
            "for a new access_token + refresh_token pair. Null unless "
            "BOXKITE_REFRESH_TOKENS_ENABLED is true."
        ),
    )
    account: AccountOut


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class PasswordResetRequestRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=200, description="Minimum 8 characters.")


class EmailVerificationConfirmRequest(BaseModel):
    token: str = Field(min_length=1)


class MessageResponse(BaseModel):
    """Generic ack body for endpoints that must not leak information via
    their response shape (e.g. password-reset request, which always
    returns the same message whether or not the email is registered)."""

    message: str


# ── API keys ─────────────────────────────────────────────────────────────
class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="A label to identify this key later.")
    role: Literal["admin", "member"] = Field(
        default="admin",
        description=(
            "Permission role for this key within the account. 'admin' (the "
            "default -- preserves this project's original behavior) can do "
            "everything an API key can do, including opening "
            "`WS /v1/sandboxes/{id}/takeover`. 'member' can do everything "
            "EXCEPT initiate a takeover session -- see "
            "docs/SANDBOX-OBSERVABILITY-DESIGN.md and SECURITY.md's 'Human "
            "takeover' section. There is no separate account-member/user "
            "concept in this API today; a key's role is the actual unit "
            "this permission is enforced on."
        ),
    )


class ApiKeyOut(BaseModel):
    """A previously created API key. `key` is never included here — only at
    creation time (see ApiKeyCreated)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    prefix: str
    role: str = Field(default="admin", description="See ApiKeyCreateRequest.role.")
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = Field(
        default=None, description="Set on first authenticated use, updated on every use after that."
    )


class ApiKeyCreated(ApiKeyOut):
    """Returned exactly once, at creation time. Store `key` now — it cannot
    be retrieved again; if lost, revoke it and create a new one."""

    key: str


# ── Sandboxes ────────────────────────────────────────────────────────────
class SandboxCreateRequest(BaseModel):
    label: str | None = Field(
        default=None,
        max_length=200,
        description="Optional caller-supplied label for the caller's own reference. Not sent to the sandbox itself.",
    )
    size: Literal["small", "medium", "large"] = Field(
        default="small",
        description=(
            "Sandbox CPU/memory size preset. Capped per-account by "
            "BOXKITE_MAX_SANDBOX_SIZE -- requesting a larger size than the "
            "account is allowed returns a 429."
        ),
    )
    storage_gb: float | None = Field(
        default=None,
        description=(
            "Optional override for the workspace/uploads/outputs/skills volume "
            "size limit (Gi), capped by BOXKITE_MAX_SANDBOX_STORAGE_GB."
        ),
    )
    lifetime_minutes: int | None = Field(
        default=None,
        description=(
            "Optional override for how long the sandbox pod may run before "
            "being torn down, in minutes. Still bounded by the manager's own "
            "max active-deadline ceiling."
        ),
    )
    count: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of sandbox sessions to create in this request.",
    )
    secret_names: list[str] | None = Field(
        default=None,
        description=(
            "Names of this account's secrets (see POST /v1/secrets) this "
            "session should be granted access to via the sidecar's "
            "POST /http-request secrets broker. Every name must already "
            "exist for the caller's account -- a name that doesn't resolve "
            "returns 404 secret_not_found before any sandbox is created. "
            "The raw secret values are never included in this request or "
            "any response; only the session's sidecar (via a short-lived "
            "internal capability token) can resolve them at request time."
        ),
    )
    image_id: str | None = Field(
        default=None,
        description=(
            "Optional id of a custom image built via POST /v1/images "
            "(docs/DECLARATIVE-BUILDER-DESIGN.md). If omitted, behaves "
            "exactly as today (the operator's default SANDBOX_IMAGE). 404s "
            "if image_id isn't owned by the caller's account, or isn't "
            "status 'completed' yet -- never silently falls back to the "
            "default image."
        ),
    )
    mcp_connection_names: list[str] | None = Field(
        default=None,
        description=(
            "Labels of this account's outbound-MCP connections (see "
            "POST /v1/mcp-connections, GitHub issues #116/#117) this "
            "session should be granted network egress to. Every name must "
            "already exist for the caller's account -- a name that doesn't "
            "resolve returns 404 mcp_connection_not_found before any "
            "sandbox is created, same precedent as secret_names. NOTE: "
            "this only widens the session's per-pod NetworkPolicy egress "
            "allowlist (issue #74's mechanism) to the connection's curated "
            "catalog hostname -- there is no MCP-proxy transport yet "
            "(docs/OUTBOUND-MCP-DESIGN.md section 6), so a granted "
            "connection does not yet let an agent actually speak MCP to it."
        ),
    )
    volume_mounts: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional {volume_id: mount_path} mapping of independent "
            "PVC-backed volumes (POST /v1/volumes, "
            "docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum) to "
            "mount into this sandbox. Every volume_id must already exist for "
            "the caller's account and be status 'ready' -- 404s otherwise, "
            "never silently omitted. mount_path must be an absolute path "
            "outside the sandbox's typed roots (/workspace, /mnt/*, /tmp)."
        ),
    )
    gpu_count: int | None = Field(
        default=None,
        description=(
            "Opt-in, experimental (docs/GPU-SUPPORT-SCOPING.md) -- requests "
            "this many GPUs as a Kubernetes extended-resource limit on the "
            "sandbox container. 422s (gpu_support_disabled) unless the "
            "deployment has BOXKITE_GPU_ENABLED set and a GPU-equipped node "
            "pool with a device plugin provisioned; not verified against "
            "real GPU hardware in this codebase. Bounded by "
            "BOXKITE_MAX_GPU_COUNT_PER_SESSION."
        ),
    )


class SandboxConnectInfo(BaseModel):
    """How to reach the sandbox session.

    SandboxManager's pod networking is cluster-internal only (a pod IP, or
    the K8s API proxy in local-kind dev) — it does not expose an externally
    routable endpoint an outside HTTP caller could hit directly, nor would
    handing out a raw pod IP/sidecar token to an external caller be safe (it
    would bypass this service's own account/limit enforcement entirely).
    `pod_name` remains an opaque handle for operators with cluster access.
    External callers instead operate on the session through this control
    plane's own authenticated boundary: `POST /v1/sandboxes/{id}/exec` to run
    commands, and `POST /v1/sandboxes/{id}/files` (+ `/files/view`,
    `/files/str-replace`) for file operations. Both proxy to the same
    sidecar the pod already runs, via `SandboxManager.execute`/`.file_create`/
    `.view`/`.str_replace`.
    """

    pod_name: str | None
    note: str = (
        "This session's sandbox pod is only reachable from inside the cluster directly; "
        "use POST /v1/sandboxes/{id}/exec and /files endpoints to run commands and "
        "operate on files through this API instead."
    )


class SandboxSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="The session_id passed to SandboxManager.create_session.")
    status: Literal["active", "destroyed"]
    label: str | None = None
    created_at: datetime
    destroyed_at: datetime | None = None
    expires_at: datetime = Field(
        description="created_at + BOXKITE_MAX_SESSION_MINUTES; the reaper destroys the session at this point."
    )
    connect: SandboxConnectInfo | None = None


class UsageSummary(BaseModel):
    """Included on sandbox creation responses so callers can see how close
    they are to the monthly fair-use cap without a separate call."""

    monthly_sandbox_hours_used: float
    monthly_sandbox_hours_limit: float
    concurrent_sandboxes: int
    concurrent_sandboxes_limit: int


class UsageRollupGroup(BaseModel):
    """One row in GET /v1/usage/rollup's breakdown. What `key` holds
    depends on the request's `group_by`: a session id, an ISO calendar day
    (`YYYY-MM-DD`), or an operation name -- never a dollar figure, only
    compute time (`duration_ms`) and a count."""

    key: str
    duration_ms: int
    operation_count: int


class UsageRollupResponse(BaseModel):
    """Response for GET /v1/usage/rollup -- a read-only duration/operation-
    count attribution rollup over the authenticated account's own exec-log
    rows (GitHub issue #162), grouped by session, by day, or by operation
    and optionally narrowed to a `start`/`end` window. This reports compute
    time and operation counts only, never a dollar cost or pricing figure --
    see CLAUDE.md's "no billing, pricing, or payment language" rule."""

    group_by: Literal["session", "day", "operation"]
    start: datetime | None
    end: datetime | None
    total_duration_ms: int
    total_operation_count: int
    groups: list[UsageRollupGroup]
    group_count: int = Field(description="Total distinct groups matching the filter, independent of limit/offset.")
    limit: int
    offset: int


class AdminAccountUsage(BaseModel):
    """One account's row in GET /v1/admin/metrics's per-account breakdown
    (docs/ADMIN-ROLE-DESIGN.md) -- deliberately a narrower view than the
    account's own GET /v1/usage: no password/API-key material, just the
    same two numbers /v1/usage already exposes to the account itself,
    now visible cross-account to an admin."""

    account_id: str
    email: str
    concurrent_sandboxes: int
    monthly_sandbox_hours_used: float


class AdminClusterMetrics(BaseModel):
    """GET /v1/admin/metrics response -- cluster-wide aggregation across
    ALL accounts, admin-gated (docs/ADMIN-ROLE-DESIGN.md). Distinct from
    GET /v1/usage, which is scoped to the calling account only; see that
    doc's "Boundary vs. per-account /v1/usage" section for why both exist
    rather than one subsuming the other."""

    total_accounts: int
    global_concurrent_sandboxes: int
    global_concurrent_sandboxes_limit: int
    total_monthly_sandbox_hours_used: float
    accounts: list[AdminAccountUsage]


class SandboxCreatedResponse(SandboxSessionOut):
    usage: UsageSummary


# ── Sandbox operations (exec / file ops) ────────────────────────────────
# These proxy straight through to the session's sidecar via
# `SandboxManager.execute` / `.file_create` / `.view` / `.str_replace` — see
# routers/sandboxes.py. Field names and shapes intentionally mirror the
# sidecar's own request/response models (sidecar/main.py) rather than
# inventing a parallel vocabulary.
class SandboxExecRequest(BaseModel):
    command: str = Field(min_length=1, description="Shell command to run inside the sandbox.")
    timeout: int = Field(
        default=30,
        ge=1,
        le=SANDBOX_EXEC_MAX_TIMEOUT_SECONDS,
        description=f"Command timeout in seconds (1-{SANDBOX_EXEC_MAX_TIMEOUT_SECONDS}).",
    )
    description: str | None = Field(default=None, max_length=500)


class SandboxExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


# ── LSP (GitHub issue #183, docs/LSP-SUPPORT-SCOPING.md) ────────────────
# Proxy straight through to SandboxManager.lsp_start/.lsp_open/
# .lsp_completion/.lsp_stop -- see routers/sandboxes.py.
class SandboxLspStartRequest(BaseModel):
    language: str = Field(min_length=1, max_length=32)


class SandboxLspStartResponse(BaseModel):
    lsp_id: str


class SandboxLspOpenRequest(BaseModel):
    path: str = Field(min_length=1)
    content: str = Field(max_length=SANDBOX_FILE_CONTENT_MAX_LENGTH)


class SandboxLspStatusResponse(BaseModel):
    status: str


class SandboxLspCompletionRequest(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(ge=0)
    character: int = Field(ge=0)


class SandboxLspCompletionResponse(BaseModel):
    items: list[dict]


SANDBOX_HTTP_REQUEST_BODY_MAX_LENGTH = 10 * 1024 * 1024


class SandboxHttpRequestRequest(BaseModel):
    """Body for POST /v1/sandboxes/{id}/http-request -- the secrets-broker
    HTTP request (docs/SECRETS-DESIGN.md §3). Proxies straight through to
    SandboxManager.http_request -> the session's sidecar's own
    `POST /http-request` route, which performs the real
    `{{secret:name}}` substitution, DNS-rebinding-safe allowlist check, and
    response scrubbing -- this control-plane route never sees a resolved
    secret value; it only relays the request/response envelope."""

    method: str = Field(default="GET", max_length=10)
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=SANDBOX_HTTP_REQUEST_BODY_MAX_LENGTH)
    timeout: int = Field(default=15, ge=1, le=60)


class SandboxHttpRequestResponse(BaseModel):
    status_code: int
    headers: dict[str, str]
    body: str
    truncated: bool = False


class SandboxFileCreateRequest(BaseModel):
    path: str = Field(min_length=1, description="File path, relative to /workspace or an absolute writable path.")
    content: str = Field(max_length=SANDBOX_FILE_CONTENT_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=500)


class SandboxFileCreateResponse(BaseModel):
    path: str
    size: int
    created: bool


class SandboxFileViewRequest(BaseModel):
    path: str = Field(min_length=1)
    view_range: list[int] | None = Field(
        default=None, description="Optional [start_line, end_line], 1-indexed."
    )
    description: str | None = Field(default=None, max_length=500)


class SandboxFileViewResponse(BaseModel):
    content: str
    lines: int
    is_directory: bool = False
    entries: list[str] | None = None


class SandboxStrReplaceRequest(BaseModel):
    path: str = Field(min_length=1)
    old_str: str = Field(
        min_length=1,
        max_length=SANDBOX_FILE_CONTENT_MAX_LENGTH,
        description="Exact string to find; must appear exactly once unless replace_all is set.",
    )
    new_str: str = Field(max_length=SANDBOX_FILE_CONTENT_MAX_LENGTH)
    replace_all: bool = False
    description: str | None = Field(default=None, max_length=500)


class SandboxStrReplaceResponse(BaseModel):
    path: str
    replaced: bool
    occurrences: int


class SandboxLsRequest(BaseModel):
    path: str = Field(default="/", min_length=1)


class SandboxLsResponse(BaseModel):
    entries: list[dict]


class SandboxGlobRequest(BaseModel):
    pattern: str = Field(min_length=1, description="Glob pattern, e.g. '**/*.py'.")
    path: str = Field(default="/", min_length=1)


class SandboxGlobResponse(BaseModel):
    matches: list[dict]


class SandboxGrepRequest(BaseModel):
    pattern: str = Field(min_length=1, description="Regex pattern to search file contents for.")
    path: str = Field(default="/", min_length=1)
    glob: str | None = Field(default=None, description="Optional glob to restrict which files are searched.")
    max_matches: int = Field(default=500, ge=1, le=SANDBOX_GREP_MAX_MATCHES_CEILING)


class SandboxGrepResponse(BaseModel):
    matches: list[dict]
    error: str | None = None
    truncated: bool = False


# ── Sandbox background processes/sessions ───────────────────────────────
# Distinct from exec: exec is one-shot request/response, bounded by
# `timeout`. These track a process across multiple calls, proxying to
# SandboxManager.start_process/.get_process_output/.send_process_input/
# .stop_process/.list_processes -- see docs/PROCESS-SESSIONS-DESIGN.md. Field
# names mirror the sidecar's own /process/* models (sidecar/main.py) rather
# than inventing a parallel vocabulary, same convention as the exec/file-op
# schemas above.
#
# A background process is a standing resource commitment (unlike a bounded
# exec call), so max_runtime_seconds is required with a server-enforced
# ceiling here too -- kept in sync with the sidecar's own
# PROCESS_MAX_RUNTIME_SECONDS_CEILING default (sidecar/main.py) so a request
# accepted at this layer isn't silently rejected once it reaches the sidecar.
SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING = 4 * 3600


class SandboxProcessStartRequest(BaseModel):
    command: str = Field(min_length=1, description="Shell command to run in the background.")
    description: str | None = Field(default=None, max_length=500)
    max_runtime_seconds: int = Field(
        ge=1,
        le=SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING,
        description=(
            "Hard ceiling on how long the process may run before being "
            f"force-killed (1-{SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING})."
        ),
    )
    expose_port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description=(
            "Opt-in port to expose via a signed preview URL once this "
            "process is listening -- see docs/NETWORK-INGRESS-DESIGN.md and "
            "POST /v1/sandboxes/{session_id}/preview/{port}. Leave unset for "
            "a normal, fully network-isolated background process."
        ),
    )


class SandboxProcessStartResponse(BaseModel):
    process_id: str
    status: str
    started_at: str


class SandboxProcessOutputResponse(BaseModel):
    status: str
    stdout_chunk: str
    next_offset: int
    truncated: bool
    exit_code: int | None = None


class SandboxProcessInputRequest(BaseModel):
    data: str = Field(min_length=1, max_length=SANDBOX_FILE_CONTENT_MAX_LENGTH)


class SandboxProcessInputResponse(BaseModel):
    bytes_written: int


class SandboxProcessStopResponse(BaseModel):
    status: str
    exit_code: int | None = None


class SandboxProcessInfo(BaseModel):
    process_id: str
    command: str
    description: str | None = None
    status: str
    started_at: str
    exit_code: int | None = None
    expose_port: int | None = None


class SandboxProcessListResponse(BaseModel):
    processes: list[SandboxProcessInfo]


# ── Network ingress preview URLs (docs/NETWORK-INGRESS-DESIGN.md) ──────────
# A signed, time-limited URL that proxies HTTP traffic to a port a
# background process opened inside a running sandbox, without opening the
# pod's own network ingress or bypassing the sidecar's auth model -- see the
# design doc for the full security model.
SANDBOX_PREVIEW_MIN_TTL_SECONDS = 30
SANDBOX_PREVIEW_MAX_TTL_SECONDS = 24 * 3600
SANDBOX_PREVIEW_DEFAULT_TTL_SECONDS = 15 * 60


class SandboxPreviewUrlRequest(BaseModel):
    ttl_seconds: int = Field(
        default=SANDBOX_PREVIEW_DEFAULT_TTL_SECONDS,
        ge=SANDBOX_PREVIEW_MIN_TTL_SECONDS,
        le=SANDBOX_PREVIEW_MAX_TTL_SECONDS,
        description=(
            "How long the minted preview URL stays valid, in seconds "
            f"({SANDBOX_PREVIEW_MIN_TTL_SECONDS}-{SANDBOX_PREVIEW_MAX_TTL_SECONDS})."
        ),
    )


class SandboxPreviewUrlResponse(BaseModel):
    url: str
    expires_at: datetime
    token_id: str = Field(
        description=(
            "Opaque handle for this specific minted token (its JWT `jti` "
            "claim) -- pass this to POST .../preview/{port}/revoke to "
            "invalidate this one token early, without tearing down the "
            "session or affecting any other preview token minted for the "
            "same session/port."
        )
    )


class SandboxPreviewRevokeRequest(BaseModel):
    token_id: str = Field(
        description="The `token_id` returned by the mint call (POST .../preview/{port})."
    )


class SandboxPreviewRevokeResponse(BaseModel):
    revoked: bool
    token_id: str


class SandboxTakeoverTokenRequest(BaseModel):
    """Optional request body for `POST /v1/sandboxes/{id}/takeover-token`.
    Omit entirely (or send `read_only: false`) for the default, unchanged
    full-control token. Set `read_only: true` (GitHub issue #131) to mint a
    token that `WS .../takeover` will accept for streaming server->client
    PTY output, but for which the takeover proxy will refuse to forward any
    client->PTY input -- an observer seat rather than a full takeover."""

    read_only: bool = False


class SandboxTakeoverTokenResponse(BaseModel):
    """Response of `POST /v1/sandboxes/{id}/takeover-token` -- see
    routers/sandboxes.py's `mint_sandbox_takeover_token`. Pass `token` as
    `?token=` on the `WS .../takeover` URL immediately after minting it: it
    is single-use and expires quickly (`expires_at`) even if never
    redeemed."""

    token: str
    expires_at: datetime
    read_only: bool = False


class SandboxDesktopTokenResponse(BaseModel):
    """Response of `POST /v1/sandboxes/{id}/desktop-token` -- see
    routers/sandboxes.py's `mint_sandbox_desktop_token`. Pass `token` as
    `?token=` on the `WS .../desktop` URL immediately after minting it: it
    is single-use and expires quickly (`expires_at`) even if never
    redeemed. No `read_only` field -- see `create_desktop_token`'s
    docstring for why there is no view-only concept for v1."""

    token: str
    expires_at: datetime


class SandboxCreateTokenResponse(BaseModel):
    """Response of `POST /v1/account/sandbox-create-token` -- see
    routers/account.py's `mint_sandbox_create_token`. Pass `token` as the
    `Authorization: Bearer` credential on `POST /v1/sandboxes` immediately
    after minting it: it is single-use and expires quickly (`expires_at`)
    even if never redeemed (GitHub issue #221)."""

    token: str
    expires_at: datetime


# ── Sandbox observability (audit log / live watch) ──────────────────────
# Read-side of `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3 -- the
# write side (`ExecLogEntry`, `_log_exec_entry`) already exists in
# models_orm.py/routers/sandboxes.py. `ExecLogEntryOut` mirrors the ORM
# model's fields 1:1 rather than inventing a parallel vocabulary, same
# convention as the exec/file-op response schemas above.
SANDBOX_LOG_DEFAULT_LIMIT = 50
SANDBOX_LOG_MAX_LIMIT = 500


class ExecLogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    source: Literal["agent", "human_takeover"]
    operation: str
    detail: dict
    exit_code: int | None = None
    output_truncated: str | None = None
    started_at: datetime
    duration_ms: int
    row_hash: str | None = Field(
        default=None,
        description=(
            "Hash-chain digest for this row (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md), "
            "or null for legacy rows written before hash-chaining was added. "
            "Exposed so an external auditor can independently verify an exported "
            "copy of these entries without trusting this service's own "
            "verification of itself -- see boxkite.audit.verify_chain_rows."
        ),
    )
    prev_hash: str | None = Field(
        default=None,
        description="The previous chained row's row_hash this row was computed from, or null alongside row_hash.",
    )


class SandboxLogResponse(BaseModel):
    """Response for `GET /v1/sandboxes/{session_id}/log`. `limit`/`offset`
    echo back the effective pagination window used, so a caller paging
    through results doesn't have to re-derive it from the request."""

    entries: list[ExecLogEntryOut]
    limit: int
    offset: int
    total: int = Field(description="Total matching rows for this session, independent of limit/offset.")


# ── Admin audit-log aggregation (docs/ADMIN-ROLE-DESIGN.md, closing
# GitHub issue #140) ─────────────────────────────────────────────────────
# `GET /v1/admin/audit-log` is the cross-account counterpart to
# `GET /v1/sandboxes/{session_id}/log` above: same `ExecLogEntry` rows,
# admin-gated, aggregated across every session in every account (or just
# one account via `account_id`) instead of one already-authorized session.
ADMIN_AUDIT_LOG_DEFAULT_LIMIT = 100


class AdminAuditLogEntryOut(BaseModel):
    """`ExecLogEntryOut` plus `account_id` -- the one extra field this view
    needs since it spans accounts rather than being scoped to a single
    session a caller already owns."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    session_id: str
    source: Literal["agent", "human_takeover"]
    operation: str
    detail: dict
    exit_code: int | None = None
    output_truncated: str | None = None
    started_at: datetime
    duration_ms: int
    row_hash: str | None = Field(default=None, description=ExecLogEntryOut.model_fields["row_hash"].description)
    prev_hash: str | None = Field(default=None, description=ExecLogEntryOut.model_fields["prev_hash"].description)


class AdminAuditLogResponse(BaseModel):
    """Response for `GET /v1/admin/audit-log`. `limit`/`offset` echo back
    the effective pagination window, same convention as
    `SandboxLogResponse`. Rows are newest-first (unlike `SandboxLogResponse`,
    which is oldest-first) -- an admin scanning fleet-wide activity wants
    the most recent operations first, not a session's own chronological
    replay."""

    entries: list[AdminAuditLogEntryOut]
    limit: int
    offset: int
    total: int = Field(description="Total matching rows for this query, independent of limit/offset.")


# ── Snapshots (filesystem snapshot/restore) ─────────────────────────────
# See docs/SNAPSHOT-DESIGN.md. "Filesystem snapshot," never bare "snapshot,"
# in every user-facing description below -- this is a point-in-time copy of
# the workspace/output filesystem, not a VM-level checkpoint (no in-memory
# state, open file descriptors, or network connections are preserved).
class SnapshotCreateRequest(BaseModel):
    label: str | None = Field(
        default=None,
        max_length=200,
        description="Optional caller-supplied label for the caller's own reference.",
    )


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str | None = Field(
        description="The session this snapshot was taken from. Null if that session has since been destroyed -- a snapshot outlives its source session."
    )
    label: str | None = None
    status: Literal["pending", "completed", "failed"]
    storage_key_prefix: str
    size_bytes: int
    created_at: datetime
    deleted_at: datetime | None = None


class SnapshotCreatedResponse(SnapshotOut):
    pass


# ── Secrets (proxy-substitution secrets broker, docs/SECRETS-DESIGN.md) ──
# `value` is write-only: accepted on create, never returned by any route
# below (list/get/the create response itself all omit it). See §3 of the
# design doc.
BOXKITE_SECRET_NAME_MAX_LENGTH = 200
BOXKITE_SECRET_VALUE_MAX_LENGTH = 65536


class SecretCreateRequest(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=BOXKITE_SECRET_NAME_MAX_LENGTH,
        description="Unique (per-account) name used to reference this secret from secret_names and {{secret:name}}.",
    )
    value: str = Field(
        min_length=1,
        max_length=BOXKITE_SECRET_VALUE_MAX_LENGTH,
        description="The real credential value. Write-only -- never returned by any route.",
    )
    allowed_hosts: list[str] = Field(
        min_length=1,
        description=(
            "Destination hostnames this secret may be used against via "
            "POST /http-request. Required, not optional -- an unscoped "
            "secret usable against any destination defeats the point of "
            "this feature. Hosts that resolve to a private/link-local/"
            "loopback/metadata address are rejected at creation time "
            "(a best-effort backstop -- see docs/SECRETS-DESIGN.md §5 for "
            "why the real control is the sidecar's request-time check)."
        ),
    )
    trust_tier: str | None = Field(
        default=None,
        description=(
            "Only meaningful for wallet/private-key-style secrets "
            "(docs/WALLET-SECRETS-DESIGN.md). Omit for an ordinary "
            "API-key-style secret. The only accepted value today is "
            "'testnet' -- a disposable, faucet-funded key with no "
            "realizable value, consumed via the existing secret_env "
            "mechanism like any other secret. 'mainnet' is refused at "
            "creation (422): it requires the session-scoped signing "
            "mechanism WALLET-SECRETS-DESIGN.md §4b describes, which does "
            "not exist yet -- labeling a key 'mainnet' without that "
            "enforcement would be worse than not offering the label."
        ),
    )


class SecretOut(BaseModel):
    """Metadata only -- the value is never included here or anywhere else
    after creation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    allowed_hosts: list[str]
    trust_tier: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None


class SecretCreatedResponse(SecretOut):
    pass


# ── MCP Connections (outbound-MCP connection grants, GitHub issues
# #116/#117, docs/OUTBOUND-MCP-DESIGN.md §3) ─────────────────────────────
# A connection grant is modeled the same way a Secret is: an org-scoped,
# named row a session can request access to by label (SandboxCreateRequest.
# mcp_connection_names, mirroring secret_names exactly). The destination
# host is never caller-supplied -- catalog_id must resolve against the
# curated allowlist in config.py's BOXKITE_MCP_CATALOG (see mcp_catalog.py).
#
# Scope note: this pass wires the resolved catalog host into the existing
# per-session NetworkPolicy egress allowlist only (issue #74's mechanism,
# unioned with secret_grants) -- there is no MCP-proxy transport and no
# third-party OAuth credential handling yet (both explicitly flagged as
# needing their own follow-on design pass in docs/OUTBOUND-MCP-DESIGN.md
# §6/§7), so this model deliberately has no credential field of any kind.
BOXKITE_MCP_CONNECTION_LABEL_MAX_LENGTH = 200

McpCatalogId = Literal["slack", "notion", "linear", "github"]


class McpConnectionCreateRequest(BaseModel):
    label: str = Field(
        min_length=1,
        max_length=BOXKITE_MCP_CONNECTION_LABEL_MAX_LENGTH,
        description="Unique (per-account) name used to reference this connection from mcp_connection_names.",
    )
    catalog_id: McpCatalogId = Field(
        description=(
            "Which curated MCP catalog entry (GitHub issue #117) this "
            "connection grants network egress to. Restricted to boxkite's "
            "own reviewed allowlist (config.py's BOXKITE_MCP_CATALOG) -- "
            "never a caller-supplied hostname."
        )
    )


class McpConnectionOut(BaseModel):
    """The resolved catalog host is included for the caller's own
    visibility -- it is never treated as caller-supplied input on any
    other route."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    catalog_id: str
    host: str
    created_at: datetime
    last_used_at: datetime | None = None


class McpConnectionCreatedResponse(McpConnectionOut):
    pass


# ── Webhooks (outbound event delivery, docs/WEBHOOKS-DESIGN.md) ─────────
# Distinct from Secrets above: this is push (the control plane calls a URL
# the caller registered), not pull (an agent's own request). See
# docs/WEBHOOKS-DESIGN.md's "vs. AuditSink" section for the full contrast.
BOXKITE_WEBHOOK_DESCRIPTION_MAX_LENGTH = 200
BOXKITE_WEBHOOK_URL_MAX_LENGTH = 2048

# Literal kept in lockstep with webhooks.WEBHOOK_EVENT_TYPES -- see that
# module's module docstring. "audit_log.entry" added per GitHub issue #125
# (SIEM/audit-log export).
WebhookEventType = Literal["sandbox.created", "sandbox.destroyed", "audit_log.entry"]

# Literal kept in lockstep with webhooks.WEBHOOK_PAYLOAD_FORMATS.
# "boxkite_v1" is this API's own envelope (default, unchanged from the
# original design); "splunk_hec" wraps that same envelope in a Splunk HTTP
# Event Collector-shaped body -- see webhooks.build_splunk_hec_payload.
WebhookPayloadFormat = Literal["boxkite_v1", "splunk_hec"]

BOXKITE_WEBHOOK_HEC_TOKEN_MAX_LENGTH = 500


class WebhookCreateRequest(BaseModel):
    url: str = Field(
        min_length=1,
        max_length=BOXKITE_WEBHOOK_URL_MAX_LENGTH,
        description="HTTPS (or HTTP, for local testing) URL the control plane will POST events to.",
    )
    event_types: list[WebhookEventType] = Field(
        min_length=1,
        description="Event types this subscription should receive. At least one is required.",
    )
    description: str | None = Field(
        default=None,
        max_length=BOXKITE_WEBHOOK_DESCRIPTION_MAX_LENGTH,
        description="Optional caller-supplied label for this subscription (e.g. 'Slack notifier').",
    )
    payload_format: WebhookPayloadFormat = Field(
        default="boxkite_v1",
        description=(
            "Body shape for deliveries to this subscription. 'boxkite_v1' "
            "(default) is this API's own JSON envelope. 'splunk_hec' wraps "
            "the same envelope in a Splunk HTTP Event Collector-shaped body "
            "so it can be POSTed directly at a Splunk HEC endpoint -- see "
            "docs/WEBHOOKS-DESIGN.md."
        ),
    )
    hec_token: str | None = Field(
        default=None,
        max_length=BOXKITE_WEBHOOK_HEC_TOKEN_MAX_LENGTH,
        description=(
            "Optional Splunk HEC token for the destination endpoint, sent "
            "as 'Authorization: Splunk <token>' on every delivery when set. "
            "Only meaningful alongside payload_format='splunk_hec'; "
            "envelope-encrypted at rest exactly like the signing secret, "
            "and never returned by any route after this create response "
            "(in fact never echoed even here -- unlike the signing secret, "
            "the caller already knows this value, since they supplied it)."
        ),
    )

    @field_validator("url")
    @classmethod
    def _url_has_http_scheme(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value

    @field_validator("event_types")
    @classmethod
    def _event_types_no_duplicates(cls, value: list[str]) -> list[str]:
        # Order-preserving de-dup rather than a bare set() -- keeps the
        # caller's own ordering intact for a field they'll see echoed back.
        seen: set[str] = set()
        deduped = []
        for event_type in value:
            if event_type not in seen:
                seen.add(event_type)
                deduped.append(event_type)
        return deduped


class WebhookOut(BaseModel):
    """Metadata only -- the signing secret and hec_token are never included
    here or anywhere else after creation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    event_types: list[str]
    description: str | None = None
    is_active: bool
    payload_format: str = "boxkite_v1"
    created_at: datetime
    last_triggered_at: datetime | None = None


class WebhookCreatedResponse(WebhookOut):
    secret: str = Field(
        description=(
            "The raw signing secret, shown exactly once. Use it to verify "
            "the X-Boxkite-Webhook-Signature header on every delivery -- "
            "it cannot be retrieved again after this response; register a "
            "new subscription if it's lost."
        )
    )


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: str
    status: Literal["pending", "delivered", "failed"]
    attempt_count: int
    next_attempt_at: datetime
    last_attempt_at: datetime | None = None
    response_status_code: int | None = None
    failure_reason: str | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class SnapshotRestoreRequest(BaseModel):
    label: str | None = Field(
        default=None,
        max_length=200,
        description="Optional label for the newly-created sandbox session (not the snapshot).",
    )


# ── Declarative builder (custom sandbox images) ─────────────────────────
# See docs/DECLARATIVE-BUILDER-DESIGN.md. Deliberately NOT a Dockerfile-
# passthrough API -- a constrained, pre-approved base plus an exact-version-
# pinned package list, per the design doc's section 3 rationale.
_PINNED_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*==[A-Za-z0-9][A-Za-z0-9._+-]*$")
# Defense in depth against resource amplification: up to 100 packages per
# list (Field(max_length=100) below) times an unbounded per-string length
# would let a caller submit a large payload that gets JSON-encoded, hashed,
# and stored repeatedly (cache_key_for, the DB JSON column). No real package
# name/version needs anywhere close to this; it only bounds worst-case
# request size, not legitimate use.
_MAX_PACKAGE_SPEC_LENGTH = 200


def _validate_pinned_packages(packages: list[str]) -> list[str]:
    for pkg in packages:
        if len(pkg) > _MAX_PACKAGE_SPEC_LENGTH:
            raise ValueError(f"Package spec exceeds max length of {_MAX_PACKAGE_SPEC_LENGTH} characters")
        if not _PINNED_PACKAGE_RE.match(pkg):
            raise ValueError(
                f"Package {pkg!r} must be exact-version pinned as 'name==version' (no ranges, no 'latest')"
            )
    return packages


# npm package names may be scoped ("@anthropic-ai/claude-code") -- the plain
# _PINNED_PACKAGE_RE above has no allowance for the leading "@scope/" segment,
# so npm gets its own pattern rather than silently rejecting every scoped
# package (which covers a large fraction of real npm packages, including the
# one that motivated this field -- see docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md).
_NPM_PINNED_PACKAGE_RE = re.compile(
    r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9._+-]*$"
)


def _validate_npm_pinned_packages(packages: list[str]) -> list[str]:
    for pkg in packages:
        if len(pkg) > _MAX_PACKAGE_SPEC_LENGTH:
            raise ValueError(f"Package spec exceeds max length of {_MAX_PACKAGE_SPEC_LENGTH} characters")
        if not _NPM_PINNED_PACKAGE_RE.match(pkg):
            raise ValueError(
                f"Package {pkg!r} must be exact-version pinned as 'name==version' or "
                "'@scope/name==version' (no ranges, no 'latest')"
            )
    return packages


class SandboxImageBuildRequest(BaseModel):
    label: str | None = Field(default=None, max_length=200, description="Caller-supplied label for reference.")
    base: Literal[
        "boxkite-default", "boxkite-minimal", "boxkite-node", "boxkite-go", "boxkite-nextjs", "boxkite-rust"
    ] = Field(
        default="boxkite-default",
        description=(
            "Pre-approved base image. Each legal value is itself a "
            "separate, boxkite-maintained hardened image (see "
            "deploy/sandbox.Dockerfile, deploy/sandbox-minimal.Dockerfile, "
            "deploy/sandbox-node.Dockerfile, deploy/sandbox-go.Dockerfile, "
            "deploy/sandbox-nextjs.Dockerfile, and deploy/sandbox-rust.Dockerfile) "
            "-- 'boxkite-default' is the full data-science/document/browser "
            "stack, 'boxkite-minimal' is a lean python+node base with none of "
            "that preinstalled, 'boxkite-node' drops Python entirely for "
            "callers whose workload is purely JS/TS (no python_packages are "
            "installable on this base -- only apt_packages/npm_packages), "
            "'boxkite-go' drops both Python and Node entirely for callers "
            "whose workload is purely Go (no python_packages or npm_packages "
            "are installable on this base -- only apt_packages), "
            "'boxkite-nextjs' is 'boxkite-node' plus a pre-installed, "
            "dependency-resolved Next.js (App Router) starter vendored at "
            "/opt/nextjs-template (copy it into /workspace to start from it "
            "-- see deploy/sandbox-nextjs.Dockerfile) -- same Python-free "
            "restriction as 'boxkite-node' (no python_packages), since it is "
            "the same Node-only runtime underneath, and 'boxkite-rust' drops "
            "both Python and Node entirely for callers whose workload is "
            "purely Rust (no python_packages or npm_packages are installable "
            "on this base -- only apt_packages), for a genuinely smaller "
            "footprint than carrying an unused runtime. "
            "NOT a free-form image reference: a caller can never supply an "
            "arbitrary base OS unrelated to boxkite's own hardening work."
        ),
    )
    python_packages: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Exact-version-pinned pip packages, e.g. 'polars==1.9.0'. Ranges/'latest' are rejected with 400.",
    )
    apt_packages: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Exact-version-pinned apt/apk packages, e.g. 'ripgrep==14.1.0-1'. Ranges/'latest' are rejected with 400.",
    )
    npm_packages: list[str] = Field(
        default_factory=list,
        max_length=100,
        description=(
            "Exact-version-pinned npm packages, e.g. '@anthropic-ai/claude-code==2.0.1' or "
            "'typescript==5.6.0'. Ranges/'latest' are rejected with 400. Both bases already "
            "have Node.js; npm itself is reinstalled only for the duration of this build's "
            "one layer and removed again, same as python_packages' transient pip -- see "
            "image_builder.py's render_dockerfile."
        ),
    )

    @field_validator("python_packages", "apt_packages")
    @classmethod
    def _pinned(cls, value: list[str]) -> list[str]:
        return _validate_pinned_packages(value)

    @field_validator("npm_packages")
    @classmethod
    def _npm_pinned(cls, value: list[str]) -> list[str]:
        return _validate_npm_pinned_packages(value)

    @model_validator(mode="after")
    def _no_python_packages_on_node_only_bases(self) -> "SandboxImageBuildRequest":
        # 'boxkite-node' (deploy/sandbox-node.Dockerfile) and 'boxkite-nextjs'
        # (deploy/sandbox-nextjs.Dockerfile -- the same Node-only runtime,
        # plus a pre-installed Next.js starter) have no Python interpreter
        # at all -- `apk add py3.11-pip` would silently pull python-3.11 back
        # in as a transitive apk dependency, quietly defeating the whole
        # point of a Python-free base rather than failing loudly. Reject at
        # the API boundary instead.
        if self.base in ("boxkite-node", "boxkite-nextjs") and self.python_packages:
            raise ValueError(
                f"python_packages is not supported on base={self.base!r} (no Python interpreter "
                "on this base) -- use 'boxkite-minimal' or 'boxkite-default' if you need pip packages"
            )
        return self

    @model_validator(mode="after")
    def _no_python_or_npm_packages_on_go_base(self) -> "SandboxImageBuildRequest":
        # 'boxkite-go' (deploy/sandbox-go.Dockerfile) has neither a Python
        # interpreter nor a Node runtime -- same reasoning as
        # _no_python_packages_on_node_base above, but for both runtimes at
        # once: `apk add py3.11-pip` or `apk add npm` would silently pull
        # python/node back in as transitive apk dependencies, quietly
        # defeating the whole point of a lean Go-only base rather than
        # failing loudly. Reject at the API boundary instead.
        if self.base == "boxkite-go" and self.python_packages:
            raise ValueError(
                "python_packages is not supported on base='boxkite-go' (no Python interpreter "
                "on this base) -- use 'boxkite-minimal' or 'boxkite-default' if you need pip packages"
            )
        if self.base == "boxkite-go" and self.npm_packages:
            raise ValueError(
                "npm_packages is not supported on base='boxkite-go' (no Node runtime on this base) "
                "-- use 'boxkite-minimal' or 'boxkite-node' if you need npm packages"
            )
        return self

    @model_validator(mode="after")
    def _no_python_or_npm_packages_on_rust_base(self) -> "SandboxImageBuildRequest":
        # 'boxkite-rust' (deploy/sandbox-rust.Dockerfile) has neither a
        # Python interpreter nor a Node runtime -- identical reasoning to
        # _no_python_or_npm_packages_on_go_base above, for the same two
        # runtimes: `apk add py3.11-pip` or `apk add npm` would silently
        # pull python/node back in as transitive apk dependencies, quietly
        # defeating the whole point of a lean Rust-only base rather than
        # failing loudly. Reject at the API boundary instead.
        if self.base == "boxkite-rust" and self.python_packages:
            raise ValueError(
                "python_packages is not supported on base='boxkite-rust' (no Python interpreter "
                "on this base) -- use 'boxkite-minimal' or 'boxkite-default' if you need pip packages"
            )
        if self.base == "boxkite-rust" and self.npm_packages:
            raise ValueError(
                "npm_packages is not supported on base='boxkite-rust' (no Node runtime on this base) "
                "-- use 'boxkite-minimal' or 'boxkite-node' if you need npm packages"
            )
        return self


class SandboxImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str | None = None
    base: str
    python_packages: list[str]
    apt_packages: list[str]
    npm_packages: list[str] = []
    status: Literal["queued", "building", "scanning", "completed", "failed", "rejected"]
    digest: str | None = None
    registry_ref: str | None = None
    scan_result: dict | None = None
    failure_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class SandboxImageBuildAccepted(BaseModel):
    id: str
    label: str | None = None
    status: Literal["queued", "building", "scanning", "completed", "failed", "rejected"]
    created_at: datetime


# ── Independent Storage Volumes (PVC-backed) ────────────────────────────
# See docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum -- this is
# E2B's e2b.Volume equivalent (independent block storage, dynamic
# create/attach/detach, own lifecycle apart from any single sandbox
# session), NOT the FUSE object-storage mount the rest of that doc scopes.


class VolumeCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=200, description="Caller-supplied label for reference.")
    size_gb: float = Field(gt=0, le=1024, description="Requested volume size in GB (max 1024).")


class VolumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str | None = None
    size_gb: float
    status: Literal["queued", "creating", "ready", "failed", "deleting"]
    pvc_name: str | None = None
    failure_reason: str | None = None
    created_at: datetime


class VolumeAccepted(BaseModel):
    id: str
    label: str | None = None
    status: Literal["queued", "creating", "ready", "failed", "deleting"]
    created_at: datetime


# ── MCP OAuth 2.1 (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3) ────────
class OAuthClientRegisterRequest(BaseModel):
    """POST /oauth/register body (RFC 7591 Dynamic Client Registration).

    Only the two fields this server actually uses are modeled -- an MCP
    client may send other RFC 7591 fields (e.g. `grant_types`,
    `token_endpoint_auth_method`); pydantic's default `extra="ignore"`
    behavior (inherited from BaseModel's own default) means those are
    silently accepted and ignored rather than rejected, since this server
    only ever issues public clients using the two grant types it
    advertises in its own metadata document regardless of what a caller
    requests.
    """

    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    client_name: str = Field(min_length=1, max_length=200)

    @field_validator("redirect_uris")
    @classmethod
    def _validate_redirect_uris(cls, value: list[str]) -> list[str]:
        for uri in value:
            if not (
                uri.startswith("https://")
                or uri.startswith("http://localhost")
                or uri.startswith("http://127.0.0.1")
            ):
                raise ValueError(
                    f"redirect_uri {uri!r} must be https:// or a loopback http://localhost"
                    " / http://127.0.0.1 address (RFC 8252 native-app guidance)"
                )
        return value


class OAuthClientRegisterResponse(BaseModel):
    """RFC 7591 response shape. `client_secret` is deliberately never
    present -- every client registered here is `client_type="public"`."""

    client_id: str
    client_name: str
    redirect_uris: list[str]
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = ["authorization_code", "refresh_token"]
    response_types: list[str] = ["code"]


class OAuthTokenResponse(BaseModel):
    """RFC 6749 §5.1 token response shape, returned by both grant types
    POST /oauth/token supports."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    scope: str | None = None


# ── Public demo playground (issue #103) ─────────────────────────────────
DEMO_EXEC_COMMAND_MAX_LENGTH = 4000
# Ceiling on stdout/stderr returned by POST /v1/demo/sandboxes/{id}/exec --
# this route is unauthenticated, so a caller-controlled command generating
# a huge amount of output must never turn into an equally huge response
# payload. Independent of SANDBOX_FILE_CONTENT_MAX_LENGTH (the authenticated
# /exec route has no cap of its own today) since the two routes have very
# different trust levels.
DEMO_EXEC_OUTPUT_MAX_LENGTH = 20 * 1024


class DemoSandboxCreateRequest(BaseModel):
    """POST /v1/demo/sandboxes body. Every field is optional -- a plain
    `POST` with no body is the common case for a marketing-site widget."""

    lifetime_minutes: int | None = Field(
        default=None,
        ge=1,
        le=60,
        description=(
            "Requested sandbox lifetime in minutes. Always clamped down to "
            "BOXKITE_DEMO_LIFETIME_MINUTES regardless of what's requested "
            "here -- a caller can only ever ask for a SHORTER lifetime than "
            "the demo default, never a longer one."
        ),
    )


class DemoSandboxCreatedResponse(BaseModel):
    session_id: str
    token: str = Field(
        description=(
            "Short-lived, session-scoped bearer credential. Pass it back as "
            "the X-Demo-Token header on every subsequent call for this "
            "session_id -- exec and destroy both 401 without it."
        )
    )
    expires_at: datetime


class DemoSandboxExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=DEMO_EXEC_COMMAND_MAX_LENGTH)


class DemoSandboxExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = Field(
        description="True if stdout and/or stderr were cut off at DEMO_EXEC_OUTPUT_MAX_LENGTH."
    )


# ── Organizations / teams (basic) ──────────────────────────────────────────
class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Display name for the organization.")


class OrganizationResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    role: str = Field(description="The requesting account's role in this organization.")


class OrganizationMemberAddRequest(BaseModel):
    email: EmailStr = Field(description="Email of an existing account to add as a member.")
    role: Literal["admin", "member"] = Field(
        default="member", description="Role to grant. Ownership is only assigned at creation."
    )


class OrganizationMemberResponse(BaseModel):
    account_id: str
    email: str
    role: str
    created_at: datetime


class OrganizationInviteCreateRequest(BaseModel):
    email: EmailStr = Field(description="Email to invite (need not have an account yet).")
    role: Literal["admin", "member"] = Field(default="member")


class OrganizationInviteCreatedResponse(BaseModel):
    id: str
    email: str
    role: str
    expires_at: datetime
    token: str = Field(description="Single-use invite token — shown only once, at creation.")


class OrganizationInviteResponse(BaseModel):
    id: str
    email: str
    role: str
    created_at: datetime
    expires_at: datetime


class OrganizationInviteAcceptRequest(BaseModel):
    token: str = Field(min_length=1, description="The invite token from the invitation.")
