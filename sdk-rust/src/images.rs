//! Declarative builder (custom sandbox images): `POST/GET/DELETE
//! /v1/images*`. Mirrors `sdk-python`'s `create_image`/`get_image`/
//! `list_images`/`delete_image`.

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// Pre-approved base image for [`Client::create_image`]. Each variant is
/// itself a separate, boxkite-maintained hardened image -- never a
/// caller-supplied arbitrary base OS.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum ImageBase {
    /// The full data-science/document/browser stack.
    #[serde(rename = "boxkite-default")]
    BoxkiteDefault,
    /// A lean Python+Node base with nothing preinstalled.
    #[serde(rename = "boxkite-minimal")]
    BoxkiteMinimal,
    /// Drops Python entirely -- no `python_packages` installable.
    #[serde(rename = "boxkite-node")]
    BoxkiteNode,
    /// Drops both Python and Node entirely -- no `python_packages` or
    /// `npm_packages` installable.
    #[serde(rename = "boxkite-go")]
    BoxkiteGo,
}

/// Builder for `POST /v1/images`'s optional fields.
#[derive(Debug, Clone, Default, Serialize)]
pub struct CreateImageOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    base: Option<ImageBase>,
    #[serde(skip_serializing_if = "Option::is_none")]
    python_packages: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    apt_packages: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    npm_packages: Option<Vec<String>>,
}

impl CreateImageOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn label(mut self, label: impl Into<String>) -> Self {
        self.label = Some(label.into());
        self
    }

    /// Defaults to [`ImageBase::BoxkiteDefault`] server-side when omitted.
    pub fn base(mut self, base: ImageBase) -> Self {
        self.base = Some(base);
        self
    }

    /// Exact-version-pinned packages (`"name==version"`, no ranges), e.g.
    /// `"polars==1.9.0"`. Not supported on [`ImageBase::BoxkiteNode`] or
    /// [`ImageBase::BoxkiteGo`].
    pub fn python_packages<I, S>(mut self, packages: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.python_packages = Some(packages.into_iter().map(Into::into).collect());
        self
    }

    /// Exact-version-pinned apt/apk packages (`"name==version"`, no ranges).
    pub fn apt_packages<I, S>(mut self, packages: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.apt_packages = Some(packages.into_iter().map(Into::into).collect());
        self
    }

    /// Exact-version-pinned npm packages (`"name==version"`, no ranges), e.g.
    /// `"typescript==5.6.0"`. Not supported on [`ImageBase::BoxkiteGo`].
    pub fn npm_packages<I, S>(mut self, packages: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.npm_packages = Some(packages.into_iter().map(Into::into).collect());
        self
    }
}

/// A custom sandbox image (`SandboxImageOut`/`SandboxImageBuildAccepted`).
/// [`Client::create_image`]'s response only ever populates `id`/`label`/
/// `status`/`created_at` -- the rest fill in once you poll
/// [`Client::get_image`] and the build reaches a terminal state.
#[derive(Debug, Clone, Deserialize)]
pub struct Image {
    pub id: String,
    pub label: Option<String>,
    #[serde(default)]
    pub base: String,
    #[serde(default)]
    pub python_packages: Vec<String>,
    #[serde(default)]
    pub apt_packages: Vec<String>,
    #[serde(default)]
    pub npm_packages: Vec<String>,
    /// `"queued"`, `"building"`, `"scanning"`, `"completed"`, `"failed"`, or
    /// `"rejected"`.
    pub status: String,
    pub digest: Option<String>,
    pub registry_ref: Option<String>,
    pub scan_result: Option<serde_json::Value>,
    pub failure_reason: Option<String>,
    pub created_at: String,
    pub completed_at: Option<String>,
}

impl Client {
    /// `POST /v1/images` -- build a custom sandbox image (a base image plus
    /// exact-version-pinned Python/apt/npm packages). Returns immediately
    /// with a pending record; poll [`Client::get_image`] for `status` to
    /// reach a terminal state.
    pub async fn create_image(&self, options: CreateImageOptions) -> Result<Image, BoxkiteError> {
        let builder = self.request(Method::POST, "/v1/images").json(&options);
        self.send(builder).await
    }

    /// `GET /v1/images/{id}` -- a custom sandbox image's build status and details.
    pub async fn get_image(&self, image_id: &str) -> Result<Image, BoxkiteError> {
        let builder = self.request(Method::GET, &format!("/v1/images/{image_id}"));
        self.send(builder).await
    }

    /// `GET /v1/images` -- every custom sandbox image built for this account.
    pub async fn list_images(&self) -> Result<Vec<Image>, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/images");
        self.send_or_default(builder).await
    }

    /// `DELETE /v1/images/{id}` -- delete a custom sandbox image.
    pub async fn delete_image(&self, image_id: &str) -> Result<(), BoxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/images/{image_id}"));
        self.send_no_content(builder).await
    }
}
