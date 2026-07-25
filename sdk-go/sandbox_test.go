package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateSandbox_SendsExactRequestBody(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/sandboxes" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Fatalf("decoding request body: %v", err)
		}
		want := map[string]any{
			"label":                "build-job",
			"size":                 "medium",
			"storage_gb":           float64(20),
			"lifetime_minutes":     float64(120),
			"count":                float64(3),
			"secret_names":         []any{"prod-stripe"},
			"mcp_connection_names": []any{"slack-conn"},
			"volume_mounts":        map[string]any{"vol-1": "/data"},
			"gpu_count":            float64(2),
		}
		for key, wantVal := range want {
			gotVal, ok := body[key]
			if !ok {
				t.Errorf("request body missing field %q", key)
				continue
			}
			gotJSON, _ := json.Marshal(gotVal)
			wantJSON, _ := json.Marshal(wantVal)
			if string(gotJSON) != string(wantJSON) {
				t.Errorf("field %q: got %s, want %s", key, gotJSON, wantJSON)
			}
		}
		writeJSON(t, w, 201, `{
			"id": "sess-1", "status": "active", "label": "build-job",
			"created_at": "2026-01-01T00:00:00Z", "destroyed_at": null,
			"expires_at": "2026-01-01T02:00:00Z"
		}`)
	})
	defer closeServer()

	sandbox, err := client.CreateSandbox(context.Background(), CreateSandboxRequest{
		Label:              Ptr("build-job"),
		Size:               Ptr("medium"),
		StorageGB:          Ptr(20.0),
		LifetimeMinutes:    Ptr(120),
		Count:              Ptr(3),
		SecretNames:        []string{"prod-stripe"},
		MCPConnectionNames: []string{"slack-conn"},
		VolumeMounts:       map[string]string{"vol-1": "/data"},
		GPUCount:           Ptr(2),
	})
	if err != nil {
		t.Fatalf("CreateSandbox: %v", err)
	}
	if sandbox.ID != "sess-1" || sandbox.Status != "active" {
		t.Errorf("unexpected sandbox: %+v", sandbox)
	}
}

func TestCreateSandbox_OmitsUnsetOptionalFields(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Fatalf("decoding request body: %v", err)
		}
		if len(body) != 0 {
			t.Errorf("expected an empty request body for a zero-value CreateSandboxRequest, got: %v", body)
		}
		writeJSON(t, w, 201, `{"id": "sess-2", "status": "active", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": null, "expires_at": null}`)
	})
	defer closeServer()

	if _, err := client.CreateSandbox(context.Background(), CreateSandboxRequest{}); err != nil {
		t.Fatalf("CreateSandbox: %v", err)
	}
}

func TestGetSandbox(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"id": "sess-1", "status": "destroyed", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": "2026-01-01T01:00:00Z", "expires_at": null}`)
	})
	defer closeServer()

	sandbox, err := client.GetSandbox(context.Background(), "sess-1")
	if err != nil {
		t.Fatalf("GetSandbox: %v", err)
	}
	if sandbox.Status != "destroyed" {
		t.Errorf("unexpected status: %s", sandbox.Status)
	}
}

func TestListSandboxes_SendsActiveOnlyQueryParam(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("active_only"); got != "true" {
			t.Errorf("expected active_only=true, got %q", got)
		}
		writeJSON(t, w, 200, `[{"id": "sess-1", "status": "active", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": null, "expires_at": null}]`)
	})
	defer closeServer()

	sandboxes, err := client.ListSandboxes(context.Background(), true)
	if err != nil {
		t.Fatalf("ListSandboxes: %v", err)
	}
	if len(sandboxes) != 1 || sandboxes[0].ID != "sess-1" {
		t.Errorf("unexpected sandboxes: %+v", sandboxes)
	}
}

func TestListSandboxes_ReturnsEmptySliceNotNilOnEmptyBody(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[]`)
	})
	defer closeServer()

	sandboxes, err := client.ListSandboxes(context.Background(), false)
	if err != nil {
		t.Fatalf("ListSandboxes: %v", err)
	}
	if sandboxes == nil {
		t.Fatal("expected a non-nil empty slice")
	}
	if len(sandboxes) != 0 {
		t.Errorf("expected zero sandboxes, got %d", len(sandboxes))
	}
}

func TestDestroySandbox(t *testing.T) {
	called := false
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/sandboxes/sess-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.DestroySandbox(context.Background(), "sess-1"); err != nil {
		t.Fatalf("DestroySandbox: %v", err)
	}
	if !called {
		t.Fatal("expected the DELETE request to reach the server")
	}
}

func TestCreateSandboxes_DecodesArrayResponse(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["count"] != float64(2) {
			t.Errorf("unexpected count: %v", body["count"])
		}
		writeJSON(t, w, 201, `[
			{"id": "sess-1", "status": "active", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": null, "expires_at": null},
			{"id": "sess-2", "status": "active", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": null, "expires_at": null}
		]`)
	})
	defer closeServer()

	sandboxes, err := client.CreateSandboxes(context.Background(), CreateSandboxRequest{Count: Ptr(2)})
	if err != nil {
		t.Fatalf("CreateSandboxes: %v", err)
	}
	if len(sandboxes) != 2 || sandboxes[0].ID != "sess-1" || sandboxes[1].ID != "sess-2" {
		t.Errorf("unexpected sandboxes: %+v", sandboxes)
	}
}

func TestDestroySandbox_NotFoundMapsToAPIError(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 404, `{"error": {"code": "not_found", "message": "Session not found."}}`)
	})
	defer closeServer()

	err := client.DestroySandbox(context.Background(), "sess-missing")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "not_found" {
		t.Errorf("unexpected code: %s", apiErr.Code)
	}
}
