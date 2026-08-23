//! sdk-rust side of the cross-SDK error taxonomy (GitHub issues #94, #140).
//!
//! Pins that an error `code` classifies the same way here as in sdk-python's
//! `api_error_type`, sdk-js's error subclasses and sdk-go's typed errors.
//! Driven through a real wiremock-served response rather than the internal
//! parser, matching how every other test in this crate exercises the client.

mod common;

use boxxkite_client::ApiErrorKind;
use common::{client_for, mock_server};
use wiremock::matchers::{method, path};
use wiremock::{Mock, ResponseTemplate};

async fn kind_for(status: u16, body: &str) -> Option<ApiErrorKind> {
    let server = mock_server().await;
    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1"))
        .respond_with(
            ResponseTemplate::new(status).set_body_raw(body.to_string(), "application/json"),
        )
        .mount(&server)
        .await;
    client_for(&server)
        .get_sandbox("sess_1")
        .await
        .unwrap_err()
        .kind()
}

async fn retryable_for(status: u16, body: &str) -> bool {
    let server = mock_server().await;
    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1"))
        .respond_with(
            ResponseTemplate::new(status).set_body_raw(body.to_string(), "application/json"),
        )
        .mount(&server)
        .await;
    client_for(&server)
        .get_sandbox("sess_1")
        .await
        .unwrap_err()
        .retryable()
}

#[tokio::test]
async fn classifies_the_named_failure_classes() {
    let cases = [
        (
            429,
            r#"{"error":{"code":"concurrent_sandbox_limit_reached"}}"#,
            ApiErrorKind::QuotaExceeded,
        ),
        (
            403,
            r#"{"error":{"code":"egress_denied"}}"#,
            ApiErrorKind::EgressDenied,
        ),
        (
            403,
            r#"{"error":{"code":"command_not_allowed"}}"#,
            ApiErrorKind::CapabilityDenied,
        ),
        (
            403,
            r#"{"error":{"code":"readonly_filesystem"}}"#,
            ApiErrorKind::ReadonlyFilesystem,
        ),
        (
            503,
            r#"{"error":{"code":"sandbox_not_ready"}}"#,
            ApiErrorKind::SandboxNotReady,
        ),
        (
            500,
            r#"{"error":{"code":"sandbox_crashed"}}"#,
            ApiErrorKind::SandboxCrashed,
        ),
    ];
    for (status, body, want) in cases {
        assert_eq!(kind_for(status, body).await, Some(want), "body: {body}");
    }
}

#[tokio::test]
async fn an_unknown_4xx_code_is_other_rather_than_a_hard_error() {
    assert_eq!(
        kind_for(404, r#"{"error":{"code":"not_found"}}"#).await,
        Some(ApiErrorKind::Other)
    );
}

#[tokio::test]
async fn an_unknown_5xx_code_falls_back_to_service_unavailable() {
    assert_eq!(
        kind_for(502, r#"{"error":{"code":"upstream_error"}}"#).await,
        Some(ApiErrorKind::ServiceUnavailable)
    );
}

#[tokio::test]
async fn a_5xx_whose_envelope_omits_retryable_is_still_retryable() {
    // Regression: #[serde(default)] made this false, so a control-plane
    // predating the taxonomy looked non-retryable on a 5xx. Python, JS and Go
    // all default a 5xx to retryable.
    assert!(retryable_for(500, r#"{"error":{"code":"internal_error"}}"#).await);
}

#[tokio::test]
async fn an_explicit_retryable_false_still_wins_on_a_5xx() {
    assert!(
        !retryable_for(
            500,
            r#"{"error":{"code":"internal_error","retryable":false}}"#
        )
        .await
    );
}
