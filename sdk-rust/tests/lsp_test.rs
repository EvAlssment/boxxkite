mod common;

use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn lsp_start_returns_handle() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/lsp/start"))
        .and(body_json(json!({"language": "python"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"lsp_id": "lsp_abc"})))
        .mount(&server)
        .await;

    let started = client
        .lsp_start("sb_1", "python")
        .await
        .expect("lsp_start should succeed");
    assert_eq!(started.lsp_id, "lsp_abc");
}

#[tokio::test]
async fn lsp_open_sends_path_and_content() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/lsp/lsp_abc/open"))
        .and(body_json(
            json!({"path": "/app/main.py", "content": "x = 1\n"}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"status": "opened"})))
        .mount(&server)
        .await;

    let opened = client
        .lsp_open("sb_1", "lsp_abc", "/app/main.py", "x = 1\n")
        .await
        .expect("lsp_open should succeed");
    assert_eq!(opened.status, "opened");
}

#[tokio::test]
async fn lsp_completion_returns_items() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/lsp/lsp_abc/completion"))
        .and(body_json(
            json!({"path": "/app/main.py", "line": 0, "character": 3}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "items": [{"label": "print", "kind": 3}]
        })))
        .mount(&server)
        .await;

    let completion = client
        .lsp_completion("sb_1", "lsp_abc", "/app/main.py", 0, 3)
        .await
        .expect("lsp_completion should succeed");
    assert_eq!(completion.items.len(), 1);
    assert_eq!(completion.items[0]["label"], "print");
}

#[tokio::test]
async fn lsp_stop_returns_status() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sb_1/lsp/lsp_abc/stop"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"status": "stopped"})))
        .mount(&server)
        .await;

    let stopped = client
        .lsp_stop("sb_1", "lsp_abc")
        .await
        .expect("lsp_stop should succeed");
    assert_eq!(stopped.status, "stopped");
}
