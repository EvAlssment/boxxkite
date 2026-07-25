//! Rust client for a **hosted** boxkite control-plane -- create sandboxes,
//! run commands, edit files, stream the audit log, take over a session's
//! shell, over HTTP/WebSocket. Not the `boxkite` package itself (the
//! self-hosted Python core that embeds `SandboxManager` against your own
//! Kubernetes cluster) -- use this to talk to *someone else's* running
//! control-plane, hosted or self-hosted, over its `/v1/*` REST API. See
//! `docs/API.md` in the [boxkite repository](https://github.com/EvAlssment/boxkite)
//! for the full route/schema reference this crate wraps.
//!
//! # Quickstart
//!
//! ```no_run
//! use boxkite_client::{Client, CreateSandboxOptions, ExecOptions};
//!
//! # async fn example() -> Result<(), boxkite_client::BoxkiteError> {
//! let client = Client::new("https://your-control-plane.example.com", "bxk_live_...")?;
//!
//! let sandbox = client
//!     .create_sandbox(CreateSandboxOptions::new().label("demo"))
//!     .await?;
//!
//! let result = client.exec(&sandbox.id, "python3 -c 'print(1 + 1)'", ExecOptions::new()).await?;
//! println!("{}", result.stdout); // "2\n"
//!
//! client.destroy_sandbox(&sandbox.id).await?;
//! # Ok(())
//! # }
//! ```
//!
//! # Error handling
//!
//! Every fallible call returns `Result<T, BoxkiteError>`. A non-2xx
//! response becomes [`BoxkiteError::Api`] (`.status()`/`.code()`); a
//! transport-level failure becomes [`BoxkiteError::Connection`]. See
//! [`BoxkiteError`] for the full set of variants.
//!
//! # Scope
//!
//! This crate wraps the same `/v1/*` API surface `sdk-python`/`sdk-js`/
//! `sdk-go` wrap: sandbox lifecycle, exec/file operations, the
//! `http_request` secrets-broker proxy, background processes
//! (`start_process`/`list_processes`/`get_process_output`/
//! `send_process_input`/`stop_process`), the audit log (`get_log`/`watch`),
//! human/desktop takeover, network-ingress preview URLs
//! (`create_preview_url`/`revoke_preview_url`), agent-invokable LSP
//! completions (`lsp_start`/`lsp_open`/`lsp_completion`/`lsp_stop`),
//! account/usage introspection, dashboard auth-flow helpers (password
//! reset/verify/resend/refresh/logout), the per-account command allowlist
//! (`get_allowed_commands`/`set_allowed_commands`/`clear_allowed_commands`),
//! the [`SandboxSession`]/[`Client::with_sandbox`] convenience helper, and
//! CRUD for images/volumes/outbound-MCP connections/webhooks/secrets.
//!
//! Deliberately **not** included (see `sdk-rust/README.md`'s "Scope"
//! section for the full disclosure): a LangChain-style tool-factory wrapper
//! (no dominant Rust agent-framework tool-spec convention exists yet to
//! mirror), a synchronous/blocking client variant (this crate is
//! async-first via `tokio`, matching Rust's ecosystem convention), and the
//! control-plane's filesystem-snapshot routes (a real endpoint, but not
//! wrapped by `sdk-python`/`sdk-js`/`sdk-go` either as of this writing).
//!
//! # Retries
//!
//! Automatic retry with exponential backoff + jitter is opt-in via
//! [`ClientBuilder::max_retries`]/[`ClientBuilder::retry`]; see
//! [`RetryConfig`] for exactly which failures are retried (and which never
//! are). Off by default.

mod account;
mod audit;
mod client;
mod desktop;
mod error;
mod files;
mod images;
mod lsp;
mod mcp_connections;
mod preview;
mod processes;
mod sandboxes;
mod secrets;
mod session;
mod takeover;
mod volumes;
mod webhooks;

pub use account::{
    Account, AllowedCommandRule, AllowedCommandsResponse, MessageResponse, TokenPair,
};
pub use audit::{AuditLogEntry, AuditLogResponse, GetLogOptions};
pub use client::{Client, ClientBuilder, RetryConfig};
pub use desktop::DesktopStream;
pub use error::BoxkiteError;
pub use files::{
    ExecOptions, ExecResult, FileCreateResult, FileOptions, GlobOptions, GlobResult, GrepOptions,
    GrepResult, HttpRequestOptions, HttpRequestResult, LsOptions, LsResult, StrReplaceOptions,
    StrReplaceResult, ViewOptions, ViewResult,
};
pub use images::{CreateImageOptions, Image, ImageBase};
pub use lsp::{LspCompletionResult, LspOpenResult, LspStartResult, LspStopResult};
pub use mcp_connections::{McpCatalogId, McpConnection};
pub use preview::{PreviewRevokeResult, PreviewUrl};
pub use processes::{
    ProcessInfo, ProcessInputResult, ProcessListResult, ProcessOutputResult, ProcessStartResult,
    ProcessStopResult, ProcessStreamEvent, StartProcessOptions,
};
pub use sandboxes::{CreateSandboxOptions, Sandbox, SandboxConnectInfo, SandboxSize, UsageSummary};
pub use secrets::{CreateSecretOptions, Secret};
pub use session::SandboxSession;
pub use takeover::TakeoverStream;
pub use volumes::{CreateVolumeOptions, Volume};
pub use webhooks::{
    CreateWebhookOptions, ListWebhookDeliveriesOptions, Webhook, WebhookDelivery, WebhookEventType,
};
