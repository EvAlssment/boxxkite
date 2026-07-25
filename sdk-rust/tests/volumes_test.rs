mod common;

use boxkite_client::CreateVolumeOptions;
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_volume_sends_size_and_label() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/volumes"))
        .and(body_json(json!({"size_gb": 50.0, "label": "shared-data"})))
        .respond_with(ResponseTemplate::new(202).set_body_json(json!({
            "id": "vol_1", "label": "shared-data", "status": "queued", "created_at": "2026-01-01T00:00:00Z"
        })))
        .mount(&server)
        .await;

    let volume = client
        .create_volume(50.0, CreateVolumeOptions::new().label("shared-data"))
        .await
        .expect("create_volume should succeed");

    assert_eq!(volume.id, "vol_1");
    assert_eq!(volume.status, "queued");
}

#[tokio::test]
async fn create_volume_omits_label_when_unset() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/volumes"))
        .and(body_json(json!({"size_gb": 10.0})))
        .respond_with(ResponseTemplate::new(202).set_body_json(json!({
            "id": "vol_2", "status": "queued", "created_at": "2026-01-01T00:00:00Z"
        })))
        .mount(&server)
        .await;

    client
        .create_volume(10.0, CreateVolumeOptions::new())
        .await
        .expect("create_volume should succeed");
}

#[tokio::test]
async fn get_volume_parses_ready_status() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/volumes/vol_1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "vol_1", "label": "shared-data", "size_gb": 50.0, "status": "ready",
            "pvc_name": "boxkite-vol-1", "failure_reason": null, "created_at": "2026-01-01T00:00:00Z"
        })))
        .mount(&server)
        .await;

    let volume = client
        .get_volume("vol_1")
        .await
        .expect("get_volume should succeed");
    assert_eq!(volume.status, "ready");
    assert_eq!(volume.pvc_name.as_deref(), Some("boxkite-vol-1"));
}

#[tokio::test]
async fn list_volumes_returns_populated_vec() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/volumes"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([
            {"id": "vol_1", "size_gb": 10.0, "status": "ready", "created_at": "2026-01-01T00:00:00Z"}
        ])))
        .mount(&server)
        .await;

    let volumes = client
        .list_volumes()
        .await
        .expect("list_volumes should succeed");
    assert_eq!(volumes.len(), 1);
}

#[tokio::test]
async fn delete_volume_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/volumes/vol_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .delete_volume("vol_1")
        .await
        .expect("delete_volume should succeed");
}
