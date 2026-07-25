mod common;

use boxkite_client::{CreateSandboxOptions, ExecOptions};
use serde_json::json;
use wiremock::matchers::{method, path};
use wiremock::{Mock, ResponseTemplate};

fn sandbox_body(id: &str) -> serde_json::Value {
    json!({
        "id": id,
        "status": "active",
        "label": "demo",
        "created_at": "2026-01-01T00:00:00Z",
        "destroyed_at": null,
        "expires_at": "2026-01-01T02:00:00Z",
        "connect": null,
        "usage": null
    })
}

#[tokio::test]
async fn with_sandbox_creates_runs_and_destroys() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(201).set_body_json(sandbox_body("sb_1")))
        .mount(&server)
        .await;

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/exec"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "exit_code": 0, "stdout": "hi\n", "stderr": ""
        })))
        .mount(&server)
        .await;

    Mock::given(method("DELETE"))
        .and(path("/v1/sandboxes/sb_1"))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&server)
        .await;

    let stdout = client
        .with_sandbox(CreateSandboxOptions::new().label("demo"), |sb| async move {
            assert_eq!(sb.id(), "sb_1");
            let result = sb.exec("echo hi", ExecOptions::new()).await?;
            Ok(result.stdout)
        })
        .await
        .expect("with_sandbox should succeed");

    assert_eq!(stdout, "hi\n");
    // Teardown assertion is enforced by `.expect(1)` on the DELETE mock when
    // the server drops at end of test.
}

#[tokio::test]
async fn with_sandbox_destroys_even_when_closure_errors() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(201).set_body_json(sandbox_body("sb_2")))
        .mount(&server)
        .await;

    Mock::given(method("DELETE"))
        .and(path("/v1/sandboxes/sb_2"))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&server)
        .await;

    let result: Result<(), _> = client
        .with_sandbox(CreateSandboxOptions::new(), |_sb| async move {
            Err(boxkite_client::BoxkiteError::Config("boom".to_string()))
        })
        .await;

    assert!(result.is_err());
}
