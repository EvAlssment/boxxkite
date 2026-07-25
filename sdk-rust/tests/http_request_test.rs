mod common;

use std::collections::HashMap;

use boxkite_client::HttpRequestOptions;
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn http_request_forwards_method_url_headers_and_body() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/http-request"))
        .and(body_json(json!({
            "method": "POST",
            "url": "https://api.stripe.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:stripe-key}}"},
            "body": "amount=100"
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "status_code": 201,
            "headers": {"content-type": "application/json"},
            "body": "{\"id\":\"ch_1\"}",
            "truncated": false
        })))
        .mount(&server)
        .await;

    let mut headers = HashMap::new();
    headers.insert(
        "Authorization".to_string(),
        "Bearer {{secret:stripe-key}}".to_string(),
    );
    let result = client
        .http_request(
            "sb_1",
            "POST",
            "https://api.stripe.com/v1/charges",
            HttpRequestOptions::new()
                .headers(headers)
                .body("amount=100"),
        )
        .await
        .expect("http_request should succeed");

    assert_eq!(result.status_code, 201);
    assert_eq!(
        result.headers.get("content-type").map(String::as_str),
        Some("application/json")
    );
    assert!(result.body.contains("ch_1"));
    assert!(!result.truncated);
}

#[tokio::test]
async fn http_request_omits_unset_optional_fields() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/http-request"))
        .and(body_json(json!({
            "method": "GET",
            "url": "https://example.com"
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "status_code": 200,
            "headers": {},
            "body": "ok",
            "truncated": false
        })))
        .mount(&server)
        .await;

    let result = client
        .http_request(
            "sb_1",
            "GET",
            "https://example.com",
            HttpRequestOptions::new(),
        )
        .await
        .expect("http_request should succeed");
    assert_eq!(result.status_code, 200);
    assert_eq!(result.body, "ok");
}
