mod common;

use boxkite_client::{BoxkiteError, CreateSandboxOptions, SandboxSize};
use serde_json::json;
use wiremock::matchers::{body_json, header, method, path, query_param};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_sandbox_sends_only_set_fields_and_parses_response() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .and(header("Authorization", "Bearer bxk_live_test"))
        .and(body_json(json!({"label": "demo", "size": "medium"})))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "sess_abc",
            "status": "active",
            "label": "demo",
            "created_at": "2026-01-01T00:00:00Z",
            "destroyed_at": null,
            "expires_at": "2026-01-01T02:00:00Z",
            "connect": {"pod_name": "sandbox-sess-abc", "note": "cluster-internal only"},
            "usage": {
                "monthly_sandbox_hours_used": 1.5,
                "monthly_sandbox_hours_limit": 20.0,
                "concurrent_sandboxes": 1,
                "concurrent_sandboxes_limit": 3
            }
        })))
        .mount(&server)
        .await;

    let sandbox = client
        .create_sandbox(
            CreateSandboxOptions::new()
                .label("demo")
                .size(SandboxSize::Medium),
        )
        .await
        .expect("create_sandbox should succeed");

    assert_eq!(sandbox.id, "sess_abc");
    assert_eq!(sandbox.status, "active");
    assert_eq!(sandbox.label.as_deref(), Some("demo"));
    assert_eq!(
        sandbox.usage.as_ref().unwrap().concurrent_sandboxes_limit,
        3
    );
    assert_eq!(
        sandbox.connect.as_ref().unwrap().pod_name.as_deref(),
        Some("sandbox-sess-abc")
    );
}

#[tokio::test]
async fn create_sandbox_sends_every_optional_field_when_set() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    let mut volume_mounts = std::collections::HashMap::new();
    volume_mounts.insert("vol_1".to_string(), "/mnt/data".to_string());

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .and(body_json(json!({
            "label": "build-job",
            "size": "large",
            "storage_gb": 20.0,
            "lifetime_minutes": 120,
            "count": 3,
            "secret_names": ["prod-stripe"],
            "image_id": "img_1",
            "mcp_connection_names": ["slack-conn"],
            "volume_mounts": {"vol_1": "/mnt/data"},
            "gpu_count": 2
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!([
            {
                "id": "sess_1", "status": "active", "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T02:00:00Z"
            },
            {
                "id": "sess_2", "status": "active", "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T02:00:00Z"
            },
            {
                "id": "sess_3", "status": "active", "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T02:00:00Z"
            }
        ])))
        .mount(&server)
        .await;

    let options = CreateSandboxOptions::new()
        .label("build-job")
        .size(SandboxSize::Large)
        .storage_gb(20.0)
        .lifetime_minutes(120)
        .count(3)
        .secret_names(["prod-stripe"])
        .image_id("img_1")
        .mcp_connection_names(["slack-conn"])
        .volume_mounts(volume_mounts)
        .gpu_count(2);

    let sandboxes = client
        .create_sandbox_batch(options)
        .await
        .expect("batch create should succeed");
    assert_eq!(sandboxes.len(), 3);
    assert_eq!(sandboxes[0].id, "sess_1");
    assert_eq!(sandboxes[2].id, "sess_3");
}

#[tokio::test]
async fn get_sandbox_hits_the_right_path() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_abc"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "sess_abc", "status": "destroyed", "created_at": "2026-01-01T00:00:00Z",
            "destroyed_at": "2026-01-01T01:00:00Z", "expires_at": "2026-01-01T02:00:00Z"
        })))
        .mount(&server)
        .await;

    let sandbox = client
        .get_sandbox("sess_abc")
        .await
        .expect("get_sandbox should succeed");
    assert_eq!(sandbox.status, "destroyed");
    assert_eq!(
        sandbox.destroyed_at.as_deref(),
        Some("2026-01-01T01:00:00Z")
    );
}

#[tokio::test]
async fn get_sandbox_maps_404_to_not_found_api_error() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/does-not-exist"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({
            "error": {"code": "not_found", "message": "Sandbox session not found."}
        })))
        .mount(&server)
        .await;

    let err = client.get_sandbox("does-not-exist").await.unwrap_err();
    match err {
        BoxkiteError::Api { status, code, .. } => {
            assert_eq!(status, 404);
            assert_eq!(code, "not_found");
        }
        other => panic!("expected BoxkiteError::Api, got {other:?}"),
    }
}

#[tokio::test]
async fn list_sandboxes_sends_active_only_query_param() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes"))
        .and(query_param("active_only", "true"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([
            {"id": "sess_1", "status": "active", "created_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-01T02:00:00Z"}
        ])))
        .mount(&server)
        .await;

    let sandboxes = client
        .list_sandboxes(true)
        .await
        .expect("list_sandboxes should succeed");
    assert_eq!(sandboxes.len(), 1);
    assert_eq!(sandboxes[0].id, "sess_1");
}

#[tokio::test]
async fn list_sandboxes_returns_empty_vec_on_empty_body() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let sandboxes = client
        .list_sandboxes(false)
        .await
        .expect("empty body should map to empty Vec");
    assert!(sandboxes.is_empty());
}

#[tokio::test]
async fn destroy_sandbox_sends_delete_and_ignores_empty_body() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/sandboxes/sess_abc"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .destroy_sandbox("sess_abc")
        .await
        .expect("destroy_sandbox should succeed");
}

#[tokio::test]
async fn create_sandbox_maps_concurrent_limit_429() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(429).set_body_json(json!({
            "error": {"code": "concurrent_sandbox_limit_reached", "message": "Too many concurrent sandboxes."}
        })))
        .mount(&server)
        .await;

    let err = client
        .create_sandbox(CreateSandboxOptions::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("concurrent_sandbox_limit_reached"));
    assert_eq!(err.status(), Some(429));
}

#[tokio::test]
async fn create_sandbox_maps_invalid_gpu_count_422() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(422).set_body_json(json!({
            "error": {"code": "invalid_gpu_count", "message": "gpu_count exceeds the per-session maximum."}
        })))
        .mount(&server)
        .await;

    let err = client
        .create_sandbox(CreateSandboxOptions::new().gpu_count(64))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("invalid_gpu_count"));
    assert_eq!(err.status(), Some(422));
}
