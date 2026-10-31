package boxxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
	"time"
)

func TestCreateSnapshotUsesGenerousTimeoutAndDecodesResponse(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/sandboxes/session-1/snapshots" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		var request map[string]any
		_ = json.Unmarshal(body, &request)
		if request["label"] != "before-change" {
			t.Errorf("unexpected body: %v", request)
		}
		time.Sleep(20 * time.Millisecond)
		writeJSON(t, w, 201, `{"id":"snap-1","session_id":"session-1","label":"before-change","status":"completed","storage_key_prefix":"snapshots/acct/snap-1","size_bytes":42,"created_at":"2026-10-31T00:00:00Z","deleted_at":null}`)
	})
	defer closeServer()
	client.timeout = time.Nanosecond
	client.httpClient.Timeout = time.Nanosecond

	session := &Session{client: client, ID: "session-1"}
	snapshot, err := session.CreateSnapshot(context.Background(), CreateSnapshotRequest{Label: Ptr("before-change")})
	if err != nil {
		t.Fatalf("CreateSnapshot: %v", err)
	}
	if snapshot.ID != "snap-1" || snapshot.Status != SnapshotStatusCompleted || snapshot.SessionID == nil || *snapshot.SessionID != "session-1" {
		t.Errorf("unexpected snapshot: %+v", snapshot)
	}
}

func TestSessionSnapshotMethodsAndNullableSessionIDStatuses(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/sandboxes/session-1/snapshots" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		writeJSON(t, w, 200, `[
			{"id":"snap-pending","session_id":"session-1","status":"pending","storage_key_prefix":"snapshots/a/1","size_bytes":0,"created_at":"2026-10-31T00:00:00Z","deleted_at":null},
			{"id":"snap-completed","session_id":null,"status":"completed","storage_key_prefix":"snapshots/a/2","size_bytes":42,"created_at":"2026-10-31T00:00:00Z","deleted_at":null},
			{"id":"snap-failed","session_id":null,"status":"failed","storage_key_prefix":"snapshots/a/3","size_bytes":0,"created_at":"2026-10-31T00:00:00Z","deleted_at":null}
		]`)
	})
	defer closeServer()

	session := &Session{client: client, ID: "session-1"}
	snapshots, err := session.ListSnapshots(context.Background())
	if err != nil {
		t.Fatalf("ListSnapshots: %v", err)
	}
	want := []SnapshotStatus{SnapshotStatusPending, SnapshotStatusCompleted, SnapshotStatusFailed}
	if len(snapshots) != len(want) {
		t.Fatalf("got %d snapshots, want %d", len(snapshots), len(want))
	}
	for i, status := range want {
		if snapshots[i].Status != status {
			t.Errorf("snapshot %d status = %q, want %q", i, snapshots[i].Status, status)
		}
	}
	if snapshots[0].SessionID == nil || snapshots[1].SessionID != nil || snapshots[2].SessionID != nil {
		t.Errorf("unexpected nullable session IDs: %+v", snapshots)
	}
}

func TestGetSnapshot(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/snapshots/snap-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		writeJSON(t, w, 200, `{"id":"snap-1","session_id":null,"status":"completed","storage_key_prefix":"snapshots/acct/snap-1","size_bytes":42,"created_at":"2026-10-31T00:00:00Z","deleted_at":null}`)
	})
	defer closeServer()

	snapshot, err := client.GetSnapshot(context.Background(), "snap-1")
	if err != nil {
		t.Fatalf("GetSnapshot: %v", err)
	}
	if snapshot.ID != "snap-1" || snapshot.SessionID != nil || snapshot.Status != SnapshotStatusCompleted {
		t.Errorf("unexpected snapshot: %+v", snapshot)
	}
}

func TestRestoreSnapshotUsesGenerousTimeoutAndSandboxType(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/snapshots/snap-1/restore" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		var request map[string]any
		_ = json.Unmarshal(body, &request)
		if request["label"] != "restored" {
			t.Errorf("unexpected body: %v", request)
		}
		time.Sleep(20 * time.Millisecond)
		writeJSON(t, w, 201, `{"id":"session-2","status":"creating","label":"restored","created_at":"2026-10-31T00:00:00Z","destroyed_at":null,"expires_at":null,"usage":{"monthly_sandbox_hours_used":1,"monthly_sandbox_hours_limit":100,"concurrent_sandboxes":1,"concurrent_sandboxes_limit":10}}`)
	})
	defer closeServer()
	client.timeout = time.Nanosecond
	client.httpClient.Timeout = time.Nanosecond

	sandbox, err := client.RestoreSnapshot(context.Background(), "snap-1", RestoreSnapshotRequest{Label: Ptr("restored")})
	if err != nil {
		t.Fatalf("RestoreSnapshot: %v", err)
	}
	if sandbox.ID != "session-2" || sandbox.Status != "creating" || sandbox.Usage == nil {
		t.Errorf("unexpected restored sandbox: %+v", sandbox)
	}
}

func TestDeleteSnapshot(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/snapshots/snap-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	defer closeServer()

	if err := client.DeleteSnapshot(context.Background(), "snap-1"); err != nil {
		t.Fatalf("DeleteSnapshot: %v", err)
	}
}
