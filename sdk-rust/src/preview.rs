//! Network-ingress preview URLs (`docs/NETWORK-INGRESS-DESIGN.md`):
//! `POST /v1/sandboxes/{id}/preview/{port}` and its `/revoke` sibling.
//! Mirrors `sdk-python`'s `create_preview_url`/`revoke_preview_url` and
//! `sdk-go`'s `CreatePreviewURL`/`RevokePreviewURL`.

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// A signed, time-limited preview URL (`POST /v1/sandboxes/{id}/preview/{port}`'s
/// response). The URL carries its own authorization -- no API key is needed
/// to use it, only to mint it.
#[derive(Debug, Clone, Deserialize)]
pub struct PreviewUrl {
    pub url: String,
    pub expires_at: String,
    pub token_id: String,
}

/// `POST /v1/sandboxes/{id}/preview/{port}/revoke`'s response.
#[derive(Debug, Clone, Deserialize)]
pub struct PreviewRevokeResult {
    pub revoked: bool,
    pub token_id: String,
}

impl Client {
    /// `POST /v1/sandboxes/{session_id}/preview/{port}` -- mint a signed,
    /// time-limited URL that proxies HTTP traffic to a port a background
    /// process opened inside this session (see
    /// [`StartProcessOptions::expose_port`](crate::StartProcessOptions::expose_port)).
    ///
    /// `ttl_seconds` bounds how long the minted URL stays valid (30-86400);
    /// defaults to 900 (15 minutes) server-side when `None`.
    pub async fn create_preview_url(
        &self,
        session_id: &str,
        port: u16,
        ttl_seconds: Option<u32>,
    ) -> Result<PreviewUrl, BoxkiteError> {
        #[derive(Serialize)]
        struct Body {
            #[serde(skip_serializing_if = "Option::is_none")]
            ttl_seconds: Option<u32>,
        }
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/preview/{port}"),
            )
            .json(&Body { ttl_seconds });
        self.send(builder).await
    }

    /// `POST /v1/sandboxes/{session_id}/preview/{port}/revoke` -- invalidate
    /// one specific preview-URL token (its `token_id` from
    /// [`Client::create_preview_url`]) before its TTL expires, without
    /// tearing down the session and without affecting any other token minted
    /// for the same session/port. Idempotent: revoking an already-revoked,
    /// already-expired, or unrecognized `token_id` still returns
    /// `revoked = true` rather than erroring.
    pub async fn revoke_preview_url(
        &self,
        session_id: &str,
        port: u16,
        token_id: &str,
    ) -> Result<PreviewRevokeResult, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            token_id: &'a str,
        }
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/preview/{port}/revoke"),
            )
            .json(&Body { token_id });
        self.send(builder).await
    }
}
