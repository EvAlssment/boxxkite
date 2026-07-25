//! Org-scoped secrets for the proxy-substitution secrets broker
//! (`docs/SECRETS-DESIGN.md`): `POST/GET/DELETE /v1/secrets*`. Mirrors
//! `sdk-python`'s `create_secret`/`list_secrets`/`delete_secret`.
//!
//! The raw value is write-only: accepted on [`Client::create_secret`],
//! never returned by any route here (list/create-response both omit it).

use reqwest::Method;
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;

/// Builder for `POST /v1/secrets`'s optional fields.
#[derive(Debug, Clone, Default, Serialize)]
pub struct CreateSecretOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    trust_tier: Option<String>,
}

impl CreateSecretOptions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Only meaningful for wallet/private-key-style secrets
    /// (`docs/WALLET-SECRETS-DESIGN.md`) -- omit for an ordinary
    /// API-key-style secret. The only accepted value today is
    /// `"testnet"`; `"mainnet"` is refused (422).
    pub fn trust_tier(mut self, trust_tier: impl Into<String>) -> Self {
        self.trust_tier = Some(trust_tier.into());
        self
    }
}

/// A secret's metadata (`SecretOut`/`SecretCreatedResponse`). The raw value
/// is never included here or anywhere else after creation.
#[derive(Debug, Clone, Deserialize)]
pub struct Secret {
    pub id: String,
    pub name: String,
    pub allowed_hosts: Vec<String>,
    pub trust_tier: Option<String>,
    pub created_at: String,
    pub last_used_at: Option<String>,
}

impl Client {
    /// `POST /v1/secrets` -- register a new org-scoped secret.
    ///
    /// `name` must be unique per account; `value` is the real credential
    /// value, write-only (never returned by this or any other route);
    /// `allowed_hosts` are the destination hostnames this secret may be
    /// used against via `http_request` -- required, not optional, since an
    /// unscoped secret usable against any destination defeats the point of
    /// this feature. A host that resolves to a private/link-local/
    /// loopback/metadata address is rejected at creation time (a
    /// best-effort backstop; see `docs/SECRETS-DESIGN.md` section 5 for why
    /// the real control is the sidecar's request-time check).
    pub async fn create_secret(
        &self,
        name: &str,
        value: &str,
        allowed_hosts: &[String],
        options: CreateSecretOptions,
    ) -> Result<Secret, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            name: &'a str,
            value: &'a str,
            allowed_hosts: &'a [String],
            #[serde(skip_serializing_if = "Option::is_none")]
            trust_tier: Option<&'a str>,
        }
        let body = Body {
            name,
            value,
            allowed_hosts,
            trust_tier: options.trust_tier.as_deref(),
        };
        let builder = self.request(Method::POST, "/v1/secrets").json(&body);
        self.send(builder).await
    }

    /// `GET /v1/secrets` -- secrets registered for this account. Raw values
    /// are never returned here.
    pub async fn list_secrets(&self) -> Result<Vec<Secret>, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/secrets");
        self.send_or_default(builder).await
    }

    /// `DELETE /v1/secrets/{id}` -- delete a secret owned by this account.
    /// 404s if already gone or never owned by this account.
    pub async fn delete_secret(&self, secret_id: &str) -> Result<(), BoxkiteError> {
        let builder = self.request(Method::DELETE, &format!("/v1/secrets/{secret_id}"));
        self.send_no_content(builder).await
    }
}
