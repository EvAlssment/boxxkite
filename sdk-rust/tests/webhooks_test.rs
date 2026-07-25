mod common;

use boxkite_client::{CreateWebhookOptions, ListWebhookDeliveriesOptions, WebhookEventType};
use serde_json::json;
use wiremock::matchers::{body_json, method, path, query_param};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_webhook_sends_url_and_event_types_and_returns_secret_once() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/webhooks"))
        .and(body_json(json!({
            "url": "https://example.com/hook",
            "event_types": ["sandbox.created", "sandbox.destroyed"],
            "description": "Slack notifier"
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "wh_1", "url": "https://example.com/hook",
            "event_types": ["sandbox.created", "sandbox.destroyed"],
            "description": "Slack notifier", "is_active": true, "payload_format": "boxkite_v1",
            "created_at": "2026-01-01T00:00:00Z", "last_triggered_at": null,
            "secret": "whsec_abc123"
        })))
        .mount(&server)
        .await;

    let webhook = client
        .create_webhook(
            "https://example.com/hook",
            &[
                WebhookEventType::SandboxCreated,
                WebhookEventType::SandboxDestroyed,
            ],
            CreateWebhookOptions::new().description("Slack notifier"),
        )
        .await
        .expect("create_webhook should succeed");

    assert_eq!(webhook.id, "wh_1");
    assert_eq!(webhook.secret.as_deref(), Some("whsec_abc123"));
}

#[tokio::test]
async fn create_webhook_accepts_audit_log_entry_event_type() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/webhooks"))
        .and(body_json(json!({
            "url": "https://example.com/hook",
            "event_types": ["audit_log.entry"]
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "wh_2", "url": "https://example.com/hook",
            "event_types": ["audit_log.entry"],
            "description": null, "is_active": true, "payload_format": "boxkite_v1",
            "created_at": "2026-01-01T00:00:00Z", "last_triggered_at": null,
            "secret": "whsec_def456"
        })))
        .mount(&server)
        .await;

    let webhook = client
        .create_webhook(
            "https://example.com/hook",
            &[WebhookEventType::AuditLogEntry],
            CreateWebhookOptions::new(),
        )
        .await
        .expect("create_webhook should succeed");

    assert_eq!(webhook.id, "wh_2");
    assert_eq!(webhook.event_types, vec!["audit_log.entry"]);
}

#[tokio::test]
async fn list_webhooks_never_includes_secret() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/webhooks"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([{
            "id": "wh_1", "url": "https://example.com/hook", "event_types": ["sandbox.created"],
            "description": null, "is_active": true, "payload_format": "boxkite_v1",
            "created_at": "2026-01-01T00:00:00Z", "last_triggered_at": null
        }])))
        .mount(&server)
        .await;

    let webhooks = client
        .list_webhooks()
        .await
        .expect("list_webhooks should succeed");
    assert_eq!(webhooks.len(), 1);
    assert!(webhooks[0].secret.is_none());
}

#[tokio::test]
async fn delete_webhook_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/webhooks/wh_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .delete_webhook("wh_1")
        .await
        .expect("delete_webhook should succeed");
}

#[tokio::test]
async fn list_webhook_deliveries_sends_pagination_params() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/webhooks/wh_1/deliveries"))
        .and(query_param("limit", "10"))
        .and(query_param("offset", "0"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([{
            "id": "del_1", "event_type": "sandbox.created", "status": "delivered",
            "attempt_count": 1, "next_attempt_at": "2026-01-01T00:00:00Z",
            "last_attempt_at": "2026-01-01T00:00:00Z", "response_status_code": 200,
            "failure_reason": null, "created_at": "2026-01-01T00:00:00Z",
            "delivered_at": "2026-01-01T00:00:00Z"
        }])))
        .mount(&server)
        .await;

    let deliveries = client
        .list_webhook_deliveries(
            "wh_1",
            ListWebhookDeliveriesOptions::new().limit(10).offset(0),
        )
        .await
        .expect("list_webhook_deliveries should succeed");

    assert_eq!(deliveries.len(), 1);
    assert_eq!(deliveries[0].status, "delivered");
}

#[tokio::test]
async fn create_webhook_maps_validation_error_422() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/webhooks"))
        .respond_with(ResponseTemplate::new(422).set_body_json(json!({
            "error": {"code": "validation_error", "message": "event_types must not be empty."}
        })))
        .mount(&server)
        .await;

    let err = client
        .create_webhook("https://example.com/hook", &[], CreateWebhookOptions::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("validation_error"));
    assert_eq!(err.status(), Some(422));
}
