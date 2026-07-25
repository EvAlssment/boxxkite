//! GUI/remote-desktop human takeover: `WS /v1/sandboxes/{id}/desktop` -- an
//! interactive, duplex byte stream (VNC) proxied straight through to the
//! sidecar's own `WS /desktop`, structurally identical to `takeover.rs` but
//! bridging a full desktop instead of a shell -- see `docs/API.md` and
//! `SECURITY.md`'s "New trust boundary: remote desktop takeover" section.
//! This client is not a browser, so it uses the same header-based
//! `Authorization: Bearer` path `takeover()` does.

use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use crate::client::{to_ws_url, Client};
use crate::error::BoxkiteError;

/// The duplex byte stream returned by [`Client::desktop_takeover`]. Send and
/// receive raw bytes on it exactly as you would over a local terminal --
/// there is no separate message envelope.
pub type DesktopStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

impl Client {
    /// `WS /v1/sandboxes/{id}/desktop` -- interactive GUI/remote-desktop
    /// human takeover of a sandbox session.
    ///
    /// Reuses [`Client::takeover`]'s RBAC gate as-is: requires an
    /// **`"admin"`-role** API key (see `POST /v1/api-keys`'s `role` field) --
    /// a `"member"`-role key closes the connection with close code `4403`.
    /// There is no dedicated `can_initiate_desktop` permission yet, and no
    /// read-only variant of this connection. A missing/invalid/expired
    /// credential closes with `4401`; an unowned or already-destroyed
    /// `session_id` closes with `4404` -- this deployment closes with `4404`
    /// as well when `BOXKITE_DESKTOP_ENABLED` is unset. All surface as a
    /// [`BoxkiteError::WebSocket`] (or, for a close that happens after a
    /// clean handshake, as a close frame on the returned stream itself --
    /// inspect it if the connection ends unexpectedly).
    pub async fn desktop_takeover(&self, session_id: &str) -> Result<DesktopStream, BoxkiteError> {
        let ws_url = to_ws_url(
            &self.base_url,
            &format!("/v1/sandboxes/{session_id}/desktop"),
        );
        let mut request = ws_url.into_client_request()?;
        let header_value = HeaderValue::from_str(&format!("Bearer {}", self.api_key))
            .map_err(|err| BoxkiteError::Config(format!("invalid api_key: {err}")))?;
        request.headers_mut().insert(AUTHORIZATION, header_value);

        let (stream, _response) = tokio_tungstenite::connect_async(request).await?;
        Ok(stream)
    }
}
