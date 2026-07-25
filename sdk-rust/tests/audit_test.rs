mod common;

use boxkite_client::GetLogOptions;
use futures_util::StreamExt;
use serde_json::json;
use wiremock::matchers::{method, path, query_param};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn get_log_sends_limit_and_offset() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1/log"))
        .and(query_param("limit", "20"))
        .and(query_param("offset", "5"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "entries": [{
                "id": "row_1", "session_id": "sess_1", "source": "agent", "operation": "exec",
                "detail": {"command": "echo hi"}, "exit_code": 0, "output_truncated": "hi\n",
                "started_at": "2026-01-01T00:00:00Z", "duration_ms": 12,
                "row_hash": "abc", "prev_hash": null
            }],
            "limit": 20, "offset": 5, "total": 1
        })))
        .mount(&server)
        .await;

    let response = client
        .get_log("sess_1", GetLogOptions::new().limit(20).offset(5))
        .await
        .expect("get_log should succeed");

    assert_eq!(response.entries.len(), 1);
    assert_eq!(response.entries[0].source, "agent");
    assert_eq!(response.total, 1);
}

#[tokio::test]
async fn get_log_maps_404() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/missing/log"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({
            "error": {"code": "not_found", "message": "Sandbox session not found."}
        })))
        .mount(&server)
        .await;

    let err = client
        .get_log("missing", GetLogOptions::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("not_found"));
}

#[tokio::test]
async fn watch_streams_sse_entries_in_order() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    let body = concat!(
        "data: {\"id\":\"row_1\",\"session_id\":\"sess_1\",\"source\":\"agent\",\"operation\":\"exec\",",
        "\"detail\":{},\"exit_code\":0,\"output_truncated\":null,\"started_at\":\"2026-01-01T00:00:00Z\",",
        "\"duration_ms\":1,\"row_hash\":null,\"prev_hash\":null}\n\n",
        "data: {\"id\":\"row_2\",\"session_id\":\"sess_1\",\"source\":\"human_takeover\",\"operation\":\"takeover_start\",",
        "\"detail\":{},\"exit_code\":null,\"output_truncated\":null,\"started_at\":\"2026-01-01T00:00:01Z\",",
        "\"duration_ms\":0,\"row_hash\":null,\"prev_hash\":null}\n\n",
    );

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/sess_1/watch"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("Content-Type", "text/event-stream")
                .set_body_raw(body, "text/event-stream"),
        )
        .mount(&server)
        .await;

    let mut stream = client.watch("sess_1");
    let first = stream
        .next()
        .await
        .expect("first event")
        .expect("first event ok");
    assert_eq!(first.id, "row_1");
    assert_eq!(first.source, "agent");

    let second = stream
        .next()
        .await
        .expect("second event")
        .expect("second event ok");
    assert_eq!(second.id, "row_2");
    assert_eq!(second.source, "human_takeover");
}

#[tokio::test]
async fn watch_maps_404_before_streaming() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/sandboxes/missing/watch"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({
            "error": {"code": "not_found", "message": "Sandbox session not found."}
        })))
        .mount(&server)
        .await;

    let mut stream = client.watch("missing");
    let first = stream
        .next()
        .await
        .expect("stream should yield an error, not end silently");
    let err = first.unwrap_err();
    assert_eq!(err.code(), Some("not_found"));
    assert_eq!(err.status(), Some(404));
}
