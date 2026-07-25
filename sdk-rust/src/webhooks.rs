//! Outbound webhook subscriptions (`docs/WEBHOOKS-DESIGN.md`):
//! `POST/GET/DELETE /v1/webhooks*` and `GET /v1/webhooks/{id}/deliveries`.
//! Mirrors `sdk-python`'s `create_webhook`/`list_webhooks`/`delete_webhook`/
//! `list_webhook_deliveries`.
//!
//! Push, out-of-process notifications on sandbox lifecycle events -- not
//! the same thing as this crate's own `AuditSink`-style pull model (which
//! doesn't exist here at all; that's a `src/boxkite` embedding concept, not
//! part of the hosted control-plane's HTTP surface this crate wraps).

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// Event types a webhook subscription can receive. See
/// `docs/WEBHOOKS-DESIGN.md` for the full event catalog.
///
/// `AuditLogEntry` added per GitHub issue #125 -- this enum had drifted out
/// of sync with the control plane's own `WebhookEventType` literal
/// (control-plane/src/control_plane/schemas.py).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum WebhookEventType {
    #[serde(rename = "sandbox.created")]
    SandboxCreated,
    #[serde(rename = "sandbox.destroyed")]
    SandboxDestroyed,
    #[serde(rename = "audit_log.entry")]
    AuditLogEntry,
}

/// Builder for `POST /v1/webhooks`'s optional fields.
#[derive(Debug, Clone, Default, Serialize)]
pub struct CreateWebhookOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<String>,
}

impl CreateWebhookOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Optional caller-supplied label for this subscription (e.g. `"Slack notifier"`).
    pub fn description(mut self, description: impl Into<String>) -> Self {
        self.description = Some(description.into());
        self
    }
}

/// A webhook subscription (`WebhookOut`/`WebhookCreatedResponse`). `secret`
/// is only ever populated on [`Client::create_webhook`]'s response -- the
/// raw signing secret, shown exactly once; it cannot be retrieved again.
#[derive(Debug, Clone, Deserialize)]
pub struct Webhook {
    pub id: String,
    pub url: String,
    pub event_types: Vec<String>,
    pub description: Option<String>,
    pub is_active: bool,
    #[serde(default)]
    pub payload_format: String,
    pub created_at: String,
    pub last_triggered_at: Option<String>,
    /// The raw signing secret. Only present on [`Client::create_webhook`]'s
    /// response -- use it to verify the `X-Boxkite-Webhook-Signature`
    /// header on every delivery.
    pub secret: Option<String>,
}

/// One delivery attempt for a webhook subscription
/// (`GET /v1/webhooks/{id}/deliveries`'s `WebhookDeliveryOut`).
#[derive(Debug, Clone, Deserialize)]
pub struct WebhookDelivery {
    pub id: String,
    pub event_type: String,
    /// `"pending"`, `"delivered"`, or `"failed"`.
    pub status: String,
    pub attempt_count: i64,
    pub next_attempt_at: String,
    pub last_attempt_at: Option<String>,
    pub response_status_code: Option<i32>,
    pub failure_reason: Option<String>,
    pub created_at: String,
    pub delivered_at: Option<String>,
}

/// Optional `list_webhook_deliveries` pagination parameters.
#[derive(Debug, Clone, Default)]
pub struct ListWebhookDeliveriesOptions {
    limit: Option<u32>,
    offset: Option<u32>,
}

impl ListWebhookDeliveriesOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Maximum number of entries to return (server default 20, max 100).
    pub fn limit(mut self, limit: u32) -> Self {
        self.limit = Some(limit);
        self
    }

    /// Number of entries to skip, newest-first.
    pub fn offset(mut self, offset: u32) -> Self {
        self.offset = Some(offset);
        self
    }
}

impl Client {
    /// `POST /v1/webhooks` -- register a webhook subscription.
    ///
    /// `url` is the HTTPS (or HTTP, for local testing) URL the control
    /// plane will POST events to; `event_types` must be non-empty.
    pub async fn create_webhook(
        &self,
        url: &str,
        event_types: &[WebhookEventType],
        options: CreateWebhookOptions,
    ) -> Result<Webhook, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            url: &'a str,
            event_types: &'a [WebhookEventType],
            #[serde(skip_serializing_if = "Option::is_none")]
            description: Option<&'a str>,
        }
        let body = Body {
            url,
            event_types,
            description: options.description.as_deref(),
        };
        let builder = self.request(Method::POST, "/v1/webhooks").json(&body);
        self.send(builder).await
    }

    /// `GET /v1/webhooks` -- webhook subscriptions for this account. The
    /// signing secret is never returned here.
    pub async fn list_webhooks(&self) -> Result<Vec<Webhook>, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/webhooks");
        self.send_or_default(builder).await
    }

    /// `DELETE /v1/webhooks/{id}` -- delete a webhook subscription owned by
    /// this account. 404s if already gone or never owned by this account.
    pub async fn delete_webhook(&self, subscription_id: &str) -> Result<(), BoxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/webhooks/{subscription_id}"));
        self.send_no_content(builder).await
    }

    /// `GET /v1/webhooks/{id}/deliveries` -- recent delivery attempts
    /// (pending/delivered/failed) for this subscription, newest first.
    pub async fn list_webhook_deliveries(
        &self,
        subscription_id: &str,
        options: ListWebhookDeliveriesOptions,
    ) -> Result<Vec<WebhookDelivery>, BoxkiteError> {
        let mut query = Vec::new();
        if let Some(limit) = options.limit {
            query.push(("limit", limit.to_string()));
        }
        if let Some(offset) = options.offset {
            query.push(("offset", offset.to_string()));
        }
        let builder = self
            .request(
                Method::GET,
                &format!("/v1/webhooks/{subscription_id}/deliveries"),
            )
            .query(&query);
        self.send_or_default(builder).await
    }
}
