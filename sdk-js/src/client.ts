/**
 * Client for a hosted boxkite control-plane. Thin wrapper over the same v1
 * HTTP API the `boxkite` CLI and Python SDK call -- no behavior lives here
 * beyond request/response plumbing and the withSandbox() convenience.
 *
 * Works in Node (18+, global fetch) and the browser identically -- pass a
 * custom `fetchImpl` only for testing (a fake fetch) or an unusual runtime.
 *
 * SECURITY: never construct this with a real `apiKey` in code that ships to
 * a browser (or any other client-side bundle a user can inspect) -- an API
 * key is a full-privilege, long-lived account credential. See sdk-js's
 * README "Never put a real apiKey in code that ships to a browser" section.
 */

import { BoxkiteApiError, BoxkiteConnectionError } from "./errors.js";
import {
  computeBackoffMs,
  isIdempotentMethod,
  isRetriableStatus,
  parseRetryAfter,
  resolveRetryOptions,
  sleep,
  type ResolvedRetryOptions,
  type RetryOptions,
} from "./retry.js";
import type {
  Account,
  AllowedCommandsResponse,
  ExecResult,
  FileCreateResult,
  FileViewResult,
  GetLogResult,
  GlobResult,
  GrepResult,
  HttpRequestResult,
  Image,
  LogEntry,
  LsResult,
  McpConnection,
  MessageResponse,
  PreviewRevokeResult,
  PreviewUrl,
  ProcessInputResult,
  ProcessListResult,
  ProcessOutputResult,
  ProcessStartResult,
  ProcessStopResult,
  ProcessStreamEvent,
  Sandbox,
  Secret,
  StrReplaceResult,
  TokenPair,
  Usage,
  Volume,
  Webhook,
  WebhookDelivery,
} from "./types.js";

const DEFAULT_TIMEOUT_MS = 30_000;
const EXEC_TIMEOUT_HEADROOM_MS = 15_000;
const LOCALHOST_HOSTNAMES = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

/**
 * Reject a non-https baseUrl unless it points at localhost -- every request
 * sends `Authorization: Bearer ${apiKey}` (a full-privilege, long-lived
 * account credential), so an http:// URL to anything else would put it on
 * the wire in cleartext.
 */
function validateBaseUrlScheme(baseUrl: string): void {
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new Error(`Invalid baseUrl ${JSON.stringify(baseUrl)}: not a valid URL.`);
  }
  if (parsed.protocol === "https:") return;
  if (parsed.protocol === "http:" && LOCALHOST_HOSTNAMES.has(parsed.hostname)) return;
  throw new Error(
    `Refusing to use non-https baseUrl ${JSON.stringify(baseUrl)}: this would send your ` +
      "API key in cleartext. Use an https:// URL, or http://localhost (local dev only).",
  );
}

/** https:// -> wss://, http:// -> ws:// -- baseUrl has already passed
 * validateBaseUrlScheme, so it's always one of those two. */
function httpUrlToWs(baseUrl: string): string {
  if (baseUrl.startsWith("https://")) return "wss://" + baseUrl.slice("https://".length);
  if (baseUrl.startsWith("http://")) return "ws://" + baseUrl.slice("http://".length);
  throw new Error(`Unsupported baseUrl scheme: ${JSON.stringify(baseUrl)}`);
}

export interface BoxkiteClientOptions {
  baseUrl: string;
  /**
   * A boxkite account API key (`bxk_live_...`). SECURITY: never hardcode or
   * bundle a real value into browser-shipped code -- see the module
   * docstring above and sdk-js's README.
   */
  apiKey: string;
  fetchImpl?: typeof fetch;
  /** Per-request timeout in milliseconds. Requests reject once it elapses. */
  timeoutMs?: number;
  /**
   * WebSocket constructor used by `takeover()`. Defaults to the runtime's
   * global `WebSocket` (present in browsers and Node 22+) -- pass a custom
   * one only for testing (a fake implementation) or an unusual runtime.
   */
  wsImpl?: typeof WebSocket;
  /**
   * Opt-in automatic retry with exponential backoff + jitter for
   * transiently-failing idempotent requests (connection errors, HTTP 429,
   * and 5xx). Omit for no retries (unchanged behavior); pass `{}` for
   * sensible defaults, or override individual knobs. See RetryOptions.
   */
  retry?: RetryOptions;
}

export interface ExecOptions {
  timeout?: number;
  description?: string;
}

export interface FileOptions {
  description?: string;
}

export interface ViewOptions extends FileOptions {
  viewRange?: [number, number];
}

export interface StrReplaceOptions extends FileOptions {
  replaceAll?: boolean;
}

export interface LsOptions extends FileOptions {
  path?: string;
}

export interface GlobOptions extends FileOptions {
  path?: string;
}

export interface GrepOptions extends FileOptions {
  path?: string;
  glob?: string;
  maxMatches?: number;
}

export interface GetLogOptions {
  limit?: number;
  offset?: number;
}

export interface StartProcessOptions {
  description?: string;
  /** Hard ceiling on how long the process may run before being force-killed
   * (seconds). Defaults to 3600. */
  maxRuntimeSeconds?: number;
}

export interface GetProcessOutputOptions {
  /** Byte offset to read from (default 0, meaning everything currently
   * buffered). Use a previous response's `next_offset` to fetch only new
   * output since your last check. */
  sinceOffset?: number;
}

export interface CreateImageOptions {
  /** Optional human-readable label for the image. */
  label?: string;
  /** Base image to build from. Defaults to "boxkite-default". */
  base?:
    | "boxkite-default"
    | "boxkite-minimal"
    | "boxkite-node"
    | "boxkite-go"
    | "boxkite-nextjs"
    | "boxkite-rust";
  /** Exact-version-pinned Python packages (`name==version`, no ranges). */
  pythonPackages?: string[];
  /** Exact-version-pinned apt packages (`name==version`, no ranges). */
  aptPackages?: string[];
  /** Exact-version-pinned npm packages (`name==version` or
   * `@scope/name==version`, no ranges). */
  npmPackages?: string[];
}

export interface CreateVolumeOptions {
  /** Optional human-readable label for the volume. */
  label?: string;
  /** Requested volume size in GB (max 1024). */
  sizeGb: number;
}

export interface CreatePreviewUrlOptions {
  /** How long the minted preview URL stays valid, in seconds (30-86400).
   * Defaults to 900 (15 minutes) server-side when omitted. */
  ttlSeconds?: number;
}

/** Event types a webhook subscription can receive (docs/WEBHOOKS-DESIGN.md).
 * "audit_log.entry" added per GitHub issue #125 -- this union had drifted
 * out of sync with the control plane's own `WebhookEventType` literal
 * (control-plane/src/control_plane/schemas.py). */
export type WebhookEventType =
  | "sandbox.created"
  | "sandbox.destroyed"
  | "audit_log.entry";

export interface CreateWebhookOptions {
  /** HTTPS (or HTTP, for local testing) URL the control plane will POST
   * events to. Checked at registration time against the same private/
   * link-local/loopback/metadata-address denylist POST /v1/secrets uses
   * for allowedHosts. */
  url: string;
  /** Event types this subscription should receive. At least one required. */
  eventTypes: WebhookEventType[];
  /** Optional caller-supplied label for this subscription (e.g. "Slack notifier"). */
  description?: string;
}

export interface ListWebhookDeliveriesOptions {
  /** Maximum number of entries to return (server default 20, max 100). */
  limit?: number;
  /** Number of entries to skip, newest-first. */
  offset?: number;
}

/** Curated outbound-MCP catalog entry ids (GitHub issues #116/#117,
 * docs/OUTBOUND-MCP-DESIGN.md) -- restricted to boxkite's own reviewed
 * allowlist, never a caller-supplied hostname. */
export type McpCatalogId = "slack" | "notion" | "linear" | "github";

export interface CreateMcpConnectionOptions {
  /** Unique (per-account) name for this connection -- pass it in
   * createSandbox({ mcpConnectionNames: [...] }) to grant a session network
   * egress to it. */
  label: string;
  /** Which curated MCP catalog entry this connection grants network egress
   * to. */
  catalogId: McpCatalogId;
}

export interface CreateSecretOptions {
  /** Unique (per-account) name used to reference this secret from
   * createSandbox({ secretNames: [...] }) and from an agent tool call as
   * {{secret:name}} in a POST /http-request body/header. */
  name: string;
  /** The real credential value. Write-only -- accepted here and never
   * returned by this or any other route, including this response. */
  value: string;
  /** Destination hostnames this secret may be used against via POST
   * /http-request. Required, not optional -- an unscoped secret usable
   * against any destination defeats the point of this feature. A host that
   * resolves to a private/link-local/loopback/metadata address is rejected
   * at creation time (a best-effort backstop; see docs/SECRETS-DESIGN.md
   * §5 for why the real control is the sidecar's request-time check). */
  allowedHosts: string[];
  /** Only meaningful for wallet/private-key-style secrets
   * (docs/WALLET-SECRETS-DESIGN.md) -- omit for an ordinary API-key-style
   * secret. The only accepted value today is "testnet"; "mainnet" is
   * refused (422). */
  trustTier?: string;
}

/**
 * Parse a `text/event-stream` body (as an async iterable of decoded text
 * chunks) into decoded JSON payloads, one per SSE `data:` field. Only
 * `data:` is used -- `watch` doesn't need `event:`/`id:` framing, just the
 * ExecLogEntry payload each event carries.
 */
async function* parseSseEvents(chunks: AsyncIterable<string>): AsyncGenerator<any> {
  let buffer = "";
  let dataLines: string[] = [];
  for await (const chunk of chunks) {
    buffer += chunk;
    let newlineIndex: number;
    while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newlineIndex).replace(/\r$/, "");
      buffer = buffer.slice(newlineIndex + 1);
      if (line === "") {
        if (dataLines.length > 0) {
          yield JSON.parse(dataLines.join("\n"));
          dataLines = [];
        }
        continue;
      }
      if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trimStart());
      }
    }
  }
  if (dataLines.length > 0) {
    yield JSON.parse(dataLines.join("\n"));
  }
}

/** Adapts a fetch Response body (a byte ReadableStream) into an async
 * iterable of decoded text chunks -- Node's `Response.body` doesn't
 * implement `Symbol.asyncIterator` the way Node's own streams do. */
async function* readBodyAsText(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      yield decoder.decode(value, { stream: true });
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * A single account-level command allowlist rule. Either a bare command name
 * (matches any invocation of that command) or an object naming the command
 * plus optional argument allow/deny lists (`args_allow` / `args_deny`) for
 * finer-grained matching.
 */
export type AllowedCommandRule =
  | string
  | { command: string; args_allow?: string[]; args_deny?: string[] };

async function parseErrorBody(resp: Response): Promise<{ code: string; message: string }> {
  let code = "error";
  let message = `HTTP ${resp.status}`;
  try {
    const payload = await resp.json();
    if (payload && typeof payload === "object" && "error" in payload) {
      const err = (payload as { error?: { code?: string; message?: string } }).error;
      if (err?.message) message = err.message;
      if (err?.code) code = err.code;
    }
  } catch {
    // no JSON body -- fall back to the generic HTTP status message
  }
  return { code, message };
}

export class BoxkiteClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private readonly wsImpl: typeof WebSocket;
  private readonly retry: ResolvedRetryOptions;

  constructor(options: BoxkiteClientOptions) {
    validateBaseUrlScheme(options.baseUrl);
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.wsImpl = options.wsImpl ?? WebSocket;
    this.retry = resolveRetryOptions(options.retry);
  }

  private async request(
    method: string,
    path: string,
    body?: unknown,
    params?: Record<string, string>,
    timeoutMs?: number,
    headersOverride?: Record<string, string>,
  ): Promise<any> {
    let url = `${this.baseUrl}${path}`;
    if (params) {
      const qs = new URLSearchParams(params).toString();
      if (qs) url += `?${qs}`;
    }

    const canRetry = this.retry.maxRetries > 0 && isIdempotentMethod(method);
    let attempt = 0;
    for (;;) {
      let resp: Response;
      try {
        resp = await this.fetchImpl(url, {
          method,
          headers: {
            Authorization: `Bearer ${this.apiKey}`,
            ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
            ...headersOverride,
          },
          body: body !== undefined ? JSON.stringify(body) : undefined,
          signal: AbortSignal.timeout(timeoutMs ?? this.timeoutMs),
        });
      } catch (err) {
        if (canRetry && attempt < this.retry.maxRetries) {
          await sleep(computeBackoffMs(attempt, this.retry));
          attempt++;
          continue;
        }
        throw new BoxkiteConnectionError(err instanceof Error ? err.message : String(err));
      }

      if (!resp.ok) {
        if (canRetry && attempt < this.retry.maxRetries && isRetriableStatus(resp.status)) {
          await sleep(this.retryDelayMs(resp, attempt));
          attempt++;
          continue;
        }
        const { code, message } = await parseErrorBody(resp);
        throw new BoxkiteApiError(resp.status, code, message);
      }

      const text = await resp.text();
      return text ? JSON.parse(text) : null;
    }
  }

  /** Wait before the next retry: a server `Retry-After` header wins when
   * present and honored (capped at maxDelayMs), else computed backoff. */
  private retryDelayMs(resp: Response, attempt: number): number {
    if (this.retry.respectRetryAfter) {
      const retryAfter = parseRetryAfter(resp.headers.get("retry-after"));
      if (retryAfter !== null) return Math.min(retryAfter, this.retry.maxDelayMs);
    }
    return computeBackoffMs(attempt, this.retry);
  }

  /** GET /v1/account -- identity for the API key in use. */
  account(): Promise<{ id: string; email: string; created_at: string }> {
    return this.request("GET", "/v1/account");
  }

  /** GET /v1/usage -- current usage against fair-use limits. */
  usage(): Promise<{
    monthly_sandbox_hours_used: number;
    monthly_sandbox_hours_limit: number;
    concurrent_sandboxes: number;
    concurrent_sandboxes_limit: number;
  }> {
    return this.request("GET", "/v1/usage");
  }

  /**
   * POST /v1/auth/password-reset/request -- opt-in on the control-plane
   * (BOXKITE_PASSWORD_RESET_ENABLED); throws BoxkiteApiError(404,
   * "feature_disabled") if the deployment hasn't enabled it. Always
   * returns the same message whether or not the email is registered, so
   * this call can never be used to enumerate accounts. Email delivery is
   * stubbed server-side unless the deployment has wired up a real
   * EmailSender (see control-plane/src/control_plane/email_sender.py).
   */
  requestPasswordReset(email: string): Promise<MessageResponse> {
    return this.request("POST", "/v1/auth/password-reset/request", { email });
  }

  /**
   * POST /v1/auth/password-reset/confirm -- consumes a single-use token
   * minted by requestPasswordReset() and sets a new password. Also revokes
   * every outstanding refresh token for the account, if refresh tokens are
   * enabled server-side. Throws BoxkiteApiError(400,
   * "invalid_or_expired_token") for an unknown, already-used, or expired
   * token.
   */
  confirmPasswordReset(token: string, newPassword: string): Promise<MessageResponse> {
    return this.request("POST", "/v1/auth/password-reset/confirm", {
      token,
      new_password: newPassword,
    });
  }

  /**
   * POST /v1/auth/verify-email -- opt-in
   * (BOXKITE_EMAIL_VERIFICATION_ENABLED). Consumes a single-use token
   * (minted automatically at signup, or by resendVerification()) and marks
   * the account's email verified. Throws BoxkiteApiError(400,
   * "invalid_or_expired_token") for an unknown, already-used, or expired
   * token.
   */
  verifyEmail(token: string): Promise<MessageResponse> {
    return this.request("POST", "/v1/auth/verify-email", { token });
  }

  /**
   * POST /v1/auth/resend-verification -- opt-in
   * (BOXKITE_EMAIL_VERIFICATION_ENABLED). Requires a dashboard session
   * token (the JWT returned by /v1/auth/login or /v1/auth/signup), not
   * this client's apiKey -- apiKey and a dashboard JWT are two different,
   * non-interchangeable credential types on this control-plane (see
   * control-plane/src/control_plane/deps.py), so the JWT is passed
   * explicitly here and overrides this call's Authorization header rather
   * than using this.apiKey.
   */
  resendVerification(accessToken: string): Promise<MessageResponse> {
    return this.request("POST", "/v1/auth/resend-verification", undefined, undefined, undefined, {
      Authorization: `Bearer ${accessToken}`,
    });
  }

  /**
   * POST /v1/auth/refresh -- opt-in (BOXKITE_REFRESH_TOKENS_ENABLED).
   * Exchanges a still-valid refresh token for a brand new accessToken +
   * refreshToken pair, revoking the presented one in the same request
   * (rotation, not reuse) -- store the new refresh_token from the response
   * and discard the one you presented. Throws BoxkiteApiError(401,
   * "invalid_refresh_token") if the token is unknown/expired, or (401,
   * "refresh_token_reused") if it was already rotated out or revoked
   * (which also revokes every other refresh token on the account as a
   * precaution).
   */
  refreshToken(refreshToken: string): Promise<TokenPair> {
    return this.request("POST", "/v1/auth/refresh", { refresh_token: refreshToken });
  }

  /**
   * POST /v1/auth/logout -- opt-in (BOXKITE_REFRESH_TOKENS_ENABLED).
   * Revokes one refresh token immediately. Always succeeds (204) whether
   * or not the token was valid -- never leaks which.
   */
  async logout(refreshToken: string): Promise<void> {
    await this.request("POST", "/v1/auth/logout", { refresh_token: refreshToken });
  }

  /**
   * POST /v1/sandboxes -- create one or more sandboxes.
   *
   * @param options.label Optional human-readable label for the sandbox.
   * @param options.size Sandbox size, one of "small" | "medium" | "large".
   *   Controls the compute resources allocated to the sandbox.
   * @param options.storageGb Persistent storage, in GB, to attach to the sandbox.
   * @param options.lifetimeMinutes Maximum lifetime of the sandbox, in minutes,
   *   before it is automatically destroyed.
   * @param options.count Number of sandboxes to create in this request.
   *   When greater than 1 the control plane returns an array of sandboxes
   *   rather than a single object; the return type here reflects the common
   *   single-sandbox case, so cast the result when using count > 1.
   * @param options.secretNames Names of this account's secrets (see
   *   POST /v1/secrets) this session should be granted access to via the
   *   sidecar's secrets-broker httpRequest tool (docs/SECRETS-DESIGN.md). A
   *   name that doesn't exist for this account 404s before any sandbox is
   *   created.
   * @param options.imageId ID of a custom sandbox image built via createImage()
   *   to use for this sandbox, instead of the account's default image.
   * @param options.mcpConnectionNames Labels of this account's outbound-MCP
   *   connections (see createMcpConnection(), GitHub issues #116/#117) this
   *   session should be granted network egress to. A name that doesn't exist
   *   for this account 404s before any sandbox is created, same precedent as
   *   secretNames. This only widens the session's per-pod NetworkPolicy
   *   egress allowlist -- there is no MCP-proxy transport yet
   *   (docs/OUTBOUND-MCP-DESIGN.md section 6), so a granted connection does
   *   not yet let an agent actually speak MCP to it.
   * @param options.volumeMounts Optional {volume_id: mount_path} mapping of
   *   independent PVC-backed volumes (see createVolume()) to mount into this
   *   sandbox. Every volume_id must already exist for the caller's account
   *   and be status "ready".
   * @param options.gpuCount Opt-in, experimental (docs/GPU-SUPPORT-SCOPING.md)
   *   -- requests this many GPUs as a Kubernetes extended-resource limit.
   *   422s (gpu_support_disabled) unless the deployment has
   *   BOXKITE_GPU_ENABLED set and a GPU-equipped node pool with a device
   *   plugin provisioned; not verified against real GPU hardware in this
   *   codebase.
   */
  createSandbox(options?: {
    label?: string;
    size?: "small" | "medium" | "large";
    storageGb?: number;
    lifetimeMinutes?: number;
    count?: number;
    secretNames?: string[];
    imageId?: string;
    mcpConnectionNames?: string[];
    volumeMounts?: Record<string, string>;
    gpuCount?: number;
  }): Promise<Sandbox> {
    const body: Record<string, unknown> = {};
    if (options?.label !== undefined) body.label = options.label;
    if (options?.size !== undefined) body.size = options.size;
    if (options?.storageGb !== undefined) body.storage_gb = options.storageGb;
    if (options?.lifetimeMinutes !== undefined) body.lifetime_minutes = options.lifetimeMinutes;
    if (options?.count !== undefined) body.count = options.count;
    if (options?.secretNames !== undefined) body.secret_names = options.secretNames;
    if (options?.imageId !== undefined) body.image_id = options.imageId;
    if (options?.mcpConnectionNames !== undefined) body.mcp_connection_names = options.mcpConnectionNames;
    if (options?.volumeMounts !== undefined) body.volume_mounts = options.volumeMounts;
    if (options?.gpuCount !== undefined) body.gpu_count = options.gpuCount;
    return this.request("POST", "/v1/sandboxes", body);
  }

  getSandbox(sessionId: string): Promise<Sandbox> {
    return this.request("GET", `/v1/sandboxes/${sessionId}`);
  }

  async listSandboxes(options?: { activeOnly?: boolean }): Promise<Sandbox[]> {
    const result = await this.request("GET", "/v1/sandboxes", undefined, {
      active_only: String(Boolean(options?.activeOnly)),
    });
    return result ?? [];
  }

  async destroySandbox(sessionId: string): Promise<void> {
    await this.request("DELETE", `/v1/sandboxes/${sessionId}`);
  }

  exec(sessionId: string, command: string, options?: ExecOptions): Promise<ExecResult> {
    const body: Record<string, unknown> = { command };
    if (options?.timeout !== undefined) body.timeout = options.timeout;
    if (options?.description !== undefined) body.description = options.description;
    const timeoutMs =
      options?.timeout !== undefined ? options.timeout * 1000 + EXEC_TIMEOUT_HEADROOM_MS : undefined;
    return this.request("POST", `/v1/sandboxes/${sessionId}/exec`, body, undefined, timeoutMs);
  }

  /**
   * POST /v1/sandboxes/{id}/http-request -- the secrets-broker HTTP request
   * (docs/SECRETS-DESIGN.md). `headers`/`body` may contain a literal
   * `{{secret:name}}` reference for a secret granted to this session via
   * `createSandbox({ secretNames: [...] })`; the sidecar substitutes the
   * real value in-process -- this SDK/client never sees it.
   */
  httpRequest(
    sessionId: string,
    method: string,
    url: string,
    options?: { headers?: Record<string, string>; body?: string; timeout?: number },
  ): Promise<HttpRequestResult> {
    const body: Record<string, unknown> = { method, url };
    if (options?.headers !== undefined) body.headers = options.headers;
    if (options?.body !== undefined) body.body = options.body;
    if (options?.timeout !== undefined) body.timeout = options.timeout;
    const timeoutMs =
      options?.timeout !== undefined ? options.timeout * 1000 + EXEC_TIMEOUT_HEADROOM_MS : undefined;
    return this.request("POST", `/v1/sandboxes/${sessionId}/http-request`, body, undefined, timeoutMs);
  }

  fileCreate(
    sessionId: string,
    path: string,
    content: string,
    options?: FileOptions,
  ): Promise<FileCreateResult> {
    const body: Record<string, unknown> = { path, content };
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/files`, body);
  }

  view(sessionId: string, path: string, options?: ViewOptions): Promise<FileViewResult> {
    const body: Record<string, unknown> = { path };
    if (options?.viewRange !== undefined) body.view_range = options.viewRange;
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/files/view`, body);
  }

  strReplace(
    sessionId: string,
    path: string,
    oldStr: string,
    newStr: string,
    options?: StrReplaceOptions,
  ): Promise<StrReplaceResult> {
    const body: Record<string, unknown> = {
      path,
      old_str: oldStr,
      new_str: newStr,
      replace_all: Boolean(options?.replaceAll),
    };
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/files/str-replace`, body);
  }

  /** POST /v1/sandboxes/{sessionId}/files/ls -- list direct children of a directory. */
  ls(sessionId: string, options?: LsOptions): Promise<LsResult> {
    const body: Record<string, unknown> = {};
    if (options?.path !== undefined) body.path = options.path;
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/files/ls`, body);
  }

  /** POST /v1/sandboxes/{sessionId}/files/glob -- find files by name pattern. */
  glob(sessionId: string, pattern: string, options?: GlobOptions): Promise<GlobResult> {
    const body: Record<string, unknown> = { pattern };
    if (options?.path !== undefined) body.path = options.path;
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/files/glob`, body);
  }

  /** POST /v1/sandboxes/{sessionId}/files/grep -- search file contents by regex. */
  grep(sessionId: string, pattern: string, options?: GrepOptions): Promise<GrepResult> {
    const body: Record<string, unknown> = { pattern };
    if (options?.path !== undefined) body.path = options.path;
    if (options?.glob !== undefined) body.glob = options.glob;
    if (options?.maxMatches !== undefined) body.max_matches = options.maxMatches;
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/files/grep`, body);
  }

  /** GET /v1/sandboxes/{sessionId}/log -- paginated exec/file-op audit
   * history (`docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3). */
  getLog(sessionId: string, options?: GetLogOptions): Promise<GetLogResult> {
    const params: Record<string, string> = {};
    if (options?.limit !== undefined) params.limit = String(options.limit);
    if (options?.offset !== undefined) params.offset = String(options.offset);
    return this.request("GET", `/v1/sandboxes/${sessionId}/log`, undefined, params);
  }

  /**
   * POST /v1/sandboxes/{sessionId}/processes -- start a background process
   * (a dev server, a test watcher, a long build, a REPL) that keeps running
   * after this call returns.
   *
   * Distinct from `exec`: `exec` is one-shot request/response, bounded by
   * its own `timeout`. Poll the returned `process_id`'s output with
   * `getProcessOutput`, feed it input with `sendProcessInput`, and stop it
   * with `stopProcess`. See `docs/PROCESS-SESSIONS-DESIGN.md`.
   */
  startProcess(
    sessionId: string,
    command: string,
    options?: StartProcessOptions,
  ): Promise<ProcessStartResult> {
    const body: Record<string, unknown> = {
      command,
      max_runtime_seconds: options?.maxRuntimeSeconds ?? 3600,
    };
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", `/v1/sandboxes/${sessionId}/processes`, body);
  }

  /** GET /v1/sandboxes/{sessionId}/processes -- every background process
   * currently tracked for this session. */
  listProcesses(sessionId: string): Promise<ProcessListResult> {
    return this.request("GET", `/v1/sandboxes/${sessionId}/processes`);
  }

  /**
   * GET /v1/sandboxes/{sessionId}/processes/{processId}/output -- poll a
   * background process's output since a given byte offset. Polling-style,
   * not streaming.
   */
  getProcessOutput(
    sessionId: string,
    processId: string,
    options?: GetProcessOutputOptions,
  ): Promise<ProcessOutputResult> {
    const params: Record<string, string> = { since_offset: String(options?.sinceOffset ?? 0) };
    return this.request("GET", `/v1/sandboxes/${sessionId}/processes/${processId}/output`, undefined, params);
  }

  /**
   * GET /v1/sandboxes/{sessionId}/processes/{processId}/stream -- live SSE
   * stream of a background process's stdout, the streaming counterpart to
   * `getProcessOutput`'s polling. Yields one event per SSE `data:` line: an
   * `{ type: "output", ... }` per new chunk, then a terminal
   * `{ type: "exit", ... }` when the process finishes.
   *
   * `break`ing out of a `for await` loop over it (or calling `.return()`)
   * closes the underlying stream.
   */
  async *streamProcessOutput(
    sessionId: string,
    processId: string,
    options?: GetProcessOutputOptions,
  ): AsyncGenerator<ProcessStreamEvent> {
    const sinceOffset = String(options?.sinceOffset ?? 0);
    const url = `${this.baseUrl}/v1/sandboxes/${sessionId}/processes/${processId}/stream?since_offset=${sinceOffset}`;
    let resp: Response;
    try {
      resp = await this.fetchImpl(url, {
        method: "GET",
        headers: { Authorization: `Bearer ${this.apiKey}` },
      });
    } catch (err) {
      throw new BoxkiteConnectionError(err instanceof Error ? err.message : String(err));
    }
    if (!resp.ok) {
      const { code, message } = await parseErrorBody(resp);
      throw new BoxkiteApiError(resp.status, code, message);
    }
    if (!resp.body) {
      throw new BoxkiteConnectionError("stream response had no body to stream");
    }
    yield* parseSseEvents(readBodyAsText(resp.body)) as AsyncGenerator<ProcessStreamEvent>;
  }

  /** POST /v1/sandboxes/{sessionId}/processes/{processId}/input -- write to
   * a tracked background process's stdin pipe. */
  sendProcessInput(sessionId: string, processId: string, data: string): Promise<ProcessInputResult> {
    return this.request("POST", `/v1/sandboxes/${sessionId}/processes/${processId}/input`, { data });
  }

  /** POST /v1/sandboxes/{sessionId}/processes/{processId}/stop -- stop a
   * tracked background process (SIGTERM, then SIGKILL if it doesn't exit
   * within a short grace period). */
  stopProcess(sessionId: string, processId: string): Promise<ProcessStopResult> {
    return this.request("POST", `/v1/sandboxes/${sessionId}/processes/${processId}/stop`);
  }

  /**
   * GET /v1/sandboxes/{sessionId}/watch -- streams new audit-log entries as
   * they're written, one decoded JSON object per Server-Sent Event `data:`
   * line. This is a live feed of exec/file operations as control-plane logs
   * them, not a live terminal -- see
   * `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 2 ("Live watch").
   *
   * The returned async generator stays open for as long as the server keeps
   * the connection alive; `break`ing out of a `for await` loop over it (or
   * calling `.return()`) closes the underlying stream.
   */
  async *watch(sessionId: string): AsyncGenerator<LogEntry> {
    let resp: Response;
    try {
      resp = await this.fetchImpl(`${this.baseUrl}/v1/sandboxes/${sessionId}/watch`, {
        method: "GET",
        headers: { Authorization: `Bearer ${this.apiKey}` },
      });
    } catch (err) {
      throw new BoxkiteConnectionError(err instanceof Error ? err.message : String(err));
    }

    if (!resp.ok) {
      const { code, message } = await parseErrorBody(resp);
      throw new BoxkiteApiError(resp.status, code, message);
    }
    if (!resp.body) {
      throw new BoxkiteConnectionError("watch response had no body to stream");
    }
    yield* parseSseEvents(readBodyAsText(resp.body));
  }

  /**
   * WS /v1/sandboxes/{sessionId}/takeover -- interactive human takeover of a
   * sandbox session's shell: a raw duplex byte stream proxied straight
   * through to the sandbox's PTY (see `docs/API.md`). There is no message
   * envelope -- send and receive raw bytes on the returned WebSocket exactly
   * as you would over a local terminal.
   *
   * First calls `POST /v1/sandboxes/{sessionId}/takeover-token` (a normal
   * `Authorization: Bearer` request, RBAC-checked there -- see
   * `docs/API.md` and SECURITY.md's "Human takeover" section) to mint a
   * short-lived, single-use token, then opens the WebSocket with
   * `?token=<that token>` -- NOT `?api_key=<the real apiKey>`. This SDK
   * used to put the long-lived apiKey directly on the WS URL (the browser
   * WebSocket API cannot set a custom header at all, and this SDK runs in
   * Node and the browser identically -- see the module docstring); that
   * put a full-privilege credential in access logs and browser history.
   * The mint call itself still uses the normal Authorization header, so it
   * carries none of that exposure.
   *
   * A `member`-role apiKey (`POST /v1/api-keys`'s `role` field -- minted
   * via the dashboard JWT-authenticated API-keys endpoint, not this SDK)
   * rejects the mint call with a 403 `BoxkiteApiError` before any
   * WebSocket is even attempted. A missing/invalid/expired takeover token
   * closes the
   * WebSocket with close code 4401; an unowned or already-destroyed
   * sessionId closes it with 4404 -- both surface as the socket's `close`
   * event (see `event.code`), since the close happens after the opening
   * handshake completes.
   *
   * Resolves once the connection is open; rejects with
   * BoxkiteConnectionError if the socket errors before ever opening, or
   * with BoxkiteApiError if the takeover-token mint call itself fails.
   */
  async takeover(sessionId: string): Promise<WebSocket> {
    const { token } = (await this.request("POST", `/v1/sandboxes/${sessionId}/takeover-token`)) as {
      token: string;
    };
    const wsUrl =
      `${httpUrlToWs(this.baseUrl)}/v1/sandboxes/${sessionId}/takeover` + `?token=${encodeURIComponent(token)}`;

    return new Promise((resolve, reject) => {
      let socket: WebSocket;
      try {
        socket = new this.wsImpl(wsUrl);
      } catch (err) {
        reject(new BoxkiteConnectionError(err instanceof Error ? err.message : String(err)));
        return;
      }
      socket.binaryType = "arraybuffer";

      const onOpen = () => {
        socket.removeEventListener("open", onOpen);
        socket.removeEventListener("error", onError);
        resolve(socket);
      };
      const onError = () => {
        socket.removeEventListener("open", onOpen);
        socket.removeEventListener("error", onError);
        reject(new BoxkiteConnectionError(`Failed to open takeover WebSocket to ${wsUrl}`));
      };
      socket.addEventListener("open", onOpen);
      socket.addEventListener("error", onError);
    });
  }

  /**
   * WS /v1/sandboxes/{sessionId}/desktop -- interactive GUI/remote-desktop
   * human takeover of a sandbox session (VNC over a raw byte stream, proxied
   * straight through to the sidecar's `WS /desktop`), structurally identical
   * to `takeover()` but bridging a full desktop instead of a shell -- see
   * `docs/API.md`'s `WS .../desktop` section and SECURITY.md's "New trust
   * boundary: remote desktop takeover" section.
   *
   * First calls `POST /v1/sandboxes/{sessionId}/desktop-token` (a normal
   * `Authorization: Bearer` request, RBAC-checked there) to mint a
   * short-lived, single-use token, then opens the WebSocket with
   * `?token=<that token>` -- same mint-then-connect pattern as `takeover()`,
   * for the same reason (the browser WebSocket API cannot set a custom
   * header).
   *
   * Reuses `takeover()`'s `can_initiate_takeover` RBAC gate as-is (an
   * "admin"-role apiKey only) -- there is no dedicated `can_initiate_desktop`
   * permission yet, and no `read_only` variant of this connection. A
   * `member`-role apiKey rejects the mint call with a 403 `BoxkiteApiError`
   * (`desktop_not_permitted`) before any WebSocket is even attempted. A
   * missing/invalid/expired desktop token closes the WebSocket with close
   * code 4401; an unowned or already-destroyed sessionId closes it with
   * 4404 -- both surface as the socket's `close` event, since the close
   * happens after the opening handshake completes. 404s (as a normal
   * BoxkiteApiError from the mint call) when this deployment has not set
   * `BOXKITE_DESKTOP_ENABLED`.
   *
   * Resolves once the connection is open; rejects with
   * BoxkiteConnectionError if the socket errors before ever opening, or
   * with BoxkiteApiError if the desktop-token mint call itself fails.
   */
  async desktopTakeover(sessionId: string): Promise<WebSocket> {
    const { token } = (await this.request("POST", `/v1/sandboxes/${sessionId}/desktop-token`)) as {
      token: string;
    };
    const wsUrl =
      `${httpUrlToWs(this.baseUrl)}/v1/sandboxes/${sessionId}/desktop` + `?token=${encodeURIComponent(token)}`;

    return new Promise((resolve, reject) => {
      let socket: WebSocket;
      try {
        socket = new this.wsImpl(wsUrl);
      } catch (err) {
        reject(new BoxkiteConnectionError(err instanceof Error ? err.message : String(err)));
        return;
      }
      socket.binaryType = "arraybuffer";

      const onOpen = () => {
        socket.removeEventListener("open", onOpen);
        socket.removeEventListener("error", onError);
        resolve(socket);
      };
      const onError = () => {
        socket.removeEventListener("open", onOpen);
        socket.removeEventListener("error", onError);
        reject(new BoxkiteConnectionError(`Failed to open desktop WebSocket to ${wsUrl}`));
      };
      socket.addEventListener("open", onOpen);
      socket.addEventListener("error", onError);
    });
  }

  /**
   * POST /v1/sandboxes/{sessionId}/preview/{port} -- mint a signed,
   * time-limited URL that proxies HTTP traffic to a port a background
   * process opened inside this session (see startProcess's `expose_port`).
   * The returned URL carries its own authorization -- no API key is
   * required to use it, only to mint it (docs/API.md's "Network ingress
   * preview URLs" section).
   *
   * @param options.ttlSeconds How long the minted URL stays valid, in
   *   seconds (30-86400). Defaults to 900 (15 minutes) server-side when
   *   omitted.
   *
   * Returns the raw `{ url, expires_at, token_id }` body. Save `token_id` if
   * you might need to revoke this one link early via `revokePreviewUrl()`,
   * without tearing down the session or affecting any other preview token
   * minted for the same session/port.
   */
  createPreviewUrl(
    sessionId: string,
    port: number,
    options?: CreatePreviewUrlOptions,
  ): Promise<PreviewUrl> {
    const body: Record<string, unknown> = {};
    if (options?.ttlSeconds !== undefined) body.ttl_seconds = options.ttlSeconds;
    return this.request("POST", `/v1/sandboxes/${sessionId}/preview/${port}`, body);
  }

  /**
   * POST /v1/sandboxes/{sessionId}/preview/{port}/revoke -- invalidate one
   * specific preview-URL token (its `token_id` from `createPreviewUrl()`)
   * before its TTL expires, without tearing down the sandbox session and
   * without affecting any other preview token minted for the same
   * session/port.
   *
   * Idempotent: revoking an already-revoked, already-expired, or
   * unrecognized `tokenId` still returns `{ revoked: true, ... }` rather
   * than throwing -- the caller cannot distinguish "this token never
   * existed" from "someone already revoked it".
   */
  revokePreviewUrl(sessionId: string, port: number, tokenId: string): Promise<PreviewRevokeResult> {
    return this.request("POST", `/v1/sandboxes/${sessionId}/preview/${port}/revoke`, { token_id: tokenId });
  }

  /**
   * GET /v1/account/allowed-commands -- the account's current command
   * allowlist rules.
   *
   * This is an opt-in guardrail for restricting which shell commands `exec`
   * will run, not a sandbox-escape boundary -- it does not substitute for the
   * sandbox's own isolation (see SECURITY.md). An empty `rules` list means no
   * allowlist is configured (all commands permitted, subject to the sandbox's
   * own constraints).
   */
  getAllowedCommands(): Promise<AllowedCommandsResponse> {
    return this.request("GET", "/v1/account/allowed-commands");
  }

  /**
   * PUT /v1/account/allowed-commands -- replace the account's command
   * allowlist rules wholesale.
   *
   * @param rules Each rule is either a bare command name (string) or an
   *   object `{ command, args_allow?, args_deny? }` narrowing which
   *   arguments are permitted (`args_allow`) or forbidden (`args_deny`) for
   *   that command. This is an opt-in guardrail, not a sandbox-escape
   *   boundary -- see SECURITY.md.
   */
  setAllowedCommands(rules: AllowedCommandRule[]): Promise<AllowedCommandsResponse> {
    return this.request("PUT", "/v1/account/allowed-commands", { rules });
  }

  /**
   * DELETE /v1/account/allowed-commands -- clear the account's command
   * allowlist rules, returning to the unrestricted (no allowlist) default.
   */
  async clearAllowedCommands(): Promise<void> {
    await this.request("DELETE", "/v1/account/allowed-commands");
  }

  /**
   * POST /v1/images -- build a custom sandbox image (a declarative builder:
   * a base image plus exact-version-pinned Python/apt packages). Returns
   * immediately with a 202-style pending record; poll `getImage()` for the
   * built image's `status` to reach a terminal state.
   *
   * @param options.label Optional human-readable label for the image.
   * @param options.base Base image to build from, one of
   *   "boxkite-default" | "boxkite-minimal" | "boxkite-node" | "boxkite-go" |
   *   "boxkite-nextjs" | "boxkite-rust". Defaults to "boxkite-default".
   *   "boxkite-node" drops Python entirely (no pythonPackages), "boxkite-go"
   *   and "boxkite-rust" both drop Python and Node entirely (no
   *   pythonPackages or npmPackages), and "boxkite-nextjs" is the same
   *   Node-only runtime as "boxkite-node" plus a pre-installed Next.js App
   *   Router starter (same pythonPackages restriction as "boxkite-node").
   * @param options.pythonPackages Exact-version-pinned Python packages
   *   (`name==version`, no ranges).
   * @param options.aptPackages Exact-version-pinned apt packages
   *   (`name==version`, no ranges).
   * @param options.npmPackages Exact-version-pinned npm packages
   *   (`name==version` or `@scope/name==version`, no ranges).
   */
  createImage(options?: CreateImageOptions): Promise<Image> {
    const body: Record<string, unknown> = {};
    if (options?.label !== undefined) body.label = options.label;
    if (options?.base !== undefined) body.base = options.base;
    if (options?.pythonPackages !== undefined) body.python_packages = options.pythonPackages;
    if (options?.aptPackages !== undefined) body.apt_packages = options.aptPackages;
    if (options?.npmPackages !== undefined) body.npm_packages = options.npmPackages;
    return this.request("POST", "/v1/images", body);
  }

  /** GET /v1/images/{imageId} -- a custom sandbox image's build status and
   * details. */
  getImage(imageId: string): Promise<Image> {
    return this.request("GET", `/v1/images/${imageId}`);
  }

  /** GET /v1/images -- every custom sandbox image built for this account. */
  async listImages(): Promise<Image[]> {
    const result = await this.request("GET", "/v1/images");
    return result ?? [];
  }

  /** DELETE /v1/images/{imageId} -- delete a custom sandbox image. */
  async deleteImage(imageId: string): Promise<void> {
    await this.request("DELETE", `/v1/images/${imageId}`);
  }

  /**
   * POST /v1/volumes -- create an independent, PVC-backed storage volume
   * (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum). Returns
   * immediately with a pending record; poll `getVolume()` for the volume's
   * `status` to reach "ready" (or "failed") before mounting it via
   * `createSandbox({ volumeMounts })`.
   *
   * @param options.label Optional human-readable label for the volume.
   * @param options.sizeGb Requested volume size in GB (max 1024).
   */
  createVolume(options: CreateVolumeOptions): Promise<Volume> {
    const body: Record<string, unknown> = { size_gb: options.sizeGb };
    if (options?.label !== undefined) body.label = options.label;
    return this.request("POST", "/v1/volumes", body);
  }

  /** GET /v1/volumes/{volumeId} -- an independent storage volume's status
   * and details. */
  getVolume(volumeId: string): Promise<Volume> {
    return this.request("GET", `/v1/volumes/${volumeId}`);
  }

  /** GET /v1/volumes -- every independent storage volume created for this
   * account. */
  async listVolumes(): Promise<Volume[]> {
    const result = await this.request("GET", "/v1/volumes");
    return result ?? [];
  }

  /** DELETE /v1/volumes/{volumeId} -- delete an independent storage volume. */
  async deleteVolume(volumeId: string): Promise<void> {
    await this.request("DELETE", `/v1/volumes/${volumeId}`);
  }

  /**
   * POST /v1/webhooks -- register a webhook subscription
   * (docs/WEBHOOKS-DESIGN.md). Returns the subscription plus a `secret`
   * field -- the raw signing secret, shown exactly once. Use it to verify
   * the `X-Boxkite-Webhook-Signature` header on every delivery; it cannot
   * be retrieved again after this response.
   */
  createWebhook(options: CreateWebhookOptions): Promise<Webhook> {
    const body: Record<string, unknown> = {
      url: options.url,
      event_types: options.eventTypes,
    };
    if (options?.description !== undefined) body.description = options.description;
    return this.request("POST", "/v1/webhooks", body);
  }

  /** GET /v1/webhooks -- webhook subscriptions for this account. The
   * signing secret is never returned here. */
  async listWebhooks(): Promise<Webhook[]> {
    const result = await this.request("GET", "/v1/webhooks");
    return result ?? [];
  }

  /** DELETE /v1/webhooks/{subscriptionId} -- delete a webhook subscription
   * owned by this account. 404s if already gone or never owned by this
   * account. */
  async deleteWebhook(subscriptionId: string): Promise<void> {
    await this.request("DELETE", `/v1/webhooks/${subscriptionId}`);
  }

  /**
   * GET /v1/webhooks/{subscriptionId}/deliveries -- recent delivery
   * attempts (pending/delivered/failed) for this subscription, newest
   * first -- observability into the retry/backoff behavior described in
   * docs/WEBHOOKS-DESIGN.md.
   */
  async listWebhookDeliveries(
    subscriptionId: string,
    options?: ListWebhookDeliveriesOptions,
  ): Promise<WebhookDelivery[]> {
    const params: Record<string, string> = {};
    if (options?.limit !== undefined) params.limit = String(options.limit);
    if (options?.offset !== undefined) params.offset = String(options.offset);
    const result = await this.request(
      "GET",
      `/v1/webhooks/${subscriptionId}/deliveries`,
      undefined,
      params,
    );
    return result ?? [];
  }

  /**
   * POST /v1/mcp-connections -- grant this account access to one curated
   * outbound-MCP catalog entry (GitHub issues #116/#117,
   * docs/OUTBOUND-MCP-DESIGN.md). Note: this only widens a granted session's
   * per-pod NetworkPolicy egress allowlist to the connection's catalog
   * hostname -- there is no MCP-proxy transport yet, so this does not yet
   * let an agent speak MCP protocol to the destination.
   */
  createMcpConnection(options: CreateMcpConnectionOptions): Promise<McpConnection> {
    return this.request("POST", "/v1/mcp-connections", {
      label: options.label,
      catalog_id: options.catalogId,
    });
  }

  /** GET /v1/mcp-connections -- outbound-MCP connection grants for this
   * account. */
  async listMcpConnections(): Promise<McpConnection[]> {
    const result = await this.request("GET", "/v1/mcp-connections");
    return result ?? [];
  }

  /** DELETE /v1/mcp-connections/{connectionId} -- delete an outbound-MCP
   * connection grant owned by this account. 404s if already gone or never
   * owned by this account. */
  async deleteMcpConnection(connectionId: string): Promise<void> {
    await this.request("DELETE", `/v1/mcp-connections/${connectionId}`);
  }

  /**
   * POST /v1/secrets -- register a new org-scoped secret for the
   * proxy-substitution secrets broker (docs/SECRETS-DESIGN.md). Returns the
   * created secret's metadata (id, name, allowedHosts, trustTier,
   * createdAt, lastUsedAt) -- never the raw value.
   */
  createSecret(options: CreateSecretOptions): Promise<Secret> {
    const body: Record<string, unknown> = {
      name: options.name,
      value: options.value,
      allowed_hosts: options.allowedHosts,
    };
    if (options.trustTier !== undefined) body.trust_tier = options.trustTier;
    return this.request("POST", "/v1/secrets", body);
  }

  /** GET /v1/secrets -- secrets registered for this account. Raw values
   * are never returned here. */
  async listSecrets(): Promise<Secret[]> {
    const result = await this.request("GET", "/v1/secrets");
    return result ?? [];
  }

  /** DELETE /v1/secrets/{secretId} -- delete a secret owned by this
   * account. 404s if already gone or never owned by this account. */
  async deleteSecret(secretId: string): Promise<void> {
    await this.request("DELETE", `/v1/secrets/${secretId}`);
  }

  /**
   * Create-on-enter, destroy-on-exit convenience -- the callback-based
   * equivalent of the Python SDK's `with client.sandbox() as sb:` context
   * manager, since JS has no stable cross-runtime resource-disposal syntax
   * for this yet. Destroys the sandbox even if `fn` throws.
   */
  async withSandbox<T>(
    fn: (sb: SandboxSession) => Promise<T>,
    options?: { label?: string; secretNames?: string[]; mcpConnectionNames?: string[] },
  ): Promise<T> {
    const created = await this.createSandbox(options);
    const sb = new SandboxSession(this, created.id);
    try {
      return await fn(sb);
    } finally {
      try {
        await this.destroySandbox(created.id);
      } catch {
        // best-effort teardown -- an already-gone session shouldn't throw on cleanup
      }
    }
  }
}

export class SandboxSession {
  readonly id: string;
  private readonly client: BoxkiteClient;

  constructor(client: BoxkiteClient, id: string) {
    this.client = client;
    this.id = id;
  }

  exec(command: string, options?: ExecOptions): Promise<ExecResult> {
    return this.client.exec(this.id, command, options);
  }

  httpRequest(
    method: string,
    url: string,
    options?: { headers?: Record<string, string>; body?: string; timeout?: number },
  ): Promise<HttpRequestResult> {
    return this.client.httpRequest(this.id, method, url, options);
  }

  fileCreate(path: string, content: string, options?: FileOptions): Promise<FileCreateResult> {
    return this.client.fileCreate(this.id, path, content, options);
  }

  view(path: string, options?: ViewOptions): Promise<FileViewResult> {
    return this.client.view(this.id, path, options);
  }

  strReplace(
    path: string,
    oldStr: string,
    newStr: string,
    options?: StrReplaceOptions,
  ): Promise<StrReplaceResult> {
    return this.client.strReplace(this.id, path, oldStr, newStr, options);
  }

  ls(options?: LsOptions): Promise<LsResult> {
    return this.client.ls(this.id, options);
  }

  glob(pattern: string, options?: GlobOptions): Promise<GlobResult> {
    return this.client.glob(this.id, pattern, options);
  }

  grep(pattern: string, options?: GrepOptions): Promise<GrepResult> {
    return this.client.grep(this.id, pattern, options);
  }

  getLog(options?: GetLogOptions): Promise<GetLogResult> {
    return this.client.getLog(this.id, options);
  }

  watch(): AsyncGenerator<LogEntry> {
    return this.client.watch(this.id);
  }

  startProcess(command: string, options?: StartProcessOptions): Promise<ProcessStartResult> {
    return this.client.startProcess(this.id, command, options);
  }

  listProcesses(): Promise<ProcessListResult> {
    return this.client.listProcesses(this.id);
  }

  getProcessOutput(
    processId: string,
    options?: GetProcessOutputOptions,
  ): Promise<ProcessOutputResult> {
    return this.client.getProcessOutput(this.id, processId, options);
  }

  sendProcessInput(processId: string, data: string): Promise<ProcessInputResult> {
    return this.client.sendProcessInput(this.id, processId, data);
  }

  stopProcess(processId: string): Promise<ProcessStopResult> {
    return this.client.stopProcess(this.id, processId);
  }

  takeover(): Promise<WebSocket> {
    return this.client.takeover(this.id);
  }

  desktopTakeover(): Promise<WebSocket> {
    return this.client.desktopTakeover(this.id);
  }

  createPreviewUrl(port: number, options?: CreatePreviewUrlOptions): Promise<PreviewUrl> {
    return this.client.createPreviewUrl(this.id, port, options);
  }

  revokePreviewUrl(port: number, tokenId: string): Promise<PreviewRevokeResult> {
    return this.client.revokePreviewUrl(this.id, port, tokenId);
  }
}
