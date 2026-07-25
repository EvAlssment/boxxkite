mod common;

use boxkite_client::{
    ExecOptions, FileOptions, GlobOptions, GrepOptions, LsOptions, StrReplaceOptions, ViewOptions,
};
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn exec_sends_command_and_parses_result() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/exec"))
        .and(body_json(
            json!({"command": "echo hi", "timeout": 10, "description": "smoke test"}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "exit_code": 0, "stdout": "hi\n", "stderr": ""
        })))
        .mount(&server)
        .await;

    let result = client
        .exec(
            "sess_1",
            "echo hi",
            ExecOptions::new().timeout(10).description("smoke test"),
        )
        .await
        .expect("exec should succeed");

    assert_eq!(result.exit_code, 0);
    assert_eq!(result.stdout, "hi\n");
}

#[tokio::test]
async fn exec_omits_optional_fields_when_unset() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/exec"))
        .and(body_json(json!({"command": "echo hi"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "exit_code": 0, "stdout": "hi\n", "stderr": ""
        })))
        .mount(&server)
        .await;

    client
        .exec("sess_1", "echo hi", ExecOptions::new())
        .await
        .expect("exec should succeed");
}

#[tokio::test]
async fn file_create_sends_path_and_content() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/files"))
        .and(body_json(json!({"path": "hello.txt", "content": "hi\n"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "path": "hello.txt", "size": 3, "created": true
        })))
        .mount(&server)
        .await;

    let result = client
        .file_create("sess_1", "hello.txt", "hi\n", FileOptions::new())
        .await
        .expect("file_create should succeed");

    assert_eq!(result.path, "hello.txt");
    assert_eq!(result.size, 3);
    assert!(result.created);
}

#[tokio::test]
async fn view_sends_view_range() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/files/view"))
        .and(body_json(
            json!({"path": "hello.txt", "view_range": [1, 10]}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "content": "hi\n", "lines": 1, "is_directory": false, "entries": null
        })))
        .mount(&server)
        .await;

    let result = client
        .view("sess_1", "hello.txt", ViewOptions::new().view_range(1, 10))
        .await
        .expect("view should succeed");

    assert_eq!(result.content, "hi\n");
    assert!(!result.is_directory);
    assert!(result.entries.is_none());
}

#[tokio::test]
async fn str_replace_sends_replace_all_flag() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/files/str-replace"))
        .and(body_json(json!({
            "path": "hello.txt", "old_str": "hi", "new_str": "hello", "replace_all": true
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "path": "hello.txt", "replaced": true, "occurrences": 2
        })))
        .mount(&server)
        .await;

    let result = client
        .str_replace(
            "sess_1",
            "hello.txt",
            "hi",
            "hello",
            StrReplaceOptions::new().replace_all(true),
        )
        .await
        .expect("str_replace should succeed");

    assert_eq!(result.occurrences, 2);
}

#[tokio::test]
async fn ls_defaults_path_to_root() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/files/ls"))
        .and(body_json(json!({"path": "/"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "entries": [{"name": "hello.txt", "is_dir": false}]
        })))
        .mount(&server)
        .await;

    let result = client
        .ls("sess_1", LsOptions::new())
        .await
        .expect("ls should succeed");
    assert_eq!(result.entries.len(), 1);
}

#[tokio::test]
async fn glob_sends_pattern_and_path() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/files/glob"))
        .and(body_json(
            json!({"pattern": "**/*.py", "path": "/workspace"}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "matches": [{"path": "/workspace/main.py"}]
        })))
        .mount(&server)
        .await;

    let result = client
        .glob("sess_1", "**/*.py", GlobOptions::new().path("/workspace"))
        .await
        .expect("glob should succeed");

    assert_eq!(result.matches.len(), 1);
}

#[tokio::test]
async fn grep_sends_all_fields() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/files/grep"))
        .and(body_json(json!({
            "pattern": "TODO", "path": "/workspace", "glob": "*.rs", "max_matches": 10
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "matches": [], "error": null, "truncated": false
        })))
        .mount(&server)
        .await;

    let result = client
        .grep(
            "sess_1",
            "TODO",
            GrepOptions::new()
                .path("/workspace")
                .glob("*.rs")
                .max_matches(10),
        )
        .await
        .expect("grep should succeed");

    assert!(result.matches.is_empty());
    assert!(!result.truncated);
}

#[tokio::test]
async fn exec_maps_command_not_allowed_403() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/sandboxes/sess_1/exec"))
        .respond_with(ResponseTemplate::new(403).set_body_json(json!({
            "error": {"code": "command_not_allowed", "message": "This command isn't on the allowlist."}
        })))
        .mount(&server)
        .await;

    let err = client
        .exec("sess_1", "rm -rf /", ExecOptions::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("command_not_allowed"));
    assert_eq!(err.status(), Some(403));
}
