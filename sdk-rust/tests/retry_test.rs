mod common;

use serde_json::json;
use wiremock::matchers::{method, path};
use wiremock::{Mock, ResponseTemplate};

fn account_body() -> serde_json::Value {
    json!({"id": "acct_1", "email": "dev@example.com", "created_at": "2026-01-01T00:00:00Z"})
}

// wiremock evaluates the most-recently-mounted matching mock first, so the
// transient-failure mock is mounted AFTER the success mock and limited with
// `up_to_n_times` -- once exhausted, requests fall through to the success.

#[tokio::test]
async fn retries_idempotent_get_after_503() {
    let server = common::mock_server().await;
    let client = common::client_for_with_retry(&server, 3);

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(200).set_body_json(account_body()))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(503))
        .up_to_n_times(2)
        .mount(&server)
        .await;

    let account = client
        .account()
        .await
        .expect("retry should recover the 503");
    assert_eq!(account.id, "acct_1");
}

#[tokio::test]
async fn retries_on_429() {
    let server = common::mock_server().await;
    let client = common::client_for_with_retry(&server, 3);

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(200).set_body_json(account_body()))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(429))
        .up_to_n_times(1)
        .mount(&server)
        .await;

    let account = client
        .account()
        .await
        .expect("retry should recover the 429");
    assert_eq!(account.id, "acct_1");
}

#[tokio::test]
async fn gives_up_after_max_retries() {
    let server = common::mock_server().await;
    let client = common::client_for_with_retry(&server, 2);

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(503))
        .mount(&server)
        .await;

    let err = client.account().await.unwrap_err();
    assert_eq!(err.status(), Some(503));
}

#[tokio::test]
async fn no_retry_when_disabled() {
    let server = common::mock_server().await;
    // Default client: retries disabled.
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(503))
        .expect(1)
        .mount(&server)
        .await;

    let err = client.account().await.unwrap_err();
    assert_eq!(err.status(), Some(503));
}

#[tokio::test]
async fn does_not_retry_non_idempotent_post_5xx() {
    let server = common::mock_server().await;
    let client = common::client_for_with_retry(&server, 3);

    // A bare POST that 500s must NOT be retried -- it may have partially
    // applied server-side. Exactly one request should reach the server.
    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(500))
        .expect(1)
        .mount(&server)
        .await;

    let err = client
        .create_sandbox(boxkite_client::CreateSandboxOptions::new())
        .await
        .unwrap_err();
    assert_eq!(err.status(), Some(500));
}
