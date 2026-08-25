mod common;

use boxxkite_client::{IngestMemoryOptions, ListMemoriesOptions, RememberOptions};
use serde_json::json;
use wiremock::matchers::{body_json, method, path, query_param};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn remember_sends_content_scope_and_kind() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/memory"))
        .and(body_json(json!({
            "content": "The user prefers TypeScript.",
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "id": "mem_1",
            "scope": "default",
            "kind": "fact",
            "content": "The user prefers TypeScript.",
            "metadata": {},
            "source_session_id": null,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "expires_at": null,
            "source_content": null,
            "document_date": null,
            "event_dates": [],
            "importance": 0.5,
            "access_count": 0,
            "last_accessed_at": null,
            "superseded_at": null,
            "superseded_by_id": null
        })))
        .mount(&server)
        .await;

    let memory = client
        .remember("The user prefers TypeScript.", RememberOptions::new())
        .await
        .expect("remember should succeed");

    assert_eq!(memory.id, "mem_1");
}

#[tokio::test]
async fn ingest_memory_sends_content_and_scope() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/memory/ingest"))
        .and(body_json(json!({ "content": "long transcript" })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "source_session_id": null,
            "memories": []
        })))
        .mount(&server)
        .await;

    let resp = client
        .ingest_memory("long transcript", IngestMemoryOptions::new())
        .await
        .expect("ingest_memory should succeed");

    assert!(resp.memories.is_empty());
}

#[tokio::test]
async fn recall_sends_query_and_limit() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/memory/search"))
        .and(query_param("q", "TypeScript"))
        .and(query_param("limit", "10"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "query": "TypeScript",
            "memories": []
        })))
        .mount(&server)
        .await;

    let resp = client
        .recall("TypeScript", None, None)
        .await
        .expect("recall should succeed");

    assert_eq!(resp.query, "TypeScript");
}

#[tokio::test]
async fn memory_profile_returns_static_and_dynamic() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/memory/profile"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "static": [],
            "dynamic": []
        })))
        .mount(&server)
        .await;

    let resp = client
        .memory_profile(None, None)
        .await
        .expect("memory_profile should succeed");

    assert!(resp.r#static.is_empty());
    assert!(resp.dynamic.is_empty());
}

#[tokio::test]
async fn list_memories_returns_empty_vec_on_no_content() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/memory"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let memories = client
        .list_memories(ListMemoriesOptions::default())
        .await
        .expect("list_memories should succeed");

    assert!(memories.is_empty());
}

#[tokio::test]
async fn get_memory_returns_the_memory() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/memory/mem_1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "mem_1",
            "scope": "default",
            "kind": "fact",
            "content": "x",
            "metadata": {},
            "source_session_id": null,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "expires_at": null,
            "source_content": null,
            "document_date": null,
            "event_dates": [],
            "importance": 0.5,
            "access_count": 0,
            "last_accessed_at": null,
            "superseded_at": null,
            "superseded_by_id": null
        })))
        .mount(&server)
        .await;

    let memory = client
        .get_memory("mem_1")
        .await
        .expect("get_memory should succeed");

    assert_eq!(memory.id, "mem_1");
}

#[tokio::test]
async fn memory_relations_returns_the_relations() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/memory/mem_1/relations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "memory_id": "mem_1",
            "relations": []
        })))
        .mount(&server)
        .await;

    let resp = client
        .memory_relations("mem_1", None)
        .await
        .expect("memory_relations should succeed");

    assert_eq!(resp.memory_id, "mem_1");
}

#[tokio::test]
async fn forget_memory_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/memory/mem_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .forget_memory("mem_1")
        .await
        .expect("forget_memory should succeed");
}

#[tokio::test]
async fn export_memories_returns_the_payload() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/memory/export"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "memories": [],
            "relations": []
        })))
        .mount(&server)
        .await;

    let resp = client
        .export_memories(None)
        .await
        .expect("export_memories should succeed");

    assert!(resp.get("memories").is_some());
}

#[tokio::test]
async fn import_memories_sends_memories_and_relations() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/memory/import"))
        .and(body_json(json!({
            "memories": [{"content": "x"}],
            "relations": []
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "source_session_id": null,
            "memories": [{
                "id": "mem_1",
                "scope": "default",
                "kind": "fact",
                "content": "x",
                "metadata": {},
                "source_session_id": null,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "expires_at": null,
                "source_content": null,
                "document_date": null,
                "event_dates": [],
                "importance": 0.5,
                "access_count": 0,
                "last_accessed_at": null,
                "superseded_at": null,
                "superseded_by_id": null
            }]
        })))
        .mount(&server)
        .await;

    let resp = client
        .import_memories(vec![json!({"content": "x"})], vec![])
        .await
        .expect("import_memories should succeed");

    assert_eq!(resp.memories.len(), 1);
    assert_eq!(resp.memories[0].id, "mem_1");
}
