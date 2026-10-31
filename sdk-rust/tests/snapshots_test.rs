mod common;

use boxxkite_client::{CreateSandboxOptions, SandboxSession, SnapshotOptions, SnapshotStatus};
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn snapshot(session_id: serde_json::Value, status: &str) -> serde_json::Value {
    json!({
        "id": "snap_1",
        "session_id": session_id,
        "label": "before-change",
        "status": status,
        "storage_key_prefix": "snapshots/acct_1/snap_1",
        "size_bytes": 42,
        "created_at": "2026-10-30T12:00:00Z",
        "deleted_at": null
    })
}

#[tokio::test]
async fn create_snapshot_posts_optional_label_and_parses_typed_response() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/snapshots"))
        .and(body_json(json!({"label": "before-change"})))
        .respond_with(
            ResponseTemplate::new(201).set_body_json(snapshot(json!("sess_1"), "completed")),
        )
        .mount(&server)
        .await;

    let result = client
        .create_snapshot("sess_1", SnapshotOptions::new().label("before-change"))
        .await
        .expect("create_snapshot should succeed");
    assert_eq!(result.id, "snap_1");
    assert_eq!(result.session_id.as_deref(), Some("sess_1"));
    assert_eq!(result.status, SnapshotStatus::Completed);
    assert_eq!(result.size_bytes, 42);
}

#[tokio::test]
async fn list_snapshots_parses_nullable_source_and_each_status() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1/snapshots"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([
            snapshot(json!("sess_1"), "pending"),
            snapshot(serde_json::Value::Null, "completed"),
            snapshot(json!("sess_1"), "failed")
        ])))
        .mount(&server)
        .await;

    let results = client
        .list_snapshots("sess_1")
        .await
        .expect("list_snapshots should succeed");
    assert_eq!(results.len(), 3);
    assert_eq!(results[0].status, SnapshotStatus::Pending);
    assert_eq!(results[1].status, SnapshotStatus::Completed);
    assert_eq!(results[2].status, SnapshotStatus::Failed);
    assert_eq!(results[1].session_id, None);
}

#[tokio::test]
async fn list_snapshots_accepts_empty_response() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1/snapshots"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    assert!(client.list_snapshots("sess_1").await.unwrap().is_empty());
}

#[tokio::test]
async fn list_snapshots_accepts_null_response() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1/snapshots"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::Value::Null))
        .mount(&server)
        .await;

    assert!(client.list_snapshots("sess_1").await.unwrap().is_empty());
}

#[tokio::test]
async fn get_snapshot_fetches_by_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("GET"))
        .and(path("/v1/snapshots/snap_1"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(snapshot(serde_json::Value::Null, "completed")),
        )
        .mount(&server)
        .await;

    let result = client.get_snapshot("snap_1").await.unwrap();
    assert_eq!(result.session_id, None);
    assert_eq!(result.status, SnapshotStatus::Completed);
}

#[tokio::test]
async fn restore_snapshot_reuses_sandbox_response_type() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("POST"))
        .and(path("/v1/snapshots/snap_1/restore"))
        .and(body_json(json!({"label": "restored-copy"})))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "sess_restored",
            "status": "active",
            "label": "restored-copy",
            "created_at": "2026-10-30T12:05:00Z",
            "destroyed_at": null,
            "expires_at": null,
            "connect": null,
            "usage": null
        })))
        .mount(&server)
        .await;

    let result = client
        .restore_snapshot("snap_1", SnapshotOptions::new().label("restored-copy"))
        .await
        .expect("restore_snapshot should succeed");
    assert_eq!(result.id, "sess_restored");
    assert_eq!(result.label.as_deref(), Some("restored-copy"));
}

#[tokio::test]
async fn delete_snapshot_sends_delete() {
    let server: MockServer = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("DELETE"))
        .and(path("/v1/snapshots/snap_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .delete_snapshot("snap_1")
        .await
        .expect("delete_snapshot should succeed");
}

#[tokio::test]
async fn sandbox_session_create_snapshot_binds_its_session_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "sess_1",
            "status": "active",
            "label": "demo",
            "created_at": "2026-10-30T12:00:00Z",
            "destroyed_at": null,
            "expires_at": null,
            "connect": null,
            "usage": null
        })))
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/snapshots"))
        .and(body_json(json!({"label": "before-change"})))
        .respond_with(
            ResponseTemplate::new(201).set_body_json(snapshot(json!("sess_1"), "completed")),
        )
        .mount(&server)
        .await;
    Mock::given(method("DELETE"))
        .and(path("/v1/sandboxes/sess_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    let created = client
        .with_sandbox(
            CreateSandboxOptions::new(),
            |session: SandboxSession| async move {
                session
                    .create_snapshot(SnapshotOptions::new().label("before-change"))
                    .await
            },
        )
        .await
        .expect("session create_snapshot should succeed");
    assert_eq!(created.session_id.as_deref(), Some("sess_1"));
}

#[tokio::test]
async fn sandbox_session_list_snapshots_binds_its_session_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);
    Mock::given(method("POST"))
        .and(path("/v1/sandboxes"))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "sess_2",
            "status": "active",
            "label": "demo",
            "created_at": "2026-10-30T12:00:00Z",
            "destroyed_at": null,
            "expires_at": null,
            "connect": null,
            "usage": null
        })))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_2/snapshots"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!([snapshot(json!("sess_2"), "completed")])),
        )
        .mount(&server)
        .await;
    Mock::given(method("DELETE"))
        .and(path("/v1/sandboxes/sess_2"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    let listed = client
        .with_sandbox(CreateSandboxOptions::new(), |session| async move {
            session.list_snapshots().await
        })
        .await
        .expect("session list_snapshots should succeed");
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].session_id.as_deref(), Some("sess_2"));
}
