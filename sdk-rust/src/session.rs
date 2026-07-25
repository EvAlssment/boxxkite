//! [`SandboxSession`]: a thin, session-scoped view over [`Client`] whose
//! every method forwards to the identically-named `Client` method with this
//! session's id bound in, plus [`Client::with_sandbox`], the
//! create-on-enter/destroy-on-exit convenience. Mirrors `sdk-python`'s
//! `SandboxSession`/`client.sandbox()` context manager, `sdk-js`'s
//! `SandboxSession`/`withSandbox`, and `sdk-go`'s `Session`/`WithSandbox`.

use std::future::Future;
use std::pin::Pin;

use futures_util::Stream;

use crate::audit::{AuditLogEntry, AuditLogResponse, GetLogOptions};
use crate::client::Client;
use crate::desktop::DesktopStream;
use crate::error::BoxkiteError;
use crate::files::{
    ExecOptions, ExecResult, FileCreateResult, FileOptions, GlobOptions, GlobResult, GrepOptions,
    GrepResult, HttpRequestOptions, HttpRequestResult, LsOptions, LsResult, StrReplaceOptions,
    StrReplaceResult, ViewOptions, ViewResult,
};
use crate::lsp::{LspCompletionResult, LspOpenResult, LspStartResult, LspStopResult};
use crate::preview::{PreviewRevokeResult, PreviewUrl};
use crate::processes::{
    ProcessInputResult, ProcessListResult, ProcessOutputResult, ProcessStartResult,
    ProcessStopResult, StartProcessOptions,
};
use crate::sandboxes::CreateSandboxOptions;
use crate::takeover::TakeoverStream;

/// A [`Client`] bound to one sandbox session id -- obtained from
/// [`Client::with_sandbox`]. Holds a cheap clone of the underlying `Client`
/// (see [`Client`]'s own note on cloning), so it is `Clone` and can outlive
/// the borrow that produced it.
#[derive(Clone, Debug)]
pub struct SandboxSession {
    client: Client,
    id: String,
}

impl SandboxSession {
    /// The bound sandbox session id.
    pub fn id(&self) -> &str {
        &self.id
    }

    /// The underlying [`Client`], for the handful of calls not scoped to a
    /// single session (e.g. [`Client::list_sandboxes`]).
    pub fn client(&self) -> &Client {
        &self.client
    }

    pub async fn exec(
        &self,
        command: &str,
        options: ExecOptions,
    ) -> Result<ExecResult, BoxkiteError> {
        self.client.exec(&self.id, command, options).await
    }

    pub async fn http_request(
        &self,
        method: &str,
        url: &str,
        options: HttpRequestOptions,
    ) -> Result<HttpRequestResult, BoxkiteError> {
        self.client
            .http_request(&self.id, method, url, options)
            .await
    }

    pub async fn file_create(
        &self,
        path: &str,
        content: &str,
        options: FileOptions,
    ) -> Result<FileCreateResult, BoxkiteError> {
        self.client
            .file_create(&self.id, path, content, options)
            .await
    }

    pub async fn view(&self, path: &str, options: ViewOptions) -> Result<ViewResult, BoxkiteError> {
        self.client.view(&self.id, path, options).await
    }

    pub async fn str_replace(
        &self,
        path: &str,
        old_str: &str,
        new_str: &str,
        options: StrReplaceOptions,
    ) -> Result<StrReplaceResult, BoxkiteError> {
        self.client
            .str_replace(&self.id, path, old_str, new_str, options)
            .await
    }

    pub async fn ls(&self, options: LsOptions) -> Result<LsResult, BoxkiteError> {
        self.client.ls(&self.id, options).await
    }

    pub async fn glob(
        &self,
        pattern: &str,
        options: GlobOptions,
    ) -> Result<GlobResult, BoxkiteError> {
        self.client.glob(&self.id, pattern, options).await
    }

    pub async fn grep(
        &self,
        pattern: &str,
        options: GrepOptions,
    ) -> Result<GrepResult, BoxkiteError> {
        self.client.grep(&self.id, pattern, options).await
    }

    pub async fn get_log(&self, options: GetLogOptions) -> Result<AuditLogResponse, BoxkiteError> {
        self.client.get_log(&self.id, options).await
    }

    pub fn watch(
        &self,
    ) -> Pin<Box<dyn Stream<Item = Result<AuditLogEntry, BoxkiteError>> + Send + 'static>> {
        self.client.watch(&self.id)
    }

    pub async fn start_process(
        &self,
        command: &str,
        options: StartProcessOptions,
    ) -> Result<ProcessStartResult, BoxkiteError> {
        self.client.start_process(&self.id, command, options).await
    }

    pub async fn list_processes(&self) -> Result<ProcessListResult, BoxkiteError> {
        self.client.list_processes(&self.id).await
    }

    pub async fn get_process_output(
        &self,
        process_id: &str,
        since_offset: i64,
    ) -> Result<ProcessOutputResult, BoxkiteError> {
        self.client
            .get_process_output(&self.id, process_id, since_offset)
            .await
    }

    pub async fn send_process_input(
        &self,
        process_id: &str,
        data: &str,
    ) -> Result<ProcessInputResult, BoxkiteError> {
        self.client
            .send_process_input(&self.id, process_id, data)
            .await
    }

    pub async fn stop_process(&self, process_id: &str) -> Result<ProcessStopResult, BoxkiteError> {
        self.client.stop_process(&self.id, process_id).await
    }

    pub async fn takeover(&self) -> Result<TakeoverStream, BoxkiteError> {
        self.client.takeover(&self.id).await
    }

    pub async fn desktop_takeover(&self) -> Result<DesktopStream, BoxkiteError> {
        self.client.desktop_takeover(&self.id).await
    }

    pub async fn create_preview_url(
        &self,
        port: u16,
        ttl_seconds: Option<u32>,
    ) -> Result<PreviewUrl, BoxkiteError> {
        self.client
            .create_preview_url(&self.id, port, ttl_seconds)
            .await
    }

    pub async fn revoke_preview_url(
        &self,
        port: u16,
        token_id: &str,
    ) -> Result<PreviewRevokeResult, BoxkiteError> {
        self.client
            .revoke_preview_url(&self.id, port, token_id)
            .await
    }

    pub async fn lsp_start(&self, language: &str) -> Result<LspStartResult, BoxkiteError> {
        self.client.lsp_start(&self.id, language).await
    }

    pub async fn lsp_open(
        &self,
        lsp_id: &str,
        path: &str,
        content: &str,
    ) -> Result<LspOpenResult, BoxkiteError> {
        self.client.lsp_open(&self.id, lsp_id, path, content).await
    }

    pub async fn lsp_completion(
        &self,
        lsp_id: &str,
        path: &str,
        line: u32,
        character: u32,
    ) -> Result<LspCompletionResult, BoxkiteError> {
        self.client
            .lsp_completion(&self.id, lsp_id, path, line, character)
            .await
    }

    pub async fn lsp_stop(&self, lsp_id: &str) -> Result<LspStopResult, BoxkiteError> {
        self.client.lsp_stop(&self.id, lsp_id).await
    }
}

impl Client {
    /// Create a sandbox, run `f` against a [`SandboxSession`] bound to it, and
    /// tear the sandbox down when `f` returns -- even on error. Teardown is
    /// best-effort (an already-gone session doesn't turn a successful `f` into
    /// an error), mirroring every sibling SDK's cleanup semantics.
    ///
    /// ```no_run
    /// use boxkite_client::{Client, CreateSandboxOptions, ExecOptions};
    ///
    /// # async fn example() -> Result<(), boxkite_client::BoxkiteError> {
    /// let client = Client::new("https://cp.example.com", "bxk_live_...")?;
    /// let stdout = client
    ///     .with_sandbox(CreateSandboxOptions::new().label("demo"), |sb| async move {
    ///         let result = sb.exec("echo hi", ExecOptions::new()).await?;
    ///         Ok(result.stdout)
    ///     })
    ///     .await?;
    /// assert_eq!(stdout, "hi\n");
    /// # Ok(())
    /// # }
    /// ```
    pub async fn with_sandbox<F, Fut, T>(
        &self,
        options: CreateSandboxOptions,
        f: F,
    ) -> Result<T, BoxkiteError>
    where
        F: FnOnce(SandboxSession) -> Fut,
        Fut: Future<Output = Result<T, BoxkiteError>>,
    {
        let sandbox = self.create_sandbox(options).await?;
        let session = SandboxSession {
            client: self.clone(),
            id: sandbox.id.clone(),
        };
        let result = f(session).await;
        // Best-effort teardown: don't let a cleanup failure mask f's result.
        let _ = self.destroy_sandbox(&sandbox.id).await;
        result
    }
}
