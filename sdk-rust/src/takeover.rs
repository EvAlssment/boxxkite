//! Human takeover: `WS /v1/sandboxes/{id}/takeover` -- an interactive,
//! duplex byte stream proxied straight through to the sandbox's PTY, no
//! message envelope. Mirrors `sdk-python`'s `takeover()` (which
//! authenticates the WebSocket upgrade with a normal `Authorization: Bearer`
//! header, unlike the JS SDK/dashboard, which cannot set a custom header on
//! a browser WebSocket and must mint a short-lived takeover token first --
//! see `docs/API.md` and `SECURITY.md`'s "Human takeover" section). This
//! client is not a browser, so it uses the same header-based path as
//! `sdk-python`.

use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use crate::client::{to_ws_url, Client};
use crate::error::BoxkiteError;

/// The duplex byte stream returned by [`Client::takeover`]. Send and
/// receive raw bytes on it exactly as you would over a local terminal --
/// there is no separate message envelope.
pub type TakeoverStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

impl Client {
    /// `WS /v1/sandboxes/{id}/takeover` -- interactive human takeover of a
    /// sandbox session's shell.
    ///
    /// Requires an **`"admin"`-role** API key (see `POST /v1/api-keys`'s
    /// `role` field) -- a `"member"`-role key closes the connection with
    /// close code `4403`. A missing/invalid/expired credential closes with
    /// `4401`; an unowned or already-destroyed `session_id` closes with
    /// `4404`. All three surface as a [`BoxkiteError::WebSocket`] (or, for a
    /// close that happens after a clean handshake, as a close frame on the
    /// returned stream itself -- inspect it if the connection ends
    /// unexpectedly).
    pub async fn takeover(&self, session_id: &str) -> Result<TakeoverStream, BoxkiteError> {
        let ws_url = to_ws_url(
            &self.base_url,
            &format!("/v1/sandboxes/{session_id}/takeover"),
        );
        let mut request = ws_url.into_client_request()?;
        let header_value = HeaderValue::from_str(&format!("Bearer {}", self.api_key))
            .map_err(|err| BoxkiteError::Config(format!("invalid api_key: {err}")))?;
        request.headers_mut().insert(AUTHORIZATION, header_value);

        let (stream, _response) = tokio_tungstenite::connect_async(request).await?;
        Ok(stream)
    }
}
