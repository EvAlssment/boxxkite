//! `exec` and the file-op tool surface: `file_create`/`view`/`str_replace`/
//! `ls`/`glob`/`grep`. Mirrors `sdk-python`'s methods of the same names,
//! field-for-field against `control_plane.schemas`'s `Sandbox*Request`/
//! `Sandbox*Response` models.

use std::collections::HashMap;
use std::time::Duration;

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::{Client, EXEC_TIMEOUT_HEADROOM};
use crate::error::BoxkiteError;

/// Optional `exec` parameters.
#[derive(Debug, Clone, Default)]
pub struct ExecOptions {
    timeout: Option<u32>,
    description: Option<String>,
}

impl ExecOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Command timeout in seconds (1-100 server-side, default 30).
    pub fn timeout(mut self, timeout: u32) -> Self {
        self.timeout = Some(timeout);
        self
    }

    pub fn description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }
}

#[derive(Serialize)]
struct ExecBody<'a> {
    command: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    timeout: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<&'a str>,
}

/// `POST /v1/sandboxes/{id}/exec`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ExecResult {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Optional `http_request` parameters.
#[derive(Debug, Clone, Default)]
pub struct HttpRequestOptions {
    headers: Option<HashMap<String, String>>,
    body: Option<String>,
    timeout: Option<u32>,
}

impl HttpRequestOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Request headers. A value may contain a literal `{{secret:name}}`
    /// reference for a secret granted to this session via
    /// [`CreateSandboxOptions::secret_names`](crate::CreateSandboxOptions::secret_names);
    /// the sidecar substitutes the real value in-process -- this SDK never
    /// sees it.
    pub fn headers(mut self, headers: HashMap<String, String>) -> Self {
        self.headers = Some(headers);
        self
    }

    /// Request body. May contain `{{secret:name}}` references, same as
    /// [`HttpRequestOptions::headers`].
    pub fn body(mut self, body: impl Into<String>) -> Self {
        self.body = Some(body.into());
        self
    }

    /// Request timeout in seconds.
    pub fn timeout(mut self, timeout: u32) -> Self {
        self.timeout = Some(timeout);
        self
    }
}

#[derive(Serialize)]
struct HttpRequestBody<'a> {
    method: &'a str,
    url: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    headers: Option<&'a HashMap<String, String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    body: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    timeout: Option<u32>,
}

/// `POST /v1/sandboxes/{id}/http-request`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct HttpRequestResult {
    pub status_code: i32,
    #[serde(default)]
    pub headers: HashMap<String, String>,
    pub body: String,
    #[serde(default)]
    pub truncated: bool,
}

/// A single optional `description` field shared by every file-op request --
/// a caller-supplied annotation surfaced later in `get_log`'s audit trail.
#[derive(Debug, Clone, Default)]
pub struct FileOptions {
    description: Option<String>,
}

impl FileOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }
}

#[derive(Serialize)]
struct FileCreateBody<'a> {
    path: &'a str,
    content: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<&'a str>,
}

/// `POST /v1/sandboxes/{id}/files`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct FileCreateResult {
    pub path: String,
    pub size: i64,
    pub created: bool,
}

/// Optional `view` parameters.
#[derive(Debug, Clone, Default)]
pub struct ViewOptions {
    view_range: Option<[i64; 2]>,
    description: Option<String>,
}

impl ViewOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// `[start_line, end_line]`, 1-indexed.
    pub fn view_range(mut self, start_line: i64, end_line: i64) -> Self {
        self.view_range = Some([start_line, end_line]);
        self
    }

    pub fn description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }
}

#[derive(Serialize)]
struct ViewBody<'a> {
    path: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    view_range: Option<[i64; 2]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<&'a str>,
}

/// `POST /v1/sandboxes/{id}/files/view`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ViewResult {
    pub content: String,
    pub lines: i64,
    #[serde(default)]
    pub is_directory: bool,
    pub entries: Option<Vec<String>>,
}

/// Optional `str_replace` parameters.
#[derive(Debug, Clone, Default)]
pub struct StrReplaceOptions {
    replace_all: bool,
    description: Option<String>,
}

impl StrReplaceOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replace every occurrence of `old_str` instead of requiring exactly
    /// one match.
    pub fn replace_all(mut self, replace_all: bool) -> Self {
        self.replace_all = replace_all;
        self
    }

    pub fn description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }
}

#[derive(Serialize)]
struct StrReplaceBody<'a> {
    path: &'a str,
    old_str: &'a str,
    new_str: &'a str,
    replace_all: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<&'a str>,
}

/// `POST /v1/sandboxes/{id}/files/str-replace`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct StrReplaceResult {
    pub path: String,
    pub replaced: bool,
    pub occurrences: i64,
}

/// Optional `ls` parameters.
#[derive(Debug, Clone, Default)]
pub struct LsOptions {
    path: Option<String>,
}

impl LsOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Directory to list. Defaults to `"/"` server-side when omitted.
    pub fn path(mut self, path: impl Into<String>) -> Self {
        self.path = Some(path.into());
        self
    }
}

#[derive(Serialize)]
struct LsBody {
    path: String,
}

/// `POST /v1/sandboxes/{id}/files/ls`'s response. Each entry's shape isn't
/// pinned by the API contract (`list[dict]` server-side), so entries are
/// left as raw JSON rather than an over-fitted struct.
#[derive(Debug, Clone, Deserialize)]
pub struct LsResult {
    pub entries: Vec<serde_json::Value>,
}

/// Optional `glob` parameters.
#[derive(Debug, Clone, Default)]
pub struct GlobOptions {
    path: Option<String>,
}

impl GlobOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Directory to search under. Defaults to `"/"` server-side when omitted.
    pub fn path(mut self, path: impl Into<String>) -> Self {
        self.path = Some(path.into());
        self
    }
}

#[derive(Serialize)]
struct GlobBody<'a> {
    pattern: &'a str,
    path: String,
}

/// `POST /v1/sandboxes/{id}/files/glob`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct GlobResult {
    pub matches: Vec<serde_json::Value>,
}

/// Optional `grep` parameters.
#[derive(Debug, Clone, Default)]
pub struct GrepOptions {
    path: Option<String>,
    glob: Option<String>,
    max_matches: Option<u32>,
}

impl GrepOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Directory to search under. Defaults to `"/"` server-side when omitted.
    pub fn path(mut self, path: impl Into<String>) -> Self {
        self.path = Some(path.into());
        self
    }

    /// Restrict which files are searched by this glob.
    pub fn glob(mut self, glob: impl Into<String>) -> Self {
        self.glob = Some(glob.into());
        self
    }

    /// Maximum number of matches to return (default 500 server-side).
    pub fn max_matches(mut self, max_matches: u32) -> Self {
        self.max_matches = Some(max_matches);
        self
    }
}

#[derive(Serialize)]
struct GrepBody<'a> {
    pattern: &'a str,
    path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    glob: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_matches: Option<u32>,
}

/// `POST /v1/sandboxes/{id}/files/grep`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct GrepResult {
    pub matches: Vec<serde_json::Value>,
    pub error: Option<String>,
    #[serde(default)]
    pub truncated: bool,
}

impl Client {
    /// `POST /v1/sandboxes/{id}/exec` -- run a shell command inside the
    /// session's sandbox and return its exit code, stdout, and stderr.
    /// Commands run synchronously; there is no streaming of partial output.
    pub async fn exec(
        &self,
        session_id: &str,
        command: &str,
        options: ExecOptions,
    ) -> Result<ExecResult, BoxkiteError> {
        let body = ExecBody {
            command,
            timeout: options.timeout,
            description: options.description.as_deref(),
        };
        let mut builder = self
            .request(Method::POST, &format!("/v1/sandboxes/{session_id}/exec"))
            .json(&body);
        if let Some(timeout) = options.timeout {
            builder = builder.timeout(Duration::from_secs(timeout as u64) + EXEC_TIMEOUT_HEADROOM);
        }
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/http-request` -- the secrets-broker HTTP
    /// request proxy (`docs/SECRETS-DESIGN.md`). `options.headers`/
    /// `options.body` may contain a literal `{{secret:name}}` reference for a
    /// secret granted to this session via
    /// [`CreateSandboxOptions::secret_names`](crate::CreateSandboxOptions::secret_names);
    /// the sidecar substitutes the real value in-process -- this SDK never
    /// sees the credential.
    pub async fn http_request(
        &self,
        session_id: &str,
        method: &str,
        url: &str,
        options: HttpRequestOptions,
    ) -> Result<HttpRequestResult, BoxkiteError> {
        let body = HttpRequestBody {
            method,
            url,
            headers: options.headers.as_ref(),
            body: options.body.as_deref(),
            timeout: options.timeout,
        };
        let mut builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/http-request"),
            )
            .json(&body);
        if let Some(timeout) = options.timeout {
            builder = builder.timeout(Duration::from_secs(timeout as u64) + EXEC_TIMEOUT_HEADROOM);
        }
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/files` -- create or overwrite a file in the
    /// session's sandbox workspace.
    pub async fn file_create(
        &self,
        session_id: &str,
        path: &str,
        content: &str,
        options: FileOptions,
    ) -> Result<FileCreateResult, BoxkiteError> {
        let body = FileCreateBody {
            path,
            content,
            description: options.description.as_deref(),
        };
        let builder = self
            .request(Method::POST, &format!("/v1/sandboxes/{session_id}/files"))
            .json(&body);
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/files/view` -- read a text file's contents
    /// (optionally a line range), or list a directory's entries.
    /// Binary/image files are not supported (text-only, mirroring the
    /// sidecar's own `/view` route).
    pub async fn view(
        &self,
        session_id: &str,
        path: &str,
        options: ViewOptions,
    ) -> Result<ViewResult, BoxkiteError> {
        let body = ViewBody {
            path,
            view_range: options.view_range,
            description: options.description.as_deref(),
        };
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/files/view"),
            )
            .json(&body);
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/files/str-replace` -- replace `old_str` with
    /// `new_str` in a file; `old_str` must appear exactly once unless
    /// `options.replace_all(true)` is set.
    pub async fn str_replace(
        &self,
        session_id: &str,
        path: &str,
        old_str: &str,
        new_str: &str,
        options: StrReplaceOptions,
    ) -> Result<StrReplaceResult, BoxkiteError> {
        let body = StrReplaceBody {
            path,
            old_str,
            new_str,
            replace_all: options.replace_all,
            description: options.description.as_deref(),
        };
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/files/str-replace"),
            )
            .json(&body);
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/files/ls` -- list a directory's direct children.
    pub async fn ls(&self, session_id: &str, options: LsOptions) -> Result<LsResult, BoxkiteError> {
        let body = LsBody {
            path: options.path.unwrap_or_else(|| "/".to_string()),
        };
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/files/ls"),
            )
            .json(&body);
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/files/glob` -- find files by name pattern
    /// (e.g. `"**/*.py"`).
    pub async fn glob(
        &self,
        session_id: &str,
        pattern: &str,
        options: GlobOptions,
    ) -> Result<GlobResult, BoxkiteError> {
        let body = GlobBody {
            pattern,
            path: options.path.unwrap_or_else(|| "/".to_string()),
        };
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/files/glob"),
            )
            .json(&body);
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{id}/files/grep` -- search file contents by regex.
    pub async fn grep(
        &self,
        session_id: &str,
        pattern: &str,
        options: GrepOptions,
    ) -> Result<GrepResult, BoxkiteError> {
        let body = GrepBody {
            pattern,
            path: options.path.unwrap_or_else(|| "/".to_string()),
            glob: options.glob.as_deref(),
            max_matches: options.max_matches,
        };
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/files/grep"),
            )
            .json(&body);
        self.send(builder).await
    }
}
