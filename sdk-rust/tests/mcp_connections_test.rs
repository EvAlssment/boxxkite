mod common;

use boxkite_client::McpCatalogId;
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_mcp_connection_sends_label_and_catalog_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/mcp-connections"))
        .and(body_json(
            json!({"label": "team-slack", "catalog_id": "slack"}),
        ))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "mcp_1", "label": "team-slack", "catalog_id": "slack", "host": "slack.com",
            "created_at": "2026-01-01T00:00:00Z", "last_used_at": null
        })))
        .mount(&server)
        .await;

    let connection = client
        .create_mcp_connection("team-slack", McpCatalogId::Slack)
        .await
        .expect("create_mcp_connection should succeed");

    assert_eq!(connection.id, "mcp_1");
    assert_eq!(connection.catalog_id, "slack");
    assert_eq!(connection.host, "slack.com");
}

#[tokio::test]
async fn list_mcp_connections_returns_empty_vec_on_no_content() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/mcp-connections"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let connections = client
        .list_mcp_connections()
        .await
        .expect("list_mcp_connections should succeed");
    assert!(connections.is_empty());
}

#[tokio::test]
async fn delete_mcp_connection_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/mcp-connections/mcp_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .delete_mcp_connection("mcp_1")
        .await
        .expect("delete_mcp_connection should succeed");
}

#[tokio::test]
async fn create_mcp_connection_maps_404_for_foreign_or_unknown_id() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/mcp-connections/does-not-exist"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({
            "error": {"code": "not_found", "message": "MCP connection not found."}
        })))
        .mount(&server)
        .await;

    let err = client
        .delete_mcp_connection("does-not-exist")
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("not_found"));
}
