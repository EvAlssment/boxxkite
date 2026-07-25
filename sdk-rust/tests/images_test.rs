mod common;

use boxkite_client::{CreateImageOptions, ImageBase};
use serde_json::json;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, ResponseTemplate};

#[tokio::test]
async fn create_image_sends_base_and_packages() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/images"))
        .and(body_json(json!({
            "label": "data-science",
            "base": "boxkite-default",
            "python_packages": ["polars==1.9.0"],
            "apt_packages": ["ripgrep==14.1.0-1"]
        })))
        .respond_with(ResponseTemplate::new(202).set_body_json(json!({
            "id": "img_1", "label": "data-science", "status": "queued", "created_at": "2026-01-01T00:00:00Z"
        })))
        .mount(&server)
        .await;

    let options = CreateImageOptions::new()
        .label("data-science")
        .base(ImageBase::BoxkiteDefault)
        .python_packages(["polars==1.9.0"])
        .apt_packages(["ripgrep==14.1.0-1"]);

    let image = client
        .create_image(options)
        .await
        .expect("create_image should succeed");
    assert_eq!(image.id, "img_1");
    assert_eq!(image.status, "queued");
    // Fields absent from the "accepted" response default rather than erroring.
    assert!(image.python_packages.is_empty());
}

#[tokio::test]
async fn create_image_node_base_serializes_as_boxkite_node() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/images"))
        .and(body_json(
            json!({"base": "boxkite-node", "npm_packages": ["typescript==5.6.0"]}),
        ))
        .respond_with(ResponseTemplate::new(202).set_body_json(json!({
            "id": "img_2", "status": "queued", "created_at": "2026-01-01T00:00:00Z"
        })))
        .mount(&server)
        .await;

    let options = CreateImageOptions::new()
        .base(ImageBase::BoxkiteNode)
        .npm_packages(["typescript==5.6.0"]);
    client
        .create_image(options)
        .await
        .expect("create_image should succeed");
}

#[tokio::test]
async fn get_image_parses_full_detail() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/images/img_1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "img_1", "label": "data-science", "base": "boxkite-default",
            "python_packages": ["polars==1.9.0"], "apt_packages": [], "npm_packages": [],
            "status": "completed", "digest": "sha256:abc", "registry_ref": "registry/img:abc",
            "scan_result": {"clean": true}, "failure_reason": null,
            "created_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:05:00Z"
        })))
        .mount(&server)
        .await;

    let image = client
        .get_image("img_1")
        .await
        .expect("get_image should succeed");
    assert_eq!(image.status, "completed");
    assert_eq!(image.digest.as_deref(), Some("sha256:abc"));
    assert_eq!(image.python_packages, vec!["polars==1.9.0"]);
}

#[tokio::test]
async fn list_images_returns_empty_vec_on_no_content() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("GET"))
        .and(path("/v1/images"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let images = client
        .list_images()
        .await
        .expect("list_images should succeed");
    assert!(images.is_empty());
}

#[tokio::test]
async fn delete_image_sends_delete() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("DELETE"))
        .and(path("/v1/images/img_1"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    client
        .delete_image("img_1")
        .await
        .expect("delete_image should succeed");
}

#[tokio::test]
async fn create_image_maps_feature_disabled_404() {
    let server = common::mock_server().await;
    let client = common::client_for(&server);

    Mock::given(method("POST"))
        .and(path("/v1/images"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({
            "error": {"code": "feature_disabled", "message": "Custom images are not enabled on this deployment."}
        })))
        .mount(&server)
        .await;

    let err = client
        .create_image(CreateImageOptions::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("feature_disabled"));
}
