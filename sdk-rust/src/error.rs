//! Error types returned by every fallible call in this crate.

use std::fmt;

/// Which named failure class an API error belongs to.
///
/// Mirrors `sdk-python`'s `api_error_type`, `sdk-js`'s error subclasses and
/// `sdk-go`'s typed errors so the same `code` classifies identically in every
/// SDK. Rust gets an enum on the `Api` variant rather than separate error
/// types, which is the idiomatic shape here and keeps `BoxxkiteError` a small
/// closed set.
///
/// `Other` is deliberately not an error: a `code` this crate has not seen
/// before still arrives with its status, message and retryable flag intact,
/// so a code added to the control-plane later does not require an SDK release
/// to be usable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum ApiErrorKind {
    QuotaExceeded,
    EgressDenied,
    CapabilityDenied,
    ReadonlyFilesystem,
    SandboxNotReady,
    SandboxCrashed,
    ServiceUnavailable,
    Other,
}

/// Classify an error `code` exactly as the other SDKs do.
pub(crate) fn classify(code: &str, status: u16) -> ApiErrorKind {
    if code.ends_with("_limit_reached") || code.ends_with("_capacity_reached") {
        return ApiErrorKind::QuotaExceeded;
    }
    match code {
        "egress_denied" => ApiErrorKind::EgressDenied,
        "capability_denied" | "command_not_allowed" => ApiErrorKind::CapabilityDenied,
        "readonly_filesystem" => ApiErrorKind::ReadonlyFilesystem,
        "sandbox_not_ready" => ApiErrorKind::SandboxNotReady,
        "sandbox_crashed" => ApiErrorKind::SandboxCrashed,
        "service_unavailable" => ApiErrorKind::ServiceUnavailable,
        _ if status >= 500 => ApiErrorKind::ServiceUnavailable,
        _ => ApiErrorKind::Other,
    }
}

/// Everything this crate's `Result<T, BoxxkiteError>` can fail with.
///
/// Mirrors `sdk-python`'s `BoxxkiteApiError`/`BoxxkiteConnectionError` split
/// (both subclass `BoxxkiteError` there; here both are variants of one enum,
/// the idiomatic Rust shape for a small, closed set of error kinds).
#[derive(Debug, thiserror::Error)]
pub enum BoxxkiteError {
    /// The control-plane responded with a non-2xx status. `code` is the
    /// machine-readable `error.code` field from the response envelope
    /// (`{"error": {"code", "message"}}`) -- see `docs/API.md`'s "Error
    /// codes" table for the full list this API can return.
    #[error("boxxkite API error {status}: {code} - {message}")]
    Api {
        status: u16,
        code: String,
        message: String,
        retryable: bool,
        remediation: Option<String>,
        /// Named failure class for this `code`, classified the same way in
        /// every SDK. See [`ApiErrorKind`].
        kind: ApiErrorKind,
    },

    /// The request never reached the control-plane, or its response
    /// couldn't be read (DNS failure, connection refused, TLS error,
    /// timeout, etc).
    #[error("connection error: {0}")]
    Connection(#[source] reqwest::Error),

    /// A response body that was expected to be well-formed JSON wasn't, or
    /// couldn't be deserialized into the expected shape.
    #[error("failed to decode response body: {0}")]
    Decode(#[source] serde_json::Error),

    /// A WebSocket-based call (`takeover`) failed to connect or errored
    /// mid-stream.
    #[error("websocket error: {0}")]
    WebSocket(#[source] tokio_tungstenite::tungstenite::Error),

    /// The Server-Sent Events stream (`watch`) errored. Boxed: the largest
    /// variant of the underlying `reqwest_eventsource::Error` embeds a full
    /// `reqwest::Response`, which would otherwise make every `BoxxkiteError`
    /// (including cheap ones like `Config`) pay for that size on the stack.
    #[error("event stream error: {0}")]
    EventStream(#[source] Box<reqwest_eventsource::Error>),

    /// A caller-supplied argument was invalid before any request was even
    /// sent -- e.g. a non-`https://` `base_url` that isn't `localhost` (see
    /// `ClientBuilder::build`'s doc comment for why this is rejected rather
    /// than silently sent in cleartext).
    #[error("invalid configuration: {0}")]
    Config(String),
}

impl From<reqwest::Error> for BoxxkiteError {
    fn from(err: reqwest::Error) -> Self {
        BoxxkiteError::Connection(err)
    }
}

impl From<serde_json::Error> for BoxxkiteError {
    fn from(err: serde_json::Error) -> Self {
        BoxxkiteError::Decode(err)
    }
}

impl From<tokio_tungstenite::tungstenite::Error> for BoxxkiteError {
    fn from(err: tokio_tungstenite::tungstenite::Error) -> Self {
        BoxxkiteError::WebSocket(err)
    }
}

impl From<reqwest_eventsource::Error> for BoxxkiteError {
    fn from(err: reqwest_eventsource::Error) -> Self {
        BoxxkiteError::EventStream(Box::new(err))
    }
}

impl BoxxkiteError {
    /// The machine-readable error code from an `Api` variant, if this is
    /// one -- e.g. `"concurrent_sandbox_limit_reached"`. `None` for every
    /// other variant.
    pub fn code(&self) -> Option<&str> {
        match self {
            BoxxkiteError::Api { code, .. } => Some(code),
            _ => None,
        }
    }

    /// The HTTP status code from an `Api` variant, if this is one.
    pub fn status(&self) -> Option<u16> {
        match self {
            BoxxkiteError::Api { status, .. } => Some(*status),
            _ => None,
        }
    }

    /// Named failure class, when this is an API error. `None` for transport,
    /// decode, websocket, event-stream and config errors.
    pub fn kind(&self) -> Option<ApiErrorKind> {
        match self {
            BoxxkiteError::Api { kind, .. } => Some(*kind),
            _ => None,
        }
    }

    pub fn retryable(&self) -> bool {
        match self {
            BoxxkiteError::Api { retryable, .. } => *retryable,
            _ => false,
        }
    }

    pub fn remediation(&self) -> Option<&str> {
        match self {
            BoxxkiteError::Api { remediation, .. } => remediation.as_deref(),
            _ => None,
        }
    }
}

/// Parsed shape of this API's error envelope: `{"error": {"code", "message"}}`.
#[derive(Debug, serde::Deserialize)]
pub(crate) struct ErrorEnvelope {
    pub error: ErrorBody,
}

#[derive(Debug, serde::Deserialize)]
pub(crate) struct ErrorBody {
    #[serde(default = "default_error_code")]
    pub code: String,
    #[serde(default)]
    pub message: Option<String>,
    /// Absent on a control-plane predating the taxonomy. `None` means "not
    /// stated", which `api_error_from_bytes` resolves to `status >= 500` --
    /// matching Python, JS and Go. A plain `#[serde(default)]` would have made
    /// an older 5xx look non-retryable, which is the opposite of the truth.
    #[serde(default)]
    pub retryable: Option<bool>,
    #[serde(default)]
    pub remediation: Option<String>,
}

fn default_error_code() -> String {
    "error".to_string()
}

/// Parse this API's `{"error": {"code", "message"}}` envelope out of a
/// response body, falling back to a generic message if the body isn't (or
/// doesn't contain) that shape. Shared by [`crate::client::Client`]'s
/// regular request path and `watch`'s Server-Sent Events path, which both
/// need to turn a non-2xx response into the same `BoxxkiteError::Api` shape.
pub(crate) fn api_error_from_bytes(status: u16, bytes: &[u8]) -> BoxxkiteError {
    let parsed = serde_json::from_slice::<ErrorEnvelope>(bytes).ok();
    let (code, message, retryable, remediation) = parsed
        .map(|env| {
            (
                env.error.code,
                env.error.message.unwrap_or_default(),
                env.error.retryable.unwrap_or(status >= 500),
                env.error.remediation,
            )
        })
        .unwrap_or_else(|| {
            (
                "error".to_string(),
                format!("HTTP {status}"),
                status >= 500,
                None,
            )
        });
    let kind = classify(&code, status);
    BoxxkiteError::Api {
        status,
        code,
        message,
        retryable,
        remediation,
        kind,
    }
}

impl fmt::Display for ErrorBody {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}: {}",
            self.code,
            self.message.as_deref().unwrap_or("")
        )
    }
}
