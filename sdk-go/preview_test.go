package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreatePreviewURL_SendsTTLSeconds(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/preview/3000" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["ttl_seconds"] != float64(1800) {
			t.Errorf("unexpected ttl_seconds: %v", body["ttl_seconds"])
		}
		writeJSON(t, w, 200, `{"url": "/v1/sandboxes/sess-1/preview/3000/?token=abc", "expires_at": "2026-01-01T00:30:00Z", "token_id": "tok-1"}`)
	})
	defer closeServer()

	preview, err := client.CreatePreviewURL(context.Background(), "sess-1", 3000, Ptr(1800))
	if err != nil {
		t.Fatalf("CreatePreviewURL: %v", err)
	}
	if preview.TokenID != "tok-1" {
		t.Errorf("unexpected preview: %+v", preview)
	}
}

func TestRevokePreviewURL_SendsTokenID(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/preview/3000/revoke" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["token_id"] != "tok-1" {
			t.Errorf("unexpected token_id: %v", body["token_id"])
		}
		writeJSON(t, w, 200, `{"revoked": true, "token_id": "tok-1"}`)
	})
	defer closeServer()

	result, err := client.RevokePreviewURL(context.Background(), "sess-1", 3000, "tok-1")
	if err != nil {
		t.Fatalf("RevokePreviewURL: %v", err)
	}
	if !result.Revoked {
		t.Errorf("expected revoked=true, got: %+v", result)
	}
}
