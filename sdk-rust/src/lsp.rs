//! Agent-invokable language-server completions: `POST /v1/sandboxes/{id}/lsp/start`
//! and its `/{lsp_id}/open`, `/{lsp_id}/completion`, `/{lsp_id}/stop`
//! siblings. Mirrors `sdk-python`'s `lsp_start`/`lsp_open`/`lsp_completion`/
//! `lsp_stop` (`sdk-go` does not wrap these).

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// `POST /v1/sandboxes/{id}/lsp/start`'s response. `lsp_id` is an opaque
/// handle to pass to the other LSP calls.
#[derive(Debug, Clone, Deserialize)]
pub struct LspStartResult {
    pub lsp_id: String,
}

/// `POST /v1/sandboxes/{id}/lsp/{lsp_id}/open`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct LspOpenResult {
    pub status: String,
}

/// `POST /v1/sandboxes/{id}/lsp/{lsp_id}/completion`'s response. Each item is
/// a raw LSP `CompletionItem` object -- left as raw JSON rather than an
/// over-fitted struct, since the shape is the language server's, not this
/// API's (matching how `ls`/`glob` leave their entries untyped).
#[derive(Debug, Clone, Deserialize)]
pub struct LspCompletionResult {
    #[serde(default)]
    pub items: Vec<serde_json::Value>,
}

/// `POST /v1/sandboxes/{id}/lsp/{lsp_id}/stop`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct LspStopResult {
    pub status: String,
}

impl Client {
    /// `POST /v1/sandboxes/{session_id}/lsp/start` -- start a persistent
    /// language server (`pyright` for `"python"`,
    /// `typescript-language-server` for `"typescript"`/`"javascript"`).
    /// Returns the opaque `lsp_id` handle for the other LSP calls.
    pub async fn lsp_start(
        &self,
        session_id: &str,
        language: &str,
    ) -> Result<LspStartResult, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            language: &'a str,
        }
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/lsp/start"),
            )
            .json(&Body { language });
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{session_id}/lsp/{lsp_id}/open` -- open (or
    /// full-document-replace) a document on a running language server.
    pub async fn lsp_open(
        &self,
        session_id: &str,
        lsp_id: &str,
        path: &str,
        content: &str,
    ) -> Result<LspOpenResult, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            path: &'a str,
            content: &'a str,
        }
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/lsp/{lsp_id}/open"),
            )
            .json(&Body { path, content });
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{session_id}/lsp/{lsp_id}/completion` -- request
    /// completions at a 0-indexed `(line, character)` position. `path` must
    /// already be open on this handle (see [`Client::lsp_open`]).
    pub async fn lsp_completion(
        &self,
        session_id: &str,
        lsp_id: &str,
        path: &str,
        line: u32,
        character: u32,
    ) -> Result<LspCompletionResult, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            path: &'a str,
            line: u32,
            character: u32,
        }
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/lsp/{lsp_id}/completion"),
            )
            .json(&Body {
                path,
                line,
                character,
            });
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{session_id}/lsp/{lsp_id}/stop` -- gracefully shut
    /// down a running language server.
    pub async fn lsp_stop(
        &self,
        session_id: &str,
        lsp_id: &str,
    ) -> Result<LspStopResult, BoxkiteError> {
        let builder = self.request(
            Method::POST,
            &format!("/v1/sandboxes/{session_id}/lsp/{lsp_id}/stop"),
        );
        self.send(builder).await
    }
}
