//! Account/usage introspection, dashboard auth-flow helpers, and the
//! per-account command allowlist. Mirrors `sdk-python`'s `account`/`usage`/
//! `request_password_reset`/`confirm_password_reset`/`verify_email`/
//! `resend_verification`/`refresh_token`/`logout`/`get_allowed_commands`/
//! `set_allowed_commands`/`clear_allowed_commands` and `sdk-go`'s
//! same-named methods.

use reqwest::Method;
use serde::de::{self, Deserializer};
use serde::{Deserialize, Serialize};

use crate::client::Client;
use crate::error::BoxkiteError;
use crate::sandboxes::UsageSummary;

/// The account identity for the API key in use (`GET /v1/account`).
#[derive(Debug, Clone, Deserialize)]
pub struct Account {
    pub id: String,
    pub email: String,
    pub created_at: String,
}

/// A generic ack body (e.g. a password-reset request, which returns the same
/// message whether or not the email is registered).
#[derive(Debug, Clone, Deserialize)]
pub struct MessageResponse {
    pub message: String,
}

/// A fresh `access_token` + `refresh_token` pair plus the account identity,
/// returned by [`Client::refresh_token`].
#[derive(Debug, Clone, Deserialize)]
pub struct TokenPair {
    pub access_token: String,
    pub token_type: String,
    pub expires_in: i64,
    pub refresh_token: Option<String>,
    pub account: Account,
}

/// One account-level command-allowlist rule -- either a bare command name
/// (`args_allow`/`args_deny` both empty) or a command name plus argument
/// allow/deny regex lists.
///
/// The control-plane accepts and returns rules in either shape (a bare JSON
/// string or a `{command, args_allow?, args_deny?}` object); this type's
/// `Deserialize` decodes both, while its `Serialize` always emits the object
/// form (which the control-plane accepts identically). Mirrors `sdk-go`'s
/// `AllowedCommandRule` custom marshaling.
#[derive(Debug, Clone, Serialize)]
pub struct AllowedCommandRule {
    pub command: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub args_allow: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub args_deny: Vec<String>,
}

impl AllowedCommandRule {
    /// A bare rule: just a command name, no argument constraints.
    pub fn new(command: impl Into<String>) -> Self {
        Self {
            command: command.into(),
            args_allow: Vec::new(),
            args_deny: Vec::new(),
        }
    }

    pub fn args_allow<I, S>(mut self, patterns: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.args_allow = patterns.into_iter().map(Into::into).collect();
        self
    }

    pub fn args_deny<I, S>(mut self, patterns: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.args_deny = patterns.into_iter().map(Into::into).collect();
        self
    }
}

impl<'de> Deserialize<'de> for AllowedCommandRule {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Raw {
            Bare(String),
            Full {
                command: String,
                #[serde(default)]
                args_allow: Vec<String>,
                #[serde(default)]
                args_deny: Vec<String>,
            },
        }
        match Raw::deserialize(deserializer).map_err(de::Error::custom)? {
            Raw::Bare(command) => Ok(Self::new(command)),
            Raw::Full {
                command,
                args_allow,
                args_deny,
            } => Ok(Self {
                command,
                args_allow,
                args_deny,
            }),
        }
    }
}

/// Body shape shared by [`Client::get_allowed_commands`]/
/// [`Client::set_allowed_commands`]. An empty `rules` means unrestricted --
/// the default for every account.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct AllowedCommandsResponse {
    #[serde(default)]
    pub rules: Vec<AllowedCommandRule>,
}

impl Client {
    /// `GET /v1/account` -- the account identity for the API key in use.
    pub async fn account(&self) -> Result<Account, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/account");
        self.send(builder).await
    }

    /// `GET /v1/usage` -- current usage against this account's fair-use
    /// limits (the same shape returned inline on [`Client::create_sandbox`]).
    pub async fn usage(&self) -> Result<UsageSummary, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/usage");
        self.send(builder).await
    }

    /// `POST /v1/auth/password-reset/request` -- request a password-reset
    /// email. Opt-in server-side (`BOXKITE_PASSWORD_RESET_ENABLED`); 404s
    /// with code `feature_disabled` if the deployment hasn't enabled it.
    /// Always returns the same message whether or not the email is
    /// registered, so it can't be used to enumerate accounts.
    pub async fn request_password_reset(
        &self,
        email: &str,
    ) -> Result<MessageResponse, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            email: &'a str,
        }
        let builder = self
            .request(Method::POST, "/v1/auth/password-reset/request")
            .json(&Body { email });
        self.send(builder).await
    }

    /// `POST /v1/auth/password-reset/confirm` -- consume a single-use token
    /// from [`Client::request_password_reset`] and set a new password. Also
    /// revokes every outstanding refresh token for the account, if refresh
    /// tokens are enabled server-side.
    pub async fn confirm_password_reset(
        &self,
        token: &str,
        new_password: &str,
    ) -> Result<MessageResponse, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            token: &'a str,
            new_password: &'a str,
        }
        let builder = self
            .request(Method::POST, "/v1/auth/password-reset/confirm")
            .json(&Body {
                token,
                new_password,
            });
        self.send(builder).await
    }

    /// `POST /v1/auth/verify-email` -- consume a single-use email-verification
    /// token. Opt-in server-side (`BOXKITE_EMAIL_VERIFICATION_ENABLED`).
    pub async fn verify_email(&self, token: &str) -> Result<MessageResponse, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            token: &'a str,
        }
        let builder = self
            .request(Method::POST, "/v1/auth/verify-email")
            .json(&Body { token });
        self.send(builder).await
    }

    /// `POST /v1/auth/resend-verification` -- re-send the verification email
    /// for the dashboard-JWT-authenticated account. `access_token` is a
    /// dashboard session token (the JWT returned by `/v1/auth/login` or
    /// `/v1/auth/signup`) -- a different, non-interchangeable credential type
    /// from this client's own `api_key`, so it overrides this call's
    /// `Authorization` header rather than using the client's key.
    pub async fn resend_verification(
        &self,
        access_token: &str,
    ) -> Result<MessageResponse, BoxkiteError> {
        let builder =
            self.request_with_auth(Method::POST, "/v1/auth/resend-verification", access_token);
        self.send(builder).await
    }

    /// `POST /v1/auth/refresh` -- exchange a still-valid refresh token for a
    /// brand new `access_token` + `refresh_token` pair. Opt-in server-side
    /// (`BOXKITE_REFRESH_TOKENS_ENABLED`). Revokes the presented token in the
    /// same request (rotation, not reuse) -- store the new `refresh_token`
    /// from the response and discard the one presented here.
    pub async fn refresh_token(&self, refresh_token: &str) -> Result<TokenPair, BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            refresh_token: &'a str,
        }
        let builder = self
            .request(Method::POST, "/v1/auth/refresh")
            .json(&Body { refresh_token });
        self.send(builder).await
    }

    /// `POST /v1/auth/logout` -- revoke one refresh token immediately. Opt-in
    /// server-side (`BOXKITE_REFRESH_TOKENS_ENABLED`). Always succeeds (204)
    /// whether or not the token was valid -- never leaks which.
    pub async fn logout(&self, refresh_token: &str) -> Result<(), BoxkiteError> {
        #[derive(Serialize)]
        struct Body<'a> {
            refresh_token: &'a str,
        }
        let builder = self
            .request(Method::POST, "/v1/auth/logout")
            .json(&Body { refresh_token });
        self.send_no_content(builder).await
    }

    /// `GET /v1/account/allowed-commands` -- the current per-account command
    /// allowlist. An empty `rules` means unrestricted (the default). This
    /// allowlist is an opt-in guardrail, not a sandbox-escape boundary.
    pub async fn get_allowed_commands(&self) -> Result<AllowedCommandsResponse, BoxkiteError> {
        let builder = self.request(Method::GET, "/v1/account/allowed-commands");
        self.send(builder).await
    }

    /// `PUT /v1/account/allowed-commands` -- replace the per-account command
    /// allowlist wholesale. `rules` must be non-empty; use
    /// [`Client::clear_allowed_commands`] to reset to unrestricted.
    pub async fn set_allowed_commands(
        &self,
        rules: Vec<AllowedCommandRule>,
    ) -> Result<AllowedCommandsResponse, BoxkiteError> {
        let body = AllowedCommandsResponse { rules };
        let builder = self
            .request(Method::PUT, "/v1/account/allowed-commands")
            .json(&body);
        self.send(builder).await
    }

    /// `DELETE /v1/account/allowed-commands` -- remove the per-account
    /// command allowlist, back to the unrestricted default.
    pub async fn clear_allowed_commands(&self) -> Result<(), BoxkiteError> {
        let builder = self.request(Method::DELETE, "/v1/account/allowed-commands");
        self.send_no_content(builder).await
    }
}
