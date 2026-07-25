package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateImage_SendsExactRequestBody(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/images" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["base"] != "boxkite-minimal" {
			t.Errorf("unexpected base: %v", body["base"])
		}
		pythonPackages, _ := body["python_packages"].([]any)
		if len(pythonPackages) != 2 || pythonPackages[0] != "polars==1.9.0" {
			t.Errorf("unexpected python_packages: %v", body["python_packages"])
		}
		writeJSON(t, w, 202, `{
			"id": "img-1", "label": "data-eng", "base": "boxkite-minimal",
			"python_packages": ["polars==1.9.0", "duckdb==1.1.3"], "apt_packages": [], "npm_packages": [],
			"status": "queued", "digest": null, "registry_ref": null, "scan_result": null,
			"failure_reason": null, "created_at": "2026-01-01T00:00:00Z", "completed_at": null
		}`)
	})
	defer closeServer()

	image, err := client.CreateImage(context.Background(), CreateImageRequest{
		Label:          Ptr("data-eng"),
		Base:           "boxkite-minimal",
		PythonPackages: []string{"polars==1.9.0", "duckdb==1.1.3"},
	})
	if err != nil {
		t.Fatalf("CreateImage: %v", err)
	}
	if image.ID != "img-1" || image.Status != "queued" {
		t.Errorf("unexpected image: %+v", image)
	}
}

func TestCreateImage_FeatureDisabledMapsToAPIError(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 404, `{"error": {"code": "feature_disabled", "message": "Image builder not enabled."}}`)
	})
	defer closeServer()

	_, err := client.CreateImage(context.Background(), CreateImageRequest{})
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "feature_disabled" {
		t.Errorf("unexpected code: %s", apiErr.Code)
	}
}

func TestGetImage(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/images/img-1" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{
			"id": "img-1", "label": null, "base": "boxkite-default", "python_packages": [], "apt_packages": [], "npm_packages": [],
			"status": "completed", "digest": "sha256:abc", "registry_ref": "registry.internal/img-1@sha256:abc",
			"scan_result": {"critical": 0, "high": 0}, "failure_reason": null,
			"created_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:05:00Z"
		}`)
	})
	defer closeServer()

	image, err := client.GetImage(context.Background(), "img-1")
	if err != nil {
		t.Fatalf("GetImage: %v", err)
	}
	if image.Status != "completed" || image.Digest == nil || *image.Digest != "sha256:abc" {
		t.Errorf("unexpected image: %+v", image)
	}
}

func TestListImages(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[]`)
	})
	defer closeServer()

	images, err := client.ListImages(context.Background())
	if err != nil {
		t.Fatalf("ListImages: %v", err)
	}
	if images == nil || len(images) != 0 {
		t.Errorf("expected empty non-nil slice, got %+v", images)
	}
}

func TestDeleteImage(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/images/img-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.DeleteImage(context.Background(), "img-1"); err != nil {
		t.Fatalf("DeleteImage: %v", err)
	}
}
