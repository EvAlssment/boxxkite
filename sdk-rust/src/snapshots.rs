//! Filesystem snapshots: create/list by source session and get/restore/delete
//! by snapshot id. Restoring creates a new sandbox session.

use std::time::Duration;

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxxkiteError;
use crate::sandboxes::Sandbox;

const SNAPSHOT_COPY_TIMEOUT: Duration = Duration::from_secs(10 * 60);

/// Optional label sent when creating a filesystem snapshot or restoring one.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SnapshotOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    label: Option<String>,
}

impl SnapshotOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn label(mut self, label: impl Into<String>) -> Self {
        self.label = Some(label.into());
        self
    }
}

/// The control-plane lifecycle state for a filesystem snapshot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SnapshotStatus {
    Pending,
    Completed,
    Failed,
}

/// A point-in-time copy of a sandbox's workspace/output filesystem.
#[derive(Debug, Clone, Deserialize)]
pub struct Snapshot {
    pub id: String,
    /// Null after the source sandbox session has been destroyed.
    pub session_id: Option<String>,
    pub label: Option<String>,
    pub status: SnapshotStatus,
    pub storage_key_prefix: String,
    pub size_bytes: i64,
    pub created_at: String,
    pub deleted_at: Option<String>,
}

impl Client {
    /// `POST /v1/sandboxes/{session_id}/snapshots` -- synchronously copy the
    /// session's current filesystem into snapshot storage. The storage copy
    /// can be large, so this request uses a 10-minute timeout instead of the
    /// client's usual 30 seconds.
    pub async fn create_snapshot(
        &self,
        session_id: &str,
        options: SnapshotOptions,
    ) -> Result<Snapshot, BoxxkiteError> {
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/sandboxes/{session_id}/snapshots"),
            )
            .json(&options)
            .timeout(SNAPSHOT_COPY_TIMEOUT);
        self.send(builder).await
    }

    /// `GET /v1/sandboxes/{session_id}/snapshots` -- list snapshots from a
    /// session. This also works after the source session is destroyed.
    pub async fn list_snapshots(&self, session_id: &str) -> Result<Vec<Snapshot>, BoxxkiteError> {
        let builder = self.request(
            Method::GET,
            &format!("/v1/sandboxes/{session_id}/snapshots"),
        );
        self.send_or_default::<Option<Vec<Snapshot>>>(builder)
            .await
            .map(Option::unwrap_or_default)
    }

    /// `GET /v1/snapshots/{snapshot_id}` -- fetch a snapshot by id.
    pub async fn get_snapshot(&self, snapshot_id: &str) -> Result<Snapshot, BoxxkiteError> {
        let builder = self.request(Method::GET, &format!("/v1/snapshots/{snapshot_id}"));
        self.send(builder).await
    }

    /// `POST /v1/snapshots/{snapshot_id}/restore` -- synchronously create a
    /// new sandbox session from a snapshot. Uses the same [`Sandbox`] response
    /// type as [`Client::create_sandbox`] and a 10-minute timeout for the
    /// storage copy.
    pub async fn restore_snapshot(
        &self,
        snapshot_id: &str,
        options: SnapshotOptions,
    ) -> Result<Sandbox, BoxxkiteError> {
        let builder = self
            .request(
                Method::POST,
                &format!("/v1/snapshots/{snapshot_id}/restore"),
            )
            .json(&options)
            .timeout(SNAPSHOT_COPY_TIMEOUT);
        self.send(builder).await
    }

    /// `DELETE /v1/snapshots/{snapshot_id}` -- delete a snapshot and its
    /// stored filesystem copy.
    pub async fn delete_snapshot(&self, snapshot_id: &str) -> Result<(), BoxxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/snapshots/{snapshot_id}"));
        self.send_no_content(builder).await
    }
}
