mod common;

use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_preview_url_sends_ttl_when_given() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/preview/8080"))
        .and(body_json(json!({"ttl_seconds": 600})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "url": "https://preview.example.com/abc",
            "expires_at": "2026-01-01T01:00:00Z",
            "token_id": "tok_1"
        })))
        .mount(&server)
        .await;

    let preview = client
        .create_preview_url("sb_1", 8080, Some(600))
        .await
        .expect("create_preview_url should succeed");
    assert_eq!(preview.token_id, "tok_1");
    assert!(preview.url.contains("preview.example.com"));
}

#[tokio::test]
async fn create_preview_url_omits_ttl_when_none() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/preview/3000"))
        .and(body_json(json!({})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "url": "https://preview.example.com/def",
            "expires_at": "2026-01-01T00:15:00Z",
            "token_id": "tok_2"
        })))
        .mount(&server)
        .await;

    let preview = client
        .create_preview_url("sb_1", 3000, None)
        .await
        .expect("create_preview_url should succeed");
    assert_eq!(preview.token_id, "tok_2");
}

#[tokio::test]
async fn revoke_preview_url_sends_token_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/preview/8080/revoke"))
        .and(body_json(json!({"token_id": "tok_1"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "revoked": true, "token_id": "tok_1"
        })))
        .mount(&server)
        .await;

    let result = client
        .revoke_preview_url("sb_1", 8080, "tok_1")
        .await
        .expect("revoke_preview_url should succeed");
    assert!(result.revoked);
    assert_eq!(result.token_id, "tok_1");
}
