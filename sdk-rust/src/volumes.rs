//! Independent, PVC-backed storage volumes: `POST/GET/DELETE /v1/volumes*`.
//! Mirrors `sdk-python`'s `create_volume`/`get_volume`/`list_volumes`/
//! `delete_volume`.

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// Builder for `POST /v1/volumes`'s optional fields.
#[derive(Debug, Clone, Default, Serialize)]
pub struct CreateVolumeOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    label: Option<String>,
}

impl CreateVolumeOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn label(mut self, label: impl Into<String>) -> Self {
        self.label = Some(label.into());
        self
    }
}

/// An independent, PVC-backed storage volume (`VolumeOut`/`VolumeAccepted`).
/// [`Client::create_volume`]'s response only ever populates `id`/`label`/
/// `status`/`created_at` -- poll [`Client::get_volume`] for `status` to
/// reach `"ready"` (or `"failed"`) before mounting it via
/// [`crate::CreateSandboxOptions::volume_mounts`].
#[derive(Debug, Clone, Deserialize)]
pub struct Volume {
    pub id: String,
    pub label: Option<String>,
    #[serde(default)]
    pub size_gb: f64,
    /// `"queued"`, `"creating"`, `"ready"`, `"failed"`, or `"deleting"`.
    pub status: String,
    pub pvc_name: Option<String>,
    pub failure_reason: Option<String>,
    pub created_at: String,
}

impl Client {
    /// `POST /v1/volumes` -- create an independent storage volume. Returns
    /// immediately with a pending record; poll [`Client::get_volume`] for
    /// `status` before mounting it into a sandbox.
    ///
    /// `size_gb` is required (max 1024).
    pub async fn create_volume(
        &self,
        size_gb: f64,
        options: CreateVolumeOptions,
    ) -> Result<Volume, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            size_gb: f64,
            #[serde(skip_serializing_if = "Option::is_none")]
            label: Option<&'a str>,
        }
        let body = Body {
            size_gb,
            label: options.label.as_deref(),
        };
        let builder = self.request(Method::POST, "/v1/volumes").json(&body);
        self.send(builder).await
    }

    /// `GET /v1/volumes/{id}` -- a storage volume's status and details.
    pub async fn get_volume(&self, volume_id: &str) -> Result<Volume, BoxkiteError> {
        let builder = self.request(Method::GET, &format!("/v1/volumes/{volume_id}"));
        self.send(builder).await
    }

    /// `GET /v1/volumes` -- every storage volume created for this account.
    pub async fn list_volumes(&self) -> Result<Vec<Volume>, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/volumes");
        self.send_or_default(builder).await
    }

    /// `DELETE /v1/volumes/{id}` -- delete a storage volume.
    pub async fn delete_volume(&self, volume_id: &str) -> Result<(), BoxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/volumes/{volume_id}"));
        self.send_no_content(builder).await
    }
}
