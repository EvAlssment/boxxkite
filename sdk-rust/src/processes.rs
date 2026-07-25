//! Background processes/sessions (`docs/PROCESS-SESSIONS-DESIGN.md`):
//! `POST /v1/sandboxes/{id}/processes` and its `/{process_id}/output`,
//! `/{process_id}/input`, `/{process_id}/stop` siblings, plus
//! `GET /v1/sandboxes/{id}/processes`. Mirrors `sdk-python`'s
//! `start_process`/`list_processes`/`get_process_output`/
//! `send_process_input`/`stop_process`.
//!
//! Distinct from [`crate::Client::exec`]: `exec` is one-shot request/
//! response bounded by its own timeout; these routes track a process across
//! multiple calls. Polling-style, not streaming -- the control plane's own
//! route comments mark SSE/streaming process output as a separate, later
//! phase, not part of this route set.

use std::pin::Pin;

use futures_util::{Stream, StreamExt};
use reqwest::Method;
use reqwest_eventsource::{Event, EventSource};
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// `max_runtime_seconds` is required by the server
/// (`SandboxProcessStartRequest`) with a 4h ceiling
/// (`SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING`) -- this is only this
/// crate's client-side default when the caller doesn't set one, matching
/// `sdk-python`/`sdk-js`/`sdk-go`'s own default of 3600. The ceiling itself
/// is enforced server-side and deliberately not duplicated here.
const DEFAULT_MAX_RUNTIME_SECONDS: i64 = 3600;

/// Builder for `POST /v1/sandboxes/{id}/processes`'s optional fields.
#[derive(Debug, Clone)]
pub struct StartProcessOptions {
    description: Option<String>,
    max_runtime_seconds: i64,
    expose_port: Option<i32>,
}

impl Default for StartProcessOptions {
    fn default() -> Self {
        Self {
            description: None,
            max_runtime_seconds: DEFAULT_MAX_RUNTIME_SECONDS,
            expose_port: None,
        }
    }
}

impl StartProcessOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }

    /// Hard ceiling on how long the process may run before being
    /// force-killed, in seconds. Defaults to 3600; capped server-side at 4h.
    pub fn max_runtime_seconds(mut self, max_runtime_seconds: i64) -> Self {
        self.max_runtime_seconds = max_runtime_seconds;
        self
    }

    /// Opt-in port to expose via a signed preview URL once the process is
    /// listening -- see `docs/NETWORK-INGRESS-DESIGN.md` and
    /// `POST /v1/sandboxes/{id}/preview/{port}`. Leave unset for a normal,
    /// fully network-isolated background process.
    pub fn expose_port(mut self, expose_port: i32) -> Self {
        self.expose_port = Some(expose_port);
        self
    }
}

#[derive(Serialize)]
struct StartProcessBody<'a> {
    command: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<&'a str>,
    max_runtime_seconds: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    expose_port: Option<i32>,
}

/// `POST /v1/sandboxes/{id}/processes`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ProcessStartResult {
    pub process_id: String,
    pub status: String,
    pub started_at: String,
}

/// One tracked background process (`SandboxProcessInfo`), as returned by
/// [`Client::list_processes`].
#[derive(Debug, Clone, Deserialize)]
pub struct ProcessInfo {
    pub process_id: String,
    pub command: String,
    pub description: Option<String>,
    pub status: String,
    pub started_at: String,
    pub exit_code: Option<i32>,
    pub expose_port: Option<i32>,
}

/// `GET /v1/sandboxes/{id}/processes`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ProcessListResult {
    pub processes: Vec<ProcessInfo>,
}

/// `GET /v1/sandboxes/{id}/processes/{process_id}/output`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ProcessOutputResult {
    pub status: String,
    pub stdout_chunk: String,
    pub next_offset: i64,
    pub truncated: bool,
    pub exit_code: Option<i32>,
}

/// One event from `GET /v1/sandboxes/{id}/processes/{process_id}/stream`: an
/// [`Output`](ProcessStreamEvent::Output) per new stdout chunk, then a
/// terminal [`Exit`](ProcessStreamEvent::Exit) once the process finishes.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum ProcessStreamEvent {
    Output {
        stdout_chunk: String,
        next_offset: i64,
        truncated: bool,
    },
    Exit {
        status: String,
        exit_code: Option<i32>,
    },
}

/// `POST /v1/sandboxes/{id}/processes/{process_id}/input`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ProcessInputResult {
    pub bytes_written: i64,
}

/// `POST /v1/sandboxes/{id}/processes/{process_id}/stop`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct ProcessStopResult {
    pub status: String,
    pub exit_code: Option<i32>,
}

impl Client {
    /// `POST /v1/sandboxes/{session_id}/processes` -- start a background
    /// process (a dev server, a test watcher, a long build, a REPL) that
    /// keeps running after this call returns. Not reachable over the
    /// network from any other call by default -- same per-exec network
    /// isolation applies here -- unless `options.expose_port(..)` is set.
    pub async fn start_process(
        &self,
        session_id: &str,
        command: &str,
        options: StartProcessOptions,
    ) -> Result<ProcessStartResult, BoxkiteError> {
        let body = StartProcessBody {
            command,
            description: options.description.as_deref(),
            max_runtime_seconds: options.max_runtime_seconds,
            expose_port: options.expose_port,
        };
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/processes"),
            )
            .json(&body);
        self.send(builder).await
    }

    /// `GET /v1/sandboxes/{session_id}/processes` -- every background
    /// process currently tracked for this session (running, exited, or
    /// stopped).
    pub async fn list_processes(
        &self,
        session_id: &str,
    ) -> Result<ProcessListResult, BoxkiteError> {
        let builder = self.request(
            Method::GET,
            &format!("/v1/sandboxes/{session_id}/processes"),
        );
        self.send(builder).await
    }

    /// `GET /v1/sandboxes/{session_id}/processes/{process_id}/output` --
    /// poll a background process's output since a given byte offset.
    /// Polling-style, not streaming. `since_offset` (from a previous call's
    /// `next_offset`, or 0 the first time) fetches only the new output
    /// since the last check.
    pub async fn get_process_output(
        &self,
        session_id: &str,
        process_id: &str,
        since_offset: i64,
    ) -> Result<ProcessOutputResult, BoxkiteError> {
        let builder = self
            .request(
                Method::GET,
                &format!("/v1/sandboxes/{session_id}/processes/{process_id}/output"),
            )
            .query(&[("since_offset", since_offset.to_string())]);
        self.send(builder).await
    }

    /// `GET /v1/sandboxes/{session_id}/processes/{process_id}/stream` -- live
    /// SSE stream of a background process's stdout, the streaming counterpart
    /// to [`get_process_output`](Client::get_process_output). Yields an
    /// [`ProcessStreamEvent::Output`] per new chunk, then a terminal
    /// [`ProcessStreamEvent::Exit`]. Bring `futures_util::StreamExt` into
    /// scope to consume it; the stream ends when the process finishes, the
    /// session is destroyed, or the caller drops it.
    pub fn stream_process_output(
        &self,
        session_id: &str,
        process_id: &str,
        since_offset: i64,
    ) -> Pin<Box<dyn Stream<Item = Result<ProcessStreamEvent, BoxkiteError>> + Send + 'static>> {
        let request_builder = self
            .request(
                Method::GET,
                &format!("/v1/sandboxes/{session_id}/processes/{process_id}/stream"),
            )
            .query(&[("since_offset", since_offset.to_string())]);

        Box::pin(async_stream::stream! {
            let mut event_source = match EventSource::new(request_builder) {
                Ok(event_source) => event_source,
                Err(err) => {
                    yield Err(BoxkiteError::Config(format!("failed to build stream request: {err}")));
                    return;
                }
            };

            while let Some(event) = event_source.next().await {
                match event {
                    Ok(Event::Open) => continue,
                    Ok(Event::Message(message)) => {
                        yield serde_json::from_str::<ProcessStreamEvent>(&message.data).map_err(BoxkiteError::from);
                    }
                    Err(reqwest_eventsource::Error::StreamEnded) => break,
                    Err(reqwest_eventsource::Error::InvalidStatusCode(status, response)) => {
                        let bytes = response.bytes().await.unwrap_or_default();
                        yield Err(crate::error::api_error_from_bytes(status.as_u16(), &bytes));
                        break;
                    }
                    Err(err) => {
                        yield Err(BoxkiteError::from(err));
                        break;
                    }
                }
            }
            event_source.close();
        })
    }

    /// `POST /v1/sandboxes/{session_id}/processes/{process_id}/input` --
    /// write to a tracked background process's stdin pipe (e.g. answering
    /// an interactive prompt).
    pub async fn send_process_input(
        &self,
        session_id: &str,
        process_id: &str,
        data: &str,
    ) -> Result<ProcessInputResult, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            data: &'a str,
        }
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/processes/{process_id}/input"),
            )
            .json(&Body { data });
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{session_id}/processes/{process_id}/stop` -- stop
    /// a tracked background process: SIGTERM, a short grace period, then
    /// SIGKILL if still alive.
    pub async fn stop_process(
        &self,
        session_id: &str,
        process_id: &str,
    ) -> Result<ProcessStopResult, BoxkiteError> {
        let builder = self.request(
            Method::POST,
            &format!("/v1/sandboxes/{session_id}/processes/{process_id}/stop"),
        );
        self.send(builder).await
    }
}
