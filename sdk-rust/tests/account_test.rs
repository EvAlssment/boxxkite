mod common;

use boxkite_client::AllowedCommandRule;
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn account_returns_identity() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/account"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "acct_1", "email": "dev@example.com", "created_at": "2026-01-01T00:00:00Z"
        })))
        .mount(&server)
        .await;

    let account = client.account().await.expect("account should succeed");
    assert_eq!(account.id, "acct_1");
    assert_eq!(account.email, "dev@example.com");
}

#[tokio::test]
async fn usage_returns_fair_use_limits() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/usage"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "monthly_sandbox_hours_used": 1.5,
            "monthly_sandbox_hours_limit": 100.0,
            "concurrent_sandboxes": 2,
            "concurrent_sandboxes_limit": 10
        })))
        .mount(&server)
        .await;

    let usage = client.usage().await.expect("usage should succeed");
    assert_eq!(usage.concurrent_sandboxes, 2);
    assert_eq!(usage.monthly_sandbox_hours_limit, 100.0);
}

#[tokio::test]
async fn request_password_reset_sends_email() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/auth/password-reset/request"))
        .and(body_json(json!({"email": "dev@example.com"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "message": "If that email is registered, a reset link has been sent."
        })))
        .mount(&server)
        .await;

    let resp = client
        .request_password_reset("dev@example.com")
        .await
        .expect("request should succeed");
    assert!(resp.message.contains("reset link"));
}

#[tokio::test]
async fn refresh_token_returns_new_pair() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/auth/refresh"))
        .and(body_json(json!({"refresh_token": "old_refresh"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "new_access",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": "new_refresh",
            "account": {"id": "acct_1", "email": "dev@example.com", "created_at": "2026-01-01T00:00:00Z"}
        })))
        .mount(&server)
        .await;

    let pair = client
        .refresh_token("old_refresh")
        .await
        .expect("refresh should succeed");
    assert_eq!(pair.access_token, "new_access");
    assert_eq!(pair.refresh_token.as_deref(), Some("new_refresh"));
    assert_eq!(pair.account.id, "acct_1");
}

#[tokio::test]
async fn resend_verification_uses_dashboard_jwt_not_api_key() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/auth/resend-verification"))
        .and(wiremock::matchers::header(
            "authorization",
            "Bearer dashboard_jwt_abc",
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"message": "sent"})))
        .mount(&server)
        .await;

    let resp = client
        .resend_verification("dashboard_jwt_abc")
        .await
        .expect("resend should succeed");
    assert_eq!(resp.message, "sent");
}

#[tokio::test]
async fn logout_sends_refresh_token_and_ignores_empty_body() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/auth/logout"))
        .and(body_json(json!({"refresh_token": "r1"})))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client.logout("r1").await.expect("logout should succeed");
}

#[tokio::test]
async fn get_allowed_commands_decodes_bare_and_object_rules() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/account/allowed-commands"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "rules": [
                "ls",
                {"command": "git", "args_allow": ["^status$"], "args_deny": ["^push$"]}
            ]
        })))
        .mount(&server)
        .await;

    let resp = client
        .get_allowed_commands()
        .await
        .expect("get should succeed");
    assert_eq!(resp.rules.len(), 2);
    assert_eq!(resp.rules[0].command, "ls");
    assert!(resp.rules[0].args_allow.is_empty());
    assert_eq!(resp.rules[1].command, "git");
    assert_eq!(resp.rules[1].args_allow, vec!["^status$".to_string()]);
    assert_eq!(resp.rules[1].args_deny, vec!["^push$".to_string()]);
}

#[tokio::test]
async fn set_allowed_commands_serializes_object_form() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("PUT"))
        .and(path("/v1/account/allowed-commands"))
        .and(body_json(json!({
            "rules": [
                {"command": "ls"},
                {"command": "git", "args_allow": ["^status$"]}
            ]
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "rules": [{"command": "ls"}, {"command": "git", "args_allow": ["^status$"]}]
        })))
        .mount(&server)
        .await;

    let rules = vec![
        AllowedCommandRule::new("ls"),
        AllowedCommandRule::new("git").args_allow(["^status$"]),
    ];
    let resp = client
        .set_allowed_commands(rules)
        .await
        .expect("set should succeed");
    assert_eq!(resp.rules.len(), 2);
}

#[tokio::test]
async fn clear_allowed_commands_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/account/allowed-commands"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .clear_allowed_commands()
        .await
        .expect("clear should succeed");
}
