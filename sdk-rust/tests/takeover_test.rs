//! `takeover()` opens a raw WebSocket, so it can't be exercised through
//! `wiremock` (HTTP-only). Instead this spins up a bare `tokio` TCP
//! listener and performs the WebSocket handshake by hand with
//! `tokio-tungstenite`'s server-side helpers, asserting the client sent the
//! `Authorization: Bearer <api_key>` header exactly the way `sdk-python`'s
//! `takeover()` does (never a query-string token -- see
//! `SECURITY.md`'s "Human takeover" section).

use boxkite_client::Client;
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::Message;

// tokio-tungstenite's `Callback` trait fixes this closure's return type to
// `Result<Response, ErrorResponse>` -- the large `Err` variant is dictated by
// that external API, not a size choice made in this crate's own code.
#[allow(clippy::result_large_err)]
#[tokio::test]
async fn takeover_sends_bearer_authorization_header_and_bridges_bytes() {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind local test listener");
    let addr = listener.local_addr().expect("local addr");

    let server_task = tokio::spawn(async move {
        let (tcp_stream, _) = listener.accept().await.expect("accept connection");

        let mut seen_auth_header = None;
        let callback = |req: &Request, response: Response| {
            seen_auth_header = req
                .headers()
                .get("Authorization")
                .and_then(|value| value.to_str().ok())
                .map(|value| value.to_string());
            Ok(response)
        };

        let mut ws_stream = tokio_tungstenite::accept_hdr_async(tcp_stream, callback)
            .await
            .expect("server-side handshake should succeed");

        ws_stream
            .send(Message::text("hello from sandbox pty"))
            .await
            .expect("send greeting");

        seen_auth_header
    });

    let base_url = format!("http://{addr}");
    let client = Client::new(base_url, "bxk_live_test").expect("valid client config");

    let mut takeover_stream = client
        .takeover("sess_1")
        .await
        .expect("takeover should connect");
    let first_message = takeover_stream
        .next()
        .await
        .expect("should receive a message")
        .expect("message ok");
    assert_eq!(first_message.into_text().unwrap(), "hello from sandbox pty");

    let seen_auth_header = server_task.await.expect("server task should not panic");
    assert_eq!(seen_auth_header.as_deref(), Some("Bearer bxk_live_test"));
}
