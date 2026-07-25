//! Runnable end-to-end example against a real hosted control-plane.
//!
//! ```bash
//! BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
//!     cargo run --example quickstart
//! ```

use std::process::ExitCode;

use boxkite_client::{BoxkiteError, Client, CreateSandboxOptions, ExecOptions};

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

    let sandbox = client
        .create_sandbox(CreateSandboxOptions::new().label("sdk-quickstart"))
        .await?;
    println!("Created sandbox {}", sandbox.id);

    // Best-effort teardown even if a later step fails.
    let result = run_workload(&client, &sandbox.id).await;
    client.destroy_sandbox(&sandbox.id).await?;
    println!("Sandbox destroyed.");

    result
}

async fn run_workload(client: &Client, session_id: &str) -> Result<(), BoxkiteError> {
    let exec_result = client
        .exec(session_id, "python3 -c 'print(1 + 1)'", ExecOptions::new())
        .await?;
    println!("exec result: {}", exec_result.stdout.trim());

    client
        .file_create(
            session_id,
            "hello.txt",
            "hello from boxkite-client\n",
            Default::default(),
        )
        .await?;
    let viewed = client
        .view(session_id, "hello.txt", Default::default())
        .await?;
    println!("file contents: {}", viewed.content.trim());

    Ok(())
}
