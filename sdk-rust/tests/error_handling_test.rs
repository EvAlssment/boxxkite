use boxkite_client::{BoxkiteError, Client, ExecOptions};

#[tokio::test]
async fn unreachable_server_yields_connection_error() {
    // Port 0 is never a real listener -- nothing will ever accept this
    // connection, so it deterministically exercises the transport-error
    // path rather than a wiremock-served response.
    let client = Client::new("http://127.0.0.1:0", "bxk_live_test").expect("valid client config");

    let err = client
        .exec("sess_1", "echo hi", ExecOptions::new())
        .await
        .unwrap_err();
    assert!(matches!(err, BoxkiteError::Connection(_)));
    assert_eq!(err.code(), None);
    assert_eq!(err.status(), None);
}

#[test]
fn config_error_rejected_before_any_request_is_built() {
    let err = Client::new("ftp://cp.example.com", "bxk_live_test").unwrap_err();
    assert!(matches!(err, BoxkiteError::Config(_)));
    assert!(err.to_string().contains("cleartext") || err.to_string().contains("invalid"));
}
