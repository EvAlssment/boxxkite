mod common;

use boxkite_client::CreateSecretOptions;
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_secret_sends_name_value_and_allowed_hosts() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/secrets"))
        .and(body_json(json!({
            "name": "stripe-key",
            "value": "sk_test_abc123",
            "allowed_hosts": ["api.stripe.com"]
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "secret_1", "name": "stripe-key", "allowed_hosts": ["api.stripe.com"],
            "trust_tier": null, "created_at": "2026-01-01T00:00:00Z", "last_used_at": null
        })))
        .mount(&server)
        .await;

    let secret = client
        .create_secret(
            "stripe-key",
            "sk_test_abc123",
            &["api.stripe.com".to_string()],
            CreateSecretOptions::new(),
        )
        .await
        .expect("create_secret should succeed");

    assert_eq!(secret.id, "secret_1");
    assert_eq!(secret.allowed_hosts, vec!["api.stripe.com".to_string()]);
}

#[tokio::test]
async fn create_secret_sends_trust_tier_when_given() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/secrets"))
        .and(body_json(json!({
            "name": "wallet-key",
            "value": "0xabc",
            "allowed_hosts": ["rpc.example.com"],
            "trust_tier": "testnet"
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "secret_2", "name": "wallet-key", "allowed_hosts": ["rpc.example.com"],
            "trust_tier": "testnet", "created_at": "2026-01-01T00:00:00Z", "last_used_at": null
        })))
        .mount(&server)
        .await;

    let secret = client
        .create_secret(
            "wallet-key",
            "0xabc",
            &["rpc.example.com".to_string()],
            CreateSecretOptions::new().trust_tier("testnet"),
        )
        .await
        .expect("create_secret should succeed");

    assert_eq!(secret.trust_tier.as_deref(), Some("testnet"));
}

#[tokio::test]
async fn list_secrets_returns_empty_vec_on_no_content() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/secrets"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let secrets = client
        .list_secrets()
        .await
        .expect("list_secrets should succeed");
    assert!(secrets.is_empty());
}

#[tokio::test]
async fn delete_secret_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/secrets/secret_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .delete_secret("secret_1")
        .await
        .expect("delete_secret should succeed");
}

#[tokio::test]
async fn delete_secret_maps_404_for_foreign_or_unknown_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/secrets/does-not-exist"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({
            "error": {"code": "not_found", "message": "Secret not found"}
        })))
        .mount(&server)
        .await;

    let err = client.delete_secret("does-not-exist").await.unwrap_err();
    assert_eq!(err.code(), Some("not_found"));
}
