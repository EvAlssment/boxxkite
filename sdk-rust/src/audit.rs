//! Audit log: `GET /v1/sandboxes/{id}/log` (paginated) and
//! `GET /v1/sandboxes/{id}/watch` (a live Server-Sent Events feed). Mirrors
//! `sdk-python`'s `get_log`/`watch`.

use std::pin::Pin;

use futures_util::{Stream, StreamExt};
use reqwest::Method;
use reqwest_eventsource::{Event, EventSource};
use serde::Deserialize;

use crate::client::Client;
use crate::error::BoxkiteError;

/// One row of `GET /v1/sandboxes/{id}/log`'s audit trail -- one exec/file
/// operation, whether issued by the agent (`source: "agent"`) or by a human
/// during a `takeover` session (`source: "human_takeover"`).
#[derive(Debug, Clone, Deserialize)]
pub struct AuditLogEntry {
    pub id: String,
    pub session_id: String,
    /// `"agent"` or `"human_takeover"`.
    pub source: String,
    pub operation: String,
    pub detail: serde_json::Value,
    pub exit_code: Option<i64>,
    pub output_truncated: Option<String>,
    pub started_at: String,
    pub duration_ms: i64,
    /// Hash-chain digest for this row (`docs/TAMPER-EVIDENT-AUDIT-DESIGN.md`),
    /// or `None` for legacy rows written before hash-chaining was added.
    pub row_hash: Option<String>,
    pub prev_hash: Option<String>,
}

/// `GET /v1/sandboxes/{id}/log`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct AuditLogResponse {
    pub entries: Vec<AuditLogEntry>,
    pub limit: i64,
    pub offset: i64,
    pub total: i64,
}

/// Optional `get_log` pagination parameters.
#[derive(Debug, Clone, Default)]
pub struct GetLogOptions {
    limit: Option<u32>,
    offset: Option<u32>,
}

impl GetLogOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Maximum number of entries to return (1-500, server default 50).
    pub fn limit(mut self, limit: u32) -> Self {
        self.limit = Some(limit);
        self
    }

    /// Number of entries to skip, oldest-first.
    pub fn offset(mut self, offset: u32) -> Self {
        self.offset = Some(offset);
        self
    }
}

impl Client {
    /// `GET /v1/sandboxes/{id}/log` -- paginated exec/file-op audit history.
    pub async fn get_log(
        &self,
        session_id: &str,
        options: GetLogOptions,
    ) -> Result<AuditLogResponse, BoxkiteError> {
        let mut query = Vec::new();
        if let Some(limit) = options.limit {
            query.push(("limit", limit.to_string()));
        }
        if let Some(offset) = options.offset {
            query.push(("offset", offset.to_string()));
        }
        let builder = self
            .request(Method::GET, &format!("/v1/sandboxes/{session_id}/log"))
            .query(&query);
        self.send(builder).await
    }

    /// `GET /v1/sandboxes/{id}/watch` -- streams new audit-log entries as
    /// they're written, one [`AuditLogEntry`] per Server-Sent Event. This is
    /// a live feed of exec/file operations as the control-plane logs them,
    /// not a live terminal (that's [`Client::takeover`]).
    ///
    /// The stream ends when the sandbox session is destroyed or the caller
    /// drops it. Bring `futures_util::StreamExt` (or any other `Stream`
    /// combinator crate) into scope to consume it:
    ///
    /// ```no_run
    /// # use futures_util::StreamExt;
    /// # async fn example(client: boxkite_client::Client, session_id: &str) {
    /// let mut entries = client.watch(session_id);
    /// while let Some(entry) = entries.next().await {
    ///     match entry {
    ///         Ok(entry) => println!("{entry:?}"),
    ///         Err(err) => {
    ///             eprintln!("watch error: {err}");
    ///             break;
    ///         }
    ///     }
    /// }
    /// # }
    /// ```
    pub fn watch(
        &self,
        session_id: &str,
    ) -> Pin<Box<dyn Stream<Item = Result<AuditLogEntry, BoxkiteError>> + Send + 'static>> {
        let request_builder =
            self.request(Method::GET, &format!("/v1/sandboxes/{session_id}/watch"));

        Box::pin(async_stream::stream! {
            let mut event_source = match EventSource::new(request_builder) {
                Ok(event_source) => event_source,
                Err(err) => {
                    yield Err(BoxkiteError::Config(format!("failed to build watch request: {err}")));
                    return;
                }
            };

            while let Some(event) = event_source.next().await {
                match event {
                    Ok(Event::Open) => continue,
                    Ok(Event::Message(message)) => {
                        yield serde_json::from_str::<AuditLogEntry>(&message.data).map_err(BoxkiteError::from);
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
}
