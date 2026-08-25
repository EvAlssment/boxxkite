//! MemoryBase: durable, account-scoped agent memory owned end-to-end by the
//! control plane (no third-party memory API) -- `/v1/memory*`. Mirrors
//! `sdk-python`'s `remember`/`recall`/`ingest_memory`/`memory_profile`/
//! `forget_memory` plus the rest of the REST surface.

use std::collections::HashMap;

use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::client::Client;
use crate::error::BoxxkiteError;

/// One durable, account-scoped memory (`POST/GET /v1/memory`).
#[derive(Debug, Clone, Deserialize)]
pub struct Memory {
    pub id: String,
    pub scope: String,
    pub kind: String,
    pub content: String,
    #[serde(default)]
    pub metadata: HashMap<String, Value>,
    pub source_session_id: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    pub expires_at: Option<String>,
    pub source_content: Option<String>,
    pub document_date: Option<String>,
    #[serde(default)]
    pub event_dates: Vec<String>,
    pub importance: f64,
    pub access_count: i64,
    pub last_accessed_at: Option<String>,
    pub superseded_at: Option<String>,
    pub superseded_by_id: Option<String>,
}

/// A memory returned from [`Client::recall`], with its retrieval score.
#[derive(Debug, Clone, Deserialize)]
pub struct MemorySearchHit {
    #[serde(flatten)]
    pub memory: Memory,
    pub score: f64,
}

/// Response from [`Client::recall`] (`GET /v1/memory/search`).
#[derive(Debug, Clone, Deserialize)]
pub struct MemorySearchResponse {
    pub query: String,
    pub memories: Vec<MemorySearchHit>,
}

/// Response from [`Client::ingest_memory`] and [`Client::import_memories`].
#[derive(Debug, Clone, Deserialize)]
pub struct MemoryIngestResponse {
    pub source_session_id: Option<String>,
    pub memories: Vec<Memory>,
}

/// Response from [`Client::memory_profile`] -- stable, long-lived memories
/// separated from recently-touched ones.
#[derive(Debug, Clone, Deserialize)]
pub struct MemoryProfileResponse {
    pub r#static: Vec<Memory>,
    pub dynamic: Vec<Memory>,
}

/// One edge from [`Client::memory_relations`].
#[derive(Debug, Clone, Deserialize)]
pub struct MemoryRelation {
    pub source_memory_id: String,
    pub target_memory_id: String,
    pub relation_type: String,
    pub confidence: f64,
    pub created_at: String,
}

/// Response from [`Client::memory_relations`] (`GET /v1/memory/{id}/relations`).
#[derive(Debug, Clone, Deserialize)]
pub struct MemoryRelationsResponse {
    pub memory_id: String,
    pub relations: Vec<MemoryRelation>,
}

/// Builder for [`Client::remember`]'s optional fields.
#[derive(Debug, Clone, Default, Serialize)]
pub struct RememberOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    scope: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    metadata: Option<HashMap<String, Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    importance: Option<f64>,
}

impl RememberOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Partitions memories within an account (e.g. per project). Not an
    /// authorization boundary by itself -- every read/search/mutation is
    /// still filtered by the authenticated account. Defaults to
    /// `"default"` server-side when omitted.
    pub fn scope(mut self, scope: impl Into<String>) -> Self {
        self.scope = Some(scope.into());
        self
    }

    /// One of `fact`/`preference`/`goal`/`instruction`/`note`/`summary`.
    /// Defaults to `"fact"` server-side when omitted.
    pub fn kind(mut self, kind: impl Into<String>) -> Self {
        self.kind = Some(kind.into());
        self
    }

    pub fn metadata(mut self, metadata: HashMap<String, Value>) -> Self {
        self.metadata = Some(metadata);
        self
    }

    pub fn source_session_id(mut self, source_session_id: impl Into<String>) -> Self {
        self.source_session_id = Some(source_session_id.into());
        self
    }

    /// `0.0..=1.0`. Defaults to `0.5` server-side when omitted.
    pub fn importance(mut self, importance: f64) -> Self {
        self.importance = Some(importance);
        self
    }
}

/// Builder for [`Client::ingest_memory`]'s optional fields.
#[derive(Debug, Clone, Default, Serialize)]
pub struct IngestMemoryOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    scope: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    metadata: Option<HashMap<String, Value>>,
}

impl IngestMemoryOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn scope(mut self, scope: impl Into<String>) -> Self {
        self.scope = Some(scope.into());
        self
    }

    pub fn source_session_id(mut self, source_session_id: impl Into<String>) -> Self {
        self.source_session_id = Some(source_session_id.into());
        self
    }

    pub fn metadata(mut self, metadata: HashMap<String, Value>) -> Self {
        self.metadata = Some(metadata);
        self
    }
}

/// Options for [`Client::list_memories`].
#[derive(Debug, Clone, Default)]
pub struct ListMemoriesOptions {
    pub scope: Option<String>,
    pub include_superseded: bool,
    pub limit: Option<u32>,
    pub offset: Option<u32>,
}

impl Client {
    /// `POST /v1/memory` -- store one durable, account-scoped memory
    /// directly. See the MemoryBase developer guide for the full model
    /// (opt-in, self-hosted retrieval by default, no third-party memory
    /// API).
    pub async fn remember(
        &self,
        content: &str,
        options: RememberOptions,
    ) -> Result<Memory, BoxxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            content: &'a str,
            #[serde(flatten)]
            options: &'a RememberOptions,
        }
        let builder = self.request(Method::POST, "/v1/memory").json(&Body {
            content,
            options: &options,
        });
        self.send(builder).await
    }

    /// `POST /v1/memory/ingest` -- split a bounded document or conversation
    /// into atomic memories. Works with no model provider configured (the
    /// default extractor is deterministic).
    pub async fn ingest_memory(
        &self,
        content: &str,
        options: IngestMemoryOptions,
    ) -> Result<MemoryIngestResponse, BoxxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            content: &'a str,
            #[serde(flatten)]
            options: &'a IngestMemoryOptions,
        }
        let builder = self.request(Method::POST, "/v1/memory/ingest").json(&Body {
            content,
            options: &options,
        });
        self.send(builder).await
    }

    /// `GET /v1/memory/search` -- rank live account memories against a
    /// query using lexical evidence, phrase match, recency, and importance.
    pub async fn recall(
        &self,
        query: &str,
        scope: Option<&str>,
        limit: Option<u32>,
    ) -> Result<MemorySearchResponse, BoxxkiteError> {
        let mut params = vec![("q".to_string(), query.to_string())];
        params.push(("limit".to_string(), limit.unwrap_or(10).to_string()));
        if let Some(scope) = scope {
            params.push(("scope".to_string(), scope.to_string()));
        }
        let builder = self
            .request(Method::GET, "/v1/memory/search")
            .query(&params);
        self.send(builder).await
    }

    /// `GET /v1/memory/profile` -- stable, long-lived memories separated
    /// from recently-touched ones.
    pub async fn memory_profile(
        &self,
        scope: Option<&str>,
        limit: Option<u32>,
    ) -> Result<MemoryProfileResponse, BoxxkiteError> {
        let mut params = vec![("limit".to_string(), limit.unwrap_or(20).to_string())];
        if let Some(scope) = scope {
            params.push(("scope".to_string(), scope.to_string()));
        }
        let builder = self
            .request(Method::GET, "/v1/memory/profile")
            .query(&params);
        self.send(builder).await
    }

    /// `GET /v1/memory` -- live memories for this account.
    pub async fn list_memories(
        &self,
        options: ListMemoriesOptions,
    ) -> Result<Vec<Memory>, BoxxkiteError> {
        let mut params = vec![
            (
                "include_superseded".to_string(),
                options.include_superseded.to_string(),
            ),
            ("limit".to_string(), options.limit.unwrap_or(50).to_string()),
            (
                "offset".to_string(),
                options.offset.unwrap_or(0).to_string(),
            ),
        ];
        if let Some(scope) = options.scope {
            params.push(("scope".to_string(), scope));
        }
        let builder = self.request(Method::GET, "/v1/memory").query(&params);
        self.send_or_default(builder).await
    }

    /// `GET /v1/memory/{id}` -- one live memory owned by this account.
    pub async fn get_memory(&self, memory_id: &str) -> Result<Memory, BoxxkiteError> {
        let builder = self.request(Method::GET, &format!("/v1/memory/{memory_id}"));
        self.send(builder).await
    }

    /// `GET /v1/memory/{id}/relations` -- other memories related to this
    /// one (updates/extends/related/derives edges).
    pub async fn memory_relations(
        &self,
        memory_id: &str,
        limit: Option<u32>,
    ) -> Result<MemoryRelationsResponse, BoxxkiteError> {
        let builder = self
            .request(Method::GET, &format!("/v1/memory/{memory_id}/relations"))
            .query(&[("limit", limit.unwrap_or(20).to_string())]);
        self.send(builder).await
    }

    /// `DELETE /v1/memory/{id}` -- permanently delete one memory. Not a
    /// soft-delete; a forgotten memory cannot be recalled again.
    pub async fn forget_memory(&self, memory_id: &str) -> Result<(), BoxxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/memory/{memory_id}"));
        self.send_no_content(builder).await
    }

    /// `GET /v1/memory/export` -- export live memories (and their
    /// relations) for this account, for backup or migration.
    pub async fn export_memories(&self, scope: Option<&str>) -> Result<Value, BoxxkiteError> {
        let mut builder = self.request(Method::GET, "/v1/memory/export");
        if let Some(scope) = scope {
            builder = builder.query(&[("scope", scope)]);
        }
        self.send(builder).await
    }

    /// `POST /v1/memory/import` -- import memories (and optionally their
    /// relations) previously produced by [`Client::export_memories`].
    pub async fn import_memories(
        &self,
        memories: Vec<Value>,
        relations: Vec<Value>,
    ) -> Result<MemoryIngestResponse, BoxxkiteError> {
        #[derive(Serialize)]
        struct Body {
            memories: Vec<Value>,
            relations: Vec<Value>,
        }
        let builder = self.request(Method::POST, "/v1/memory/import").json(&Body {
            memories,
            relations,
        });
        self.send(builder).await
    }
}
