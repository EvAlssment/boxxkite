//! [`Client`]/[`ClientBuilder`] and the shared request plumbing every other
//! module in this crate builds on. No behavior lives here beyond
//! request/response handling -- mirrors `sdk-python`'s `BoxkiteClient`/
//! `sdk-js`'s `BoxkiteClient` in spirit (thin HTTP wrapper, same v1 API).

use std::time::Duration;

use reqwest::{header, Method, RequestBuilder, StatusCode};
use serde::de::DeserializeOwned;

use crate::error::{api_error_from_bytes, BoxkiteError};

const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);
/// Extra headroom added on top of a caller-supplied `exec`/`http_request`
/// `timeout` so the HTTP client's own request timeout never fires first and
/// masks the server's own timeout response -- same constant sdk-python's
/// `EXEC_TIMEOUT_HEADROOM` / sdk-js's `EXEC_TIMEOUT_HEADROOM_MS` add.
pub(crate) const EXEC_TIMEOUT_HEADROOM: Duration = Duration::from_secs(15);

const LOCALHOST_HOSTNAMES: [&str; 3] = ["localhost", "127.0.0.1", "::1"];

/// A client for a hosted boxkite control-plane's `/v1/*` REST API.
///
/// Construct one with [`Client::new`] for the common case, or
/// [`Client::builder`] for control over the timeout or the underlying
/// `reqwest::Client` (e.g. to point it at a test server). Cheap to clone --
/// clone freely instead of wrapping in `Arc` yourself.
#[derive(Clone, Debug)]
pub struct Client {
    pub(crate) http: reqwest::Client,
    pub(crate) base_url: String,
    pub(crate) api_key: String,
    pub(crate) retry: RetryConfig,
}

impl Client {
    /// Shorthand for `Client::builder().base_url(base_url).api_key(api_key).build()`.
    pub fn new(
        base_url: impl Into<String>,
        api_key: impl Into<String>,
    ) -> Result<Self, BoxkiteError> {
        ClientBuilder::new()
            .base_url(base_url)
            .api_key(api_key)
            .build()
    }

    /// Start building a [`Client`] with non-default options (timeout, a
    /// preconfigured `reqwest::Client`, etc).
    pub fn builder() -> ClientBuilder {
        ClientBuilder::new()
    }

    pub(crate) fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    pub(crate) fn request(&self, method: Method, path: &str) -> RequestBuilder {
        self.http
            .request(method, self.url(path))
            .bearer_auth(&self.api_key)
    }

    /// Like [`Client::request`], but authenticates with a caller-supplied
    /// bearer token instead of this client's `api_key` -- for the one route
    /// (`resend_verification`) that takes a dashboard-session JWT, a
    /// different, non-interchangeable credential type from an account API
    /// key.
    pub(crate) fn request_with_auth(
        &self,
        method: Method,
        path: &str,
        token: &str,
    ) -> RequestBuilder {
        self.http.request(method, self.url(path)).bearer_auth(token)
    }

    /// Send a built request through the configured retry policy. Every
    /// [`Client::send`]/[`send_or_default`](Client::send_or_default)/
    /// [`send_no_content`](Client::send_no_content) call funnels through here,
    /// so retry (when enabled via [`ClientBuilder::retry`]) applies uniformly.
    /// Streaming paths (`watch`/`takeover`/`desktop`) build their own
    /// requests and are deliberately not routed through here.
    async fn execute_with_retry(
        &self,
        builder: RequestBuilder,
    ) -> Result<reqwest::Response, BoxkiteError> {
        let request = builder.build().map_err(BoxkiteError::from)?;

        // A non-clonable body (streaming) or a disabled policy can't/shouldn't
        // be retried -- send once and return whatever happens.
        if self.retry.max_retries == 0 || request.try_clone().is_none() {
            return self.http.execute(request).await.map_err(BoxkiteError::from);
        }

        let idempotent = is_idempotent(request.method());
        let mut attempt: u32 = 0;
        loop {
            let this = request
                .try_clone()
                .expect("request body was clonable at the pre-loop check");
            match self.http.execute(this).await {
                Ok(resp) => {
                    let status = resp.status();
                    if attempt < self.retry.max_retries && should_retry_status(status, idempotent) {
                        let retry_after = parse_retry_after(resp.headers());
                        let delay = self.retry.backoff(attempt, retry_after);
                        attempt += 1;
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Ok(resp);
                }
                Err(err) => {
                    let transient = err.is_timeout() || err.is_connect() || err.is_request();
                    if attempt < self.retry.max_retries && idempotent && transient {
                        let delay = self.retry.backoff(attempt, None);
                        attempt += 1;
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(BoxkiteError::from(err));
                }
            }
        }
    }

    /// Send a request and deserialize a JSON response body. Non-2xx
    /// responses become `BoxkiteError::Api`; the body is expected to
    /// contain valid JSON of shape `T` on success.
    pub(crate) async fn send<T: DeserializeOwned>(
        &self,
        builder: RequestBuilder,
    ) -> Result<T, BoxkiteError> {
        let resp = self.execute_with_retry(builder).await?;
        let status = resp.status();
        let bytes = resp.bytes().await?;
        if !status.is_success() {
            return Err(api_error_from_bytes(status.as_u16(), &bytes));
        }
        serde_json::from_slice(&bytes).map_err(BoxkiteError::from)
    }

    /// Like [`Client::send`], but for endpoints that may return an empty
    /// (`204`, or otherwise body-less) success response -- e.g. `list_*`
    /// endpoints that return `null` instead of `[]` when nothing exists.
    /// Returns the JSON-decoded default value (e.g. an empty `Vec`) when the
    /// body is empty rather than trying to decode nothing as `T`.
    pub(crate) async fn send_or_default<T: DeserializeOwned + Default>(
        &self,
        builder: RequestBuilder,
    ) -> Result<T, BoxkiteError> {
        let resp = self.execute_with_retry(builder).await?;
        let status = resp.status();
        let bytes = resp.bytes().await?;
        if !status.is_success() {
            return Err(api_error_from_bytes(status.as_u16(), &bytes));
        }
        if bytes.is_empty() {
            return Ok(T::default());
        }
        serde_json::from_slice(&bytes).map_err(BoxkiteError::from)
    }

    /// Send a request expecting no meaningful response body (`204 No
    /// Content`, e.g. `destroy_sandbox`/`delete_image`).
    pub(crate) async fn send_no_content(
        &self,
        builder: RequestBuilder,
    ) -> Result<(), BoxkiteError> {
        let resp = self.execute_with_retry(builder).await?;
        let status = resp.status();
        if !status.is_success() {
            let bytes = resp.bytes().await?;
            return Err(api_error_from_bytes(status.as_u16(), &bytes));
        }
        Ok(())
    }
}

/// Builder for [`Client`]. `base_url` and `api_key` are required; everything
/// else has a sensible default.
#[derive(Default)]
pub struct ClientBuilder {
    base_url: Option<String>,
    api_key: Option<String>,
    timeout: Option<Duration>,
    http_client: Option<reqwest::Client>,
    retry: Option<RetryConfig>,
}

impl ClientBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    /// The control-plane's base URL, e.g. `https://cp.example.com`. Must be
    /// `https://`, unless the host is `localhost`/`127.0.0.1`/`::1` (local
    /// dev only) -- every request sends `Authorization: Bearer <api_key>`,
    /// a full-privilege, long-lived account credential, so an `http://` URL
    /// to anything else would put it on the wire in cleartext. Trailing
    /// slashes are stripped.
    pub fn base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = Some(base_url.into());
        self
    }

    /// A boxkite account API key (`bxk_live_...`).
    pub fn api_key(mut self, api_key: impl Into<String>) -> Self {
        self.api_key = Some(api_key.into());
        self
    }

    /// Per-request timeout. Defaults to 30 seconds. Ignored if
    /// [`ClientBuilder::http_client`] is also set -- configure the timeout
    /// on that client instead.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    /// Supply a preconfigured `reqwest::Client` instead of letting this
    /// builder construct one -- e.g. one pointed at a `wiremock` mock
    /// server in tests, or with custom TLS/proxy settings. Authentication
    /// is still applied per-request via `api_key` regardless.
    pub fn http_client(mut self, http_client: reqwest::Client) -> Self {
        self.http_client = Some(http_client);
        self
    }

    /// Opt in to automatic retry with exponential backoff for transient
    /// failures. Off by default -- a call fails on the first non-2xx or
    /// transport error unless a policy is set here. See [`RetryConfig`] for
    /// what is (and isn't) retried, and [`RetryConfig::default`] for the
    /// sensible defaults [`ClientBuilder::max_retries`] applies.
    pub fn retry(mut self, retry: RetryConfig) -> Self {
        self.retry = Some(retry);
        self
    }

    /// Shorthand for [`ClientBuilder::retry`] with the default backoff
    /// (500ms base, 30s cap, full jitter, honors `Retry-After`) and just the
    /// retry count changed. `max_retries(0)` disables retries (the default).
    pub fn max_retries(mut self, max_retries: u32) -> Self {
        self.retry = Some(RetryConfig {
            max_retries,
            ..RetryConfig::default()
        });
        self
    }

    /// Validate the configuration and construct the [`Client`].
    ///
    /// # Errors
    /// [`BoxkiteError::Config`] if `base_url`/`api_key` weren't set, or if
    /// `base_url` isn't a valid `https://` (or localhost-only `http://`) URL.
    pub fn build(self) -> Result<Client, BoxkiteError> {
        let base_url = self
            .base_url
            .ok_or_else(|| BoxkiteError::Config("base_url is required".to_string()))?;
        let api_key = self
            .api_key
            .ok_or_else(|| BoxkiteError::Config("api_key is required".to_string()))?;
        validate_base_url_scheme(&base_url)?;
        let base_url = base_url.trim_end_matches('/').to_string();

        let http = match self.http_client {
            Some(client) => client,
            None => reqwest::Client::builder()
                .timeout(self.timeout.unwrap_or(DEFAULT_TIMEOUT))
                .build()
                .map_err(BoxkiteError::from)?,
        };

        Ok(Client {
            http,
            base_url,
            api_key,
            retry: self.retry.unwrap_or_else(RetryConfig::disabled),
        })
    }
}

/// Automatic-retry policy for a [`Client`], set via [`ClientBuilder::retry`].
///
/// Retries are **opt-in** (a client built without one never retries) and
/// deliberately conservative about which failures are safe to repeat:
/// - `429 Too Many Requests` is retried for **any** method (the server
///   rejected the request before acting on it; `Retry-After` is honored when
///   present).
/// - `5xx` responses and transport errors (timeout/connect/request) are
///   retried only for **idempotent** methods (`GET`/`PUT`/`DELETE`/`HEAD`/
///   `OPTIONS`/`TRACE`) -- never for a bare `POST`, which may have partially
///   applied server-side.
/// - `4xx` other than `429` is never retried.
///
/// Delay grows as `base_delay * 2^attempt`, capped at `max_delay`. With
/// `jitter` on (the default) the actual sleep is a uniform random draw in
/// `[0, delay]` ("full jitter"), spreading retries so a fleet of clients
/// doesn't stampede a recovering server in lockstep.
#[derive(Clone, Debug)]
pub struct RetryConfig {
    /// Maximum number of *additional* attempts after the first. `0` disables
    /// retrying entirely.
    pub max_retries: u32,
    /// First-retry delay; doubles each subsequent attempt.
    pub base_delay: Duration,
    /// Upper bound on any single computed delay (also caps a large
    /// `Retry-After`).
    pub max_delay: Duration,
    /// Apply full jitter to each delay.
    pub jitter: bool,
    /// Honor a `Retry-After` response header (seconds form) in place of the
    /// computed backoff, still capped by `max_delay`.
    pub respect_retry_after: bool,
}

impl Default for RetryConfig {
    fn default() -> Self {
        Self {
            max_retries: 3,
            base_delay: Duration::from_millis(500),
            max_delay: Duration::from_secs(30),
            jitter: true,
            respect_retry_after: true,
        }
    }
}

impl RetryConfig {
    /// A policy that never retries -- the [`Client`] default.
    pub fn disabled() -> Self {
        Self {
            max_retries: 0,
            ..Self::default()
        }
    }

    /// The default backoff (500ms base, 30s cap, full jitter, honors
    /// `Retry-After`) with a custom retry count.
    pub fn new(max_retries: u32) -> Self {
        Self {
            max_retries,
            ..Self::default()
        }
    }

    /// Delay before the retry numbered `attempt` (0-indexed: `attempt == 0`
    /// is the first retry). `retry_after` overrides the computed backoff when
    /// present and `respect_retry_after` is set.
    fn backoff(&self, attempt: u32, retry_after: Option<Duration>) -> Duration {
        if self.respect_retry_after {
            if let Some(after) = retry_after {
                return after.min(self.max_delay);
            }
        }
        let factor = 2u32.saturating_pow(attempt);
        let raw = self.base_delay.saturating_mul(factor).min(self.max_delay);
        if self.jitter {
            raw.mul_f64(random_fraction())
        } else {
            raw
        }
    }
}

fn is_idempotent(method: &Method) -> bool {
    matches!(
        *method,
        Method::GET | Method::PUT | Method::DELETE | Method::HEAD | Method::OPTIONS | Method::TRACE
    )
}

fn should_retry_status(status: StatusCode, idempotent: bool) -> bool {
    if status == StatusCode::TOO_MANY_REQUESTS {
        return true;
    }
    status.is_server_error() && idempotent
}

fn parse_retry_after(headers: &header::HeaderMap) -> Option<Duration> {
    // Only the delta-seconds form is handled; the HTTP-date form falls back
    // to the computed backoff (a bounded overshoot at worst, never a hang).
    let raw = headers.get(header::RETRY_AFTER)?.to_str().ok()?;
    raw.trim().parse::<u64>().ok().map(Duration::from_secs)
}

/// A random `f64` in `[0, 1)` for full-jitter backoff, without pulling in a
/// `rand` dependency: `RandomState` seeds a fresh SipHash keypair per
/// instance, so `finish()` differs run to run. Jitter tolerates a
/// weak/biased source -- this is not, and must not be relied on as, a CSPRNG.
fn random_fraction() -> f64 {
    use std::hash::{BuildHasher, Hasher};
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u8(0);
    (hasher.finish() as f64) / (u64::MAX as f64 + 1.0)
}

fn validate_base_url_scheme(base_url: &str) -> Result<(), BoxkiteError> {
    let parsed = url::Url::parse(base_url)
        .map_err(|err| BoxkiteError::Config(format!("invalid base_url {base_url:?}: {err}")))?;
    match parsed.scheme() {
        "https" => Ok(()),
        "http"
            if parsed
                .host_str()
                .is_some_and(|h| LOCALHOST_HOSTNAMES.contains(&h)) =>
        {
            Ok(())
        }
        _ => Err(BoxkiteError::Config(format!(
            "refusing to use non-https base_url {base_url:?}: this would send your API key in \
             cleartext. Use an https:// URL, or http://localhost (local dev only)."
        ))),
    }
}

/// `https://` -> `wss://`, `http://` -> `ws://`. `base_url` has already
/// passed [`validate_base_url_scheme`], so it's always one of those two.
pub(crate) fn to_ws_url(base_url: &str, path: &str) -> String {
    if let Some(rest) = base_url.strip_prefix("https://") {
        format!("wss://{rest}{path}")
    } else if let Some(rest) = base_url.strip_prefix("http://") {
        format!("ws://{rest}{path}")
    } else {
        // Unreachable in practice -- validate_base_url_scheme already
        // rejects every other scheme before a Client can be constructed.
        format!("{base_url}{path}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_plain_http_to_a_remote_host() {
        let err = Client::builder()
            .base_url("http://cp.example.com")
            .api_key("bxk_live_test")
            .build()
            .unwrap_err();
        assert!(matches!(err, BoxkiteError::Config(_)));
    }

    #[test]
    fn allows_https() {
        assert!(Client::builder()
            .base_url("https://cp.example.com")
            .api_key("bxk_live_test")
            .build()
            .is_ok());
    }

    #[test]
    fn allows_http_localhost_for_local_dev() {
        assert!(Client::builder()
            .base_url("http://localhost:8090")
            .api_key("bxk_live_test")
            .build()
            .is_ok());
    }

    #[test]
    fn allows_http_127_0_0_1_for_local_dev() {
        assert!(Client::builder()
            .base_url("http://127.0.0.1:8090")
            .api_key("bxk_live_test")
            .build()
            .is_ok());
    }

    #[test]
    fn strips_trailing_slash() {
        let client = Client::builder()
            .base_url("https://cp.example.com/")
            .api_key("k")
            .build()
            .unwrap();
        assert_eq!(client.base_url, "https://cp.example.com");
    }

    #[test]
    fn requires_base_url_and_api_key() {
        assert!(matches!(
            Client::builder().api_key("k").build().unwrap_err(),
            BoxkiteError::Config(_)
        ));
        assert!(matches!(
            Client::builder()
                .base_url("https://cp.example.com")
                .build()
                .unwrap_err(),
            BoxkiteError::Config(_)
        ));
    }

    #[test]
    fn to_ws_url_converts_scheme() {
        assert_eq!(
            to_ws_url("https://cp.example.com", "/x"),
            "wss://cp.example.com/x"
        );
        assert_eq!(
            to_ws_url("http://localhost:8090", "/x"),
            "ws://localhost:8090/x"
        );
    }

    #[test]
    fn retry_is_disabled_by_default() {
        let client = Client::new("https://cp.example.com", "k").unwrap();
        assert_eq!(client.retry.max_retries, 0);
    }

    #[test]
    fn max_retries_builder_sets_default_backoff() {
        let client = Client::builder()
            .base_url("https://cp.example.com")
            .api_key("k")
            .max_retries(4)
            .build()
            .unwrap();
        assert_eq!(client.retry.max_retries, 4);
        assert!(client.retry.jitter);
        assert!(client.retry.respect_retry_after);
    }

    #[test]
    fn only_429_and_idempotent_5xx_retry() {
        assert!(should_retry_status(StatusCode::TOO_MANY_REQUESTS, false));
        assert!(should_retry_status(StatusCode::TOO_MANY_REQUESTS, true));
        assert!(should_retry_status(StatusCode::SERVICE_UNAVAILABLE, true));
        assert!(!should_retry_status(StatusCode::SERVICE_UNAVAILABLE, false));
        assert!(!should_retry_status(StatusCode::BAD_REQUEST, true));
        assert!(!should_retry_status(StatusCode::NOT_FOUND, true));
    }

    #[test]
    fn post_is_not_idempotent() {
        assert!(!is_idempotent(&Method::POST));
        assert!(is_idempotent(&Method::GET));
        assert!(is_idempotent(&Method::PUT));
        assert!(is_idempotent(&Method::DELETE));
    }

    #[test]
    fn backoff_grows_and_caps_at_max_delay() {
        let cfg = RetryConfig {
            max_retries: 10,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(4),
            jitter: false,
            respect_retry_after: true,
        };
        assert_eq!(cfg.backoff(0, None), Duration::from_secs(1));
        assert_eq!(cfg.backoff(1, None), Duration::from_secs(2));
        assert_eq!(cfg.backoff(2, None), Duration::from_secs(4));
        // 2^3 = 8s exceeds the 4s cap.
        assert_eq!(cfg.backoff(3, None), Duration::from_secs(4));
        // A huge attempt number saturates rather than overflowing.
        assert_eq!(cfg.backoff(64, None), Duration::from_secs(4));
    }

    #[test]
    fn retry_after_overrides_backoff_but_is_capped() {
        let cfg = RetryConfig {
            max_delay: Duration::from_secs(10),
            respect_retry_after: true,
            jitter: false,
            ..RetryConfig::default()
        };
        assert_eq!(
            cfg.backoff(0, Some(Duration::from_secs(3))),
            Duration::from_secs(3)
        );
        assert_eq!(
            cfg.backoff(0, Some(Duration::from_secs(999))),
            Duration::from_secs(10)
        );
    }

    #[test]
    fn parse_retry_after_reads_seconds_form_only() {
        let mut headers = header::HeaderMap::new();
        headers.insert(header::RETRY_AFTER, "5".parse().unwrap());
        assert_eq!(parse_retry_after(&headers), Some(Duration::from_secs(5)));

        let mut date_headers = header::HeaderMap::new();
        date_headers.insert(
            header::RETRY_AFTER,
            "Wed, 21 Oct 2026 07:28:00 GMT".parse().unwrap(),
        );
        assert_eq!(parse_retry_after(&date_headers), None);
    }
}
