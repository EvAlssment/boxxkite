/**
 * Response shapes for the boxkite control-plane v1 HTTP API. Field names
 * mirror the JSON the control-plane emits (snake_case), matching sdk-go's
 * structs and sdk-python's return values. Nullable fields use `T | null`
 * (the JSON is `null`, not absent); genuinely optional fields (present on
 * some responses only) use `?`.
 */

import type { AllowedCommandRule, WebhookEventType } from "./client.js";

/** GET /v1/account -- identity for the API key in use. */
export interface Account {
  id: string;
  email: string;
  created_at: string;
}

/** GET /v1/usage -- current usage against fair-use limits. Also returned
 * inline on a created sandbox's `usage` field. */
export interface Usage {
  monthly_sandbox_hours_used: number;
  monthly_sandbox_hours_limit: number;
  concurrent_sandboxes: number;
  concurrent_sandboxes_limit: number;
}

/** Generic acknowledgement body (password-reset request/confirm, email
 * verification, etc.). */
export interface MessageResponse {
  message: string;
}

/** POST /v1/auth/refresh -- a fresh access/refresh token pair plus the
 * account identity. */
export interface TokenPair {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string | null;
  account: Account;
}

/** Opaque connection metadata for operators with cluster access -- external
 * callers operate on a session through the exec/files/* routes, never this. */
export interface SandboxConnectInfo {
  pod_name: string;
  note: string;
}

/** One sandbox session (GET/POST /v1/sandboxes). */
export interface Sandbox {
  id: string;
  status: string;
  label: string | null;
  created_at: string;
  destroyed_at: string | null;
  expires_at: string | null;
  connect?: SandboxConnectInfo;
  usage?: Usage;
}

/** POST /v1/sandboxes/{id}/exec. */
export interface ExecResult {
  exit_code: number;
  stdout: string;
  stderr: string;
}

/** POST /v1/sandboxes/{id}/http-request (secrets-broker HTTP request). */
export interface HttpRequestResult {
  status_code: number;
  headers: Record<string, string>;
  body: string;
  truncated: boolean;
}

/** POST /v1/sandboxes/{id}/files. */
export interface FileCreateResult {
  path: string;
  size: number;
  created: boolean;
}

/** One entry returned by View (directory path), Ls, or Glob. */
export interface DirEntry {
  path: string;
  is_dir: boolean;
  size: number;
}

/** POST /v1/sandboxes/{id}/files/view. `entries` is populated (content/lines
 * left zero) when the target path is a directory. */
export interface FileViewResult {
  content: string;
  lines: number;
  is_directory: boolean;
  entries: DirEntry[];
}

/** POST /v1/sandboxes/{id}/files/str-replace. */
export interface StrReplaceResult {
  path: string;
  replaced: boolean;
  occurrences: number;
}

/** POST /v1/sandboxes/{id}/files/ls. */
export interface LsResult {
  entries: DirEntry[];
}

/** POST /v1/sandboxes/{id}/files/glob. */
export interface GlobResult {
  matches: DirEntry[];
}

/** One match returned by Grep. */
export interface GrepMatch {
  path: string;
  line: number;
  text: string;
}

/** POST /v1/sandboxes/{id}/files/grep. */
export interface GrepResult {
  matches: GrepMatch[];
  error: string | null;
  truncated: boolean;
}

/** One row of a session's exec/file-operation audit trail
 * (GET /v1/sandboxes/{id}/log, streamed by watch()). `detail` is an
 * operation-specific payload with no fixed shape. */
export interface LogEntry {
  id: string;
  session_id: string;
  source: string;
  operation: string;
  detail: unknown;
  exit_code: number | null;
  output_truncated: string;
  started_at: string;
  duration_ms: number | null;
  row_hash: string | null;
  prev_hash: string | null;
}

/** GET /v1/sandboxes/{id}/log. */
export interface GetLogResult {
  entries: LogEntry[];
  limit: number;
  offset: number;
  total: number;
}

/** POST /v1/sandboxes/{id}/processes. */
export interface ProcessStartResult {
  process_id: string;
  status: string;
  started_at: string;
}

/** One tracked background process (GET /v1/sandboxes/{id}/processes). */
export interface ProcessInfo {
  process_id: string;
  command: string;
  description: string | null;
  status: string;
  started_at: string;
  exit_code: number | null;
}

/** GET /v1/sandboxes/{id}/processes. */
export interface ProcessListResult {
  processes: ProcessInfo[];
}

/** GET /v1/sandboxes/{id}/processes/{processId}/output. */
export interface ProcessOutputResult {
  status: string;
  stdout_chunk: string;
  next_offset: number;
  truncated: boolean;
  exit_code: number | null;
}

/** One event from GET /v1/sandboxes/{id}/processes/{processId}/stream: an
 * `output` per new stdout chunk, then a terminal `exit`. */
export type ProcessStreamEvent =
  | { type: "output"; stdout_chunk: string; next_offset: number; truncated: boolean }
  | { type: "exit"; status: string; exit_code: number | null };

/** POST /v1/sandboxes/{id}/processes/{processId}/input. */
export interface ProcessInputResult {
  bytes_written: number;
}

/** POST /v1/sandboxes/{id}/processes/{processId}/stop. */
export interface ProcessStopResult {
  status: string;
  exit_code: number | null;
}

/** POST /v1/sandboxes/{id}/preview/{port}. */
export interface PreviewUrl {
  url: string;
  expires_at: string;
  token_id: string;
}

/** POST /v1/sandboxes/{id}/preview/{port}/revoke. */
export interface PreviewRevokeResult {
  revoked: boolean;
  token_id: string;
}

/** A custom sandbox image (POST/GET /v1/images). */
export interface Image {
  id: string;
  label: string | null;
  base: string;
  python_packages: string[];
  apt_packages: string[];
  npm_packages: string[];
  status: string;
  digest: string | null;
  registry_ref: string | null;
  scan_result: Record<string, unknown> | null;
  failure_reason: string | null;
  created_at: string;
  completed_at: string | null;
}

/** An independent, PVC-backed storage volume (POST/GET /v1/volumes). */
export interface Volume {
  id: string;
  label: string | null;
  size_gb: number;
  status: string;
  pvc_name: string | null;
  failure_reason: string | null;
  created_at: string;
}

/** A webhook subscription (POST/GET /v1/webhooks). `secret` is populated
 * only on the createWebhook() response -- never returned by any other
 * route. */
export interface Webhook {
  id: string;
  url: string;
  event_types: WebhookEventType[];
  description: string | null;
  is_active: boolean;
  created_at: string;
  last_triggered_at: string | null;
  secret?: string;
}

/** One delivery attempt for a webhook subscription
 * (GET /v1/webhooks/{id}/deliveries). */
export interface WebhookDelivery {
  id: string;
  event_type: string;
  status: string;
  attempt_count: number;
  next_attempt_at: string;
  last_attempt_at: string | null;
  response_status_code: number | null;
  failure_reason: string | null;
  created_at: string;
  delivered_at: string | null;
}

/** An outbound-MCP connection grant (POST/GET /v1/mcp-connections). */
export interface McpConnection {
  id: string;
  label: string;
  catalog_id: string;
  host: string;
  created_at: string;
  last_used_at: string | null;
}

/** A secret's metadata (POST/GET /v1/secrets). The raw value is never
 * returned here or anywhere else after creation. */
export interface Secret {
  id: string;
  name: string;
  allowed_hosts: string[];
  trust_tier: string | null;
  created_at: string;
  last_used_at: string | null;
}

/** GET/PUT /v1/account/allowed-commands. */
export interface AllowedCommandsResponse {
  rules: AllowedCommandRule[];
}
