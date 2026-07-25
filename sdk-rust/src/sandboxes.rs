//! Sandbox lifecycle: `POST/GET/DELETE /v1/sandboxes*`. Mirrors
//! `sdk-python`'s `create_sandbox`/`get_sandbox`/`list_sandboxes`/
//! `destroy_sandbox`.

use std::collections::HashMap;

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// CPU/memory size preset for a sandbox. Capped per-account by the
/// deployment's `BOXKITE_MAX_SANDBOX_SIZE` -- requesting a larger size than
/// the account is allowed returns `429`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SandboxSize {
    Small,
    Medium,
    Large,
}

/// Builder for `POST /v1/sandboxes`'s optional fields. All fields are
/// unset (server defaults apply) unless set via the chained setters.
///
/// ```no_run
/// use boxkite_client::CreateSandboxOptions;
///
/// let options = CreateSandboxOptions::new()
///     .label("demo")
///     .storage_gb(20.0)
///     .lifetime_minutes(120);
/// ```
#[derive(Debug, Clone, Default, Serialize)]
pub struct CreateSandboxOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    size: Option<SandboxSize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    storage_gb: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    lifetime_minutes: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    count: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    secret_names: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    image_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mcp_connection_names: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    volume_mounts: Option<HashMap<String, String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    gpu_count: Option<i64>,
}

impl CreateSandboxOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Optional human-readable label for the sandbox.
    pub fn label(mut self, label: impl Into<String>) -> Self {
        self.label = Some(label.into());
        self
    }

    /// CPU/memory size preset. Defaults to `Small` server-side when omitted.
    pub fn size(mut self, size: SandboxSize) -> Self {
        self.size = Some(size);
        self
    }

    /// Requested persistent storage in GB.
    pub fn storage_gb(mut self, storage_gb: f64) -> Self {
        self.storage_gb = Some(storage_gb);
        self
    }

    /// Maximum lifetime of the sandbox in minutes before automatic teardown.
    pub fn lifetime_minutes(mut self, lifetime_minutes: i64) -> Self {
        self.lifetime_minutes = Some(lifetime_minutes);
        self
    }

    /// Number of sandboxes to create in this request (1-10). Use
    /// [`Client::create_sandbox_batch`], not [`Client::create_sandbox`], to
    /// create more than one -- the server returns a JSON array only when
    /// `count` is greater than 1.
    pub fn count(mut self, count: u32) -> Self {
        self.count = Some(count);
        self
    }

    /// Names of this account's secrets (see `docs/SECRETS-DESIGN.md`) this
    /// session should be granted access to via the sidecar's secrets-broker
    /// `http_request` tool. A name that doesn't exist for this account 404s
    /// before any sandbox is created.
    pub fn secret_names<I, S>(mut self, secret_names: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.secret_names = Some(secret_names.into_iter().map(Into::into).collect());
        self
    }

    /// Id of a completed custom image built via [`Client::create_image`].
    /// 404s if not owned by this account or not yet status `"completed"`.
    pub fn image_id(mut self, image_id: impl Into<String>) -> Self {
        self.image_id = Some(image_id.into());
        self
    }

    /// Labels of this account's outbound-MCP connections (see
    /// [`Client::create_mcp_connection`]) this session should be granted
    /// network egress to.
    pub fn mcp_connection_names<I, S>(mut self, names: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.mcp_connection_names = Some(names.into_iter().map(Into::into).collect());
        self
    }

    /// `{volume_id: mount_path}` mapping of independent PVC-backed volumes
    /// (see [`Client::create_volume`]) to mount into this sandbox.
    pub fn volume_mounts(mut self, volume_mounts: HashMap<String, String>) -> Self {
        self.volume_mounts = Some(volume_mounts);
        self
    }

    /// Opt-in, experimental (`docs/GPU-SUPPORT-SCOPING.md`) -- requests this
    /// many GPUs as a Kubernetes extended-resource limit on the sandbox
    /// container. 422s (`gpu_support_disabled`) unless the deployment has
    /// `BOXKITE_GPU_ENABLED` set and a GPU-equipped node pool with a device
    /// plugin provisioned; not verified against real GPU hardware in this
    /// codebase. Bounded by `BOXKITE_MAX_GPU_COUNT_PER_SESSION`
    /// (422 `invalid_gpu_count` otherwise).
    pub fn gpu_count(mut self, gpu_count: i64) -> Self {
        self.gpu_count = Some(gpu_count);
        self
    }
}

/// How to reach the sandbox session -- an opaque handle for operators with
/// cluster access; external callers operate on the session through this
/// client's own `exec`/file-op methods instead (see `docs/API.md`).
#[derive(Debug, Clone, Deserialize)]
pub struct SandboxConnectInfo {
    pub pod_name: Option<String>,
    pub note: String,
}

/// Usage against this account's fair-use limits, included on
/// [`Client::create_sandbox`]'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct UsageSummary {
    pub monthly_sandbox_hours_used: f64,
    pub monthly_sandbox_hours_limit: f64,
    pub concurrent_sandboxes: i64,
    pub concurrent_sandboxes_limit: i64,
}

/// A sandbox session (`SandboxSessionOut`/`SandboxCreatedResponse`).
/// `usage` is only present on [`Client::create_sandbox`]'s response, not on
/// [`Client::get_sandbox`]/[`Client::list_sandboxes`].
#[derive(Debug, Clone, Deserialize)]
pub struct Sandbox {
    pub id: String,
    /// `"active"` or `"destroyed"`.
    pub status: String,
    pub label: Option<String>,
    pub created_at: String,
    pub destroyed_at: Option<String>,
    pub expires_at: Option<String>,
    pub connect: Option<SandboxConnectInfo>,
    pub usage: Option<UsageSummary>,
}

impl Client {
    /// `POST /v1/sandboxes` -- create a single sandbox. Use
    /// [`Client::create_sandbox_batch`] instead if `options.count(n)` with
    /// `n > 1` -- the server responds with a JSON array in that case, which
    /// won't deserialize as a single [`Sandbox`].
    pub async fn create_sandbox(
        &self,
        options: CreateSandboxOptions,
    ) -> Result<Sandbox, BoxkiteError> {
        let builder = self.request(Method::POST, "/v1/sandboxes").json(&options);
        self.send(builder).await
    }

    /// `POST /v1/sandboxes` with `options.count(n)`, `n > 1` -- creates a
    /// batch of sandboxes in one call and returns all of them. Each session
    /// in the batch is created and limit-checked one at a time, so a later
    /// item can still fail the concurrent-sandbox or monthly-usage cap even
    /// if earlier items succeeded (surfaces as a `BoxkiteError::Api` for the
    /// whole call, per the underlying REST contract).
    pub async fn create_sandbox_batch(
        &self,
        options: CreateSandboxOptions,
    ) -> Result<Vec<Sandbox>, BoxkiteError> {
        let builder = self.request(Method::POST, "/v1/sandboxes").json(&options);
        self.send(builder).await
    }

    /// `GET /v1/sandboxes/{session_id}` -- fetch one sandbox session by id.
    /// Resolves destroyed sessions too (a lookup, not an operational route
    /// that requires a live pod).
    pub async fn get_sandbox(&self, session_id: &str) -> Result<Sandbox, BoxkiteError> {
        let builder = self.request(Method::GET, &format!("/v1/sandboxes/{session_id}"));
        self.send(builder).await
    }

    /// `GET /v1/sandboxes` -- list sandbox sessions owned by this account.
    pub async fn list_sandboxes(&self, active_only: bool) -> Result<Vec<Sandbox>, BoxkiteError> {
        let builder = self
            .request(Method::GET, "/v1/sandboxes")
            .query(&[("active_only", active_only.to_string())]);
        self.send_or_default(builder).await
    }

    /// `DELETE /v1/sandboxes/{session_id}` -- tear down a sandbox session.
    pub async fn destroy_sandbox(&self, session_id: &str) -> Result<(), BoxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/sandboxes/{session_id}"));
        self.send_no_content(builder).await
    }
}
