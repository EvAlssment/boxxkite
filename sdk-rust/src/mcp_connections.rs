//! Outbound-MCP connections (GitHub issues #116/#117,
//! `docs/OUTBOUND-MCP-DESIGN.md`): `POST/GET/DELETE /v1/mcp-connections*`.
//! Mirrors `sdk-python`'s `create_mcp_connection`/`list_mcp_connections`/
//! `delete_mcp_connection`.
//!
//! Note: granting a connection only widens a session's per-pod
//! `NetworkPolicy` egress allowlist to the catalog hostname -- there is no
//! MCP-proxy transport yet (`docs/OUTBOUND-MCP-DESIGN.md` section 6), so a
//! granted connection does not yet let an agent actually speak MCP to it.

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// A curated outbound-MCP catalog entry -- restricted to boxkite's own
/// reviewed allowlist, never a caller-supplied hostname.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum McpCatalogId {
    Slack,
    Notion,
    Linear,
    Github,
}

/// A granted outbound-MCP connection (`McpConnectionOut`).
#[derive(Debug, Clone, Deserialize)]
pub struct McpConnection {
    pub id: String,
    /// Unique (per-account) name -- pass it to
    /// [`crate::CreateSandboxOptions::mcp_connection_names`] to
    /// grant a session network egress to it.
    pub label: String,
    pub catalog_id: String,
    /// The resolved catalog host, for the caller's own visibility -- never
    /// treated as caller-supplied input on any other route.
    pub host: String,
    pub created_at: String,
    pub last_used_at: Option<String>,
}

impl Client {
    /// `POST /v1/mcp-connections` -- grant this account access to one
    /// curated outbound-MCP catalog entry.
    ///
    /// `label` must be unique per account; `catalog_id` selects the
    /// catalog entry to grant.
    pub async fn create_mcp_connection(
        &self,
        label: &str,
        catalog_id: McpCatalogId,
    ) -> Result<McpConnection, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            label: &'a str,
            catalog_id: McpCatalogId,
        }
        let body = Body { label, catalog_id };
        let builder = self
            .request(Method::POST, "/v1/mcp-connections")
            .json(&body);
        self.send(builder).await
    }

    /// `GET /v1/mcp-connections` -- outbound-MCP connection grants for this account.
    pub async fn list_mcp_connections(&self) -> Result<Vec<McpConnection>, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/mcp-connections");
        self.send_or_default(builder).await
    }

    /// `DELETE /v1/mcp-connections/{id}` -- delete an outbound-MCP
    /// connection grant owned by this account. 404s if already gone or
    /// never owned by this account.
    pub async fn delete_mcp_connection(&self, connection_id: &str) -> Result<(), BoxkiteError> {
        let builder = self.request(
            Method::DELETE,
            &format!("/v1/mcp-connections/{connection_id}"),
        );
        self.send_no_content(builder).await
    }
}
