package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateMCPConnection_SendsLabelAndCatalogID(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/mcp-connections" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["label"] != "slack-conn" || body["catalog_id"] != "slack" {
			t.Errorf("unexpected body: %v", body)
		}
		writeJSON(t, w, 201, `{"id": "mcp-1", "label": "slack-conn", "catalog_id": "slack", "host": "slack.com", "created_at": "2026-01-01T00:00:00Z", "last_used_at": null}`)
	})
	defer closeServer()

	conn, err := client.CreateMCPConnection(context.Background(), "slack-conn", "slack")
	if err != nil {
		t.Fatalf("CreateMCPConnection: %v", err)
	}
	if conn.ID != "mcp-1" || conn.Host != "slack.com" {
		t.Errorf("unexpected connection: %+v", conn)
	}
}

func TestListMCPConnections(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[]`)
	})
	defer closeServer()

	conns, err := client.ListMCPConnections(context.Background())
	if err != nil {
		t.Fatalf("ListMCPConnections: %v", err)
	}
	if conns == nil || len(conns) != 0 {
		t.Errorf("expected empty non-nil slice, got: %+v", conns)
	}
}

func TestDeleteMCPConnection(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/mcp-connections/mcp-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.DeleteMCPConnection(context.Background(), "mcp-1"); err != nil {
		t.Fatalf("DeleteMCPConnection: %v", err)
	}
}
