//! Shared test helpers: spin up a `wiremock` mock control-plane and build a
//! `Client` pointed at it -- mirrors `sdk-python`'s `_client_with(handler)`
//! using `httpx.MockTransport`, but against a real local HTTP server
//! instead of an in-process transport shim.

use std::time::Duration;

use boxkite_client::{Client, RetryConfig};
use wiremock::MockServer;

pub const TEST_API_KEY: &str = "bxk_live_test";

pub async fn mock_server() -> MockServer {
    MockServer::start().await
}

pub fn client_for(server: &MockServer) -> Client {
    Client::new(server.uri(), TEST_API_KEY).expect("valid test client config")
}

/// A client pointed at `server` with retries enabled and near-zero delays so
/// retry tests don't actually sleep for seconds.
#[allow(dead_code)]
pub fn client_for_with_retry(server: &MockServer, max_retries: u32) -> Client {
    Client::builder()
        .base_url(server.uri())
        .api_key(TEST_API_KEY)
        .retry(RetryConfig {
            max_retries,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(5),
            jitter: false,
            respect_retry_after: false,
        })
        .build()
        .expect("valid test client config")
}
