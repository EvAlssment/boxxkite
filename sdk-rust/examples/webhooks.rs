//! Runnable end-to-end example against a real hosted control-plane.
//!
//! ```bash
//! BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
//!     cargo run --example webhooks
//! ```

use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use sha2::Sha256;

use boxkite_client::{BoxkiteError, Client, CreateWebhookOptions, WebhookEventType};

type HmacSha256 = Hmac<Sha256>;

/// Verify an `X-Boxkite-Webhook-Signature` header, per docs/WEBHOOKS-DESIGN.md §6.
fn verify_signature(
    secret: &str,
    signature_header: &str,
    raw_body: &[u8],
    tolerance_seconds: u64,
) -> bool {
    let mut timestamp: Option<u64> = None;
    let mut signature: Option<&str> = None;
    for part in signature_header.split(',') {
        if let Some(value) = part.strip_prefix("t=") {
            timestamp = value.parse().ok();
        } else if let Some(value) = part.strip_prefix("v1=") {
            signature = Some(value);
        }
    }
    let (Some(timestamp), Some(signature)) = (timestamp, signature) else {
        return false;
    };

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before UNIX epoch")
        .as_secs();
    if now.abs_diff(timestamp) > tolerance_seconds {
        return false;
    }

    let Ok(signature_bytes) = hex::decode(signature) else {
        return false;
    };
    let Ok(mut mac) = HmacSha256::new_from_slice(secret.as_bytes()) else {
        return false;
    };
    mac.update(format!("{timestamp}.").as_bytes());
    mac.update(raw_body);
    mac.verify_slice(&signature_bytes).is_ok()
}

#[tokio::main]
async fn main() -> ExitCode {
    let Ok(base_url) = std::env::var("BOXKITE_BASE_URL") else {
        eprintln!("Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.");
        return ExitCode::FAILURE;
    };
    let Ok(api_key) = std::env::var("BOXKITE_API_KEY") else {
        eprintln!("Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.");
        return ExitCode::FAILURE;
    };

    match run(base_url, api_key).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(BoxkiteError::Api { code, message, .. }) => {
            eprintln!("API error: {message} [{code}]");
            ExitCode::FAILURE
        }
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::FAILURE
        }
    }
}

async fn run(base_url: String, api_key: String) -> Result<(), BoxkiteError> {
    let client = Client::new(base_url, api_key)?;

    let webhook = client
        .create_webhook(
            "https://example.com/boxkite-webhook",
            &[
                WebhookEventType::SandboxCreated,
                WebhookEventType::SandboxDestroyed,
                WebhookEventType::AuditLogEntry,
            ],
            CreateWebhookOptions::new().description("webhooks example"),
        )
        .await?;
    println!("Created webhook {}", webhook.id);
    let secret = webhook
        .secret
        .expect("secret is populated on create_webhook's response");
    println!("Signing secret (shown once, save it now): {secret}");

    // Simulate a delivery to prove verify_signature works, without a real
    // receiver: sign a synthetic payload locally with the just-printed
    // secret, then verify it the same way a receiver would.
    let raw_body = br#"{"event_type":"sandbox.created","event_id":"evt_demo"}"#;
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before UNIX epoch")
        .as_secs();
    let mut mac =
        HmacSha256::new_from_slice(secret.as_bytes()).expect("HMAC accepts any key length");
    mac.update(format!("{timestamp}.").as_bytes());
    mac.update(raw_body);
    let signature = hex::encode(mac.finalize().into_bytes());
    let signature_header = format!("t={timestamp},v1={signature}");

    let is_valid = verify_signature(&secret, &signature_header, raw_body, 300);
    println!("Locally signed payload verifies: {is_valid}");

    client.delete_webhook(&webhook.id).await?;
    println!("Webhook deleted.");

    Ok(())
}
