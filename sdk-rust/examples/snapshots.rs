//! Run with:
//! ```bash
//! BOXXKITE_BASE_URL=https://your-control-plane BOXXKITE_API_KEY=bxk_live_... \
//!     cargo run --example snapshots
//! ```

use std::process::ExitCode;

use boxxkite_client::{BoxxkiteError, Client, CreateSandboxOptions, SnapshotOptions};

#[tokio::main]
async fn main() -> ExitCode {
    let (Ok(base_url), Ok(api_key)) = (
        std::env::var("BOXXKITE_BASE_URL"),
        std::env::var("BOXXKITE_API_KEY"),
    ) else {
        eprintln!("Set BOXXKITE_BASE_URL and BOXXKITE_API_KEY first.");
        return ExitCode::FAILURE;
    };

    match run(base_url, api_key).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::FAILURE
        }
    }
}

async fn run(base_url: String, api_key: String) -> Result<(), BoxxkiteError> {
    let client = Client::new(base_url, api_key)?;
    let source = client
        .create_sandbox(CreateSandboxOptions::new().label("snapshot-source"))
        .await?;
    let snapshot = client
        .create_snapshot(&source.id, SnapshotOptions::new().label("before-change"))
        .await?;
    println!("Created snapshot {} ({:?})", snapshot.id, snapshot.status);

    let restored = client
        .restore_snapshot(&snapshot.id, SnapshotOptions::new().label("restored-copy"))
        .await?;
    println!("Restored sandbox {}", restored.id);

    client.delete_snapshot(&snapshot.id).await?;
    client.destroy_sandbox(&restored.id).await?;
    client.destroy_sandbox(&source.id).await?;
    Ok(())
}
