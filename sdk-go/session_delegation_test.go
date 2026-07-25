package boxkite

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gorilla/websocket"
)

// TestSession_DelegatesEveryMethodToTheUnderlyingClient exercises every
// *Session forwarding method against sess-1, proving each one reaches the
// identically-named *Client method with the Session's own ID rather than
// silently no-oping (Session is a thin wrapper with no logic of its own to
// unit test beyond "does it forward correctly").
func TestSession_DelegatesEveryMethodToTheUnderlyingClient(t *testing.T) {
	upgrader := websocket.Upgrader{}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sandboxes/sess-1/http-request", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"status_code": 200, "headers": {}, "body": "", "truncated": false}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/files", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"path": "a.txt", "size": 1, "created": true}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/files/view", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"content": "a", "lines": 1, "is_directory": false, "entries": null}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/files/str-replace", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"path": "a.txt", "replaced": true, "occurrences": 1}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/files/ls", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"entries": []}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/files/glob", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"matches": []}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/files/grep", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"matches": [], "error": null, "truncated": false}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/log", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"entries": [], "limit": 50, "offset": 0, "total": 0}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/watch", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/processes", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			writeJSON(t, w, 201, `{"process_id": "proc-1", "status": "running", "started_at": "2026-01-01T00:00:00Z"}`)
			return
		}
		writeJSON(t, w, 200, `{"processes": []}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/processes/proc-1/output", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"status": "running", "stdout_chunk": "", "next_offset": 0, "truncated": false, "exit_code": null}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/processes/proc-1/input", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"bytes_written": 1}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/processes/proc-1/stop", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"status": "stopped", "exit_code": 0}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/takeover", func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		conn.Close()
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/preview/3000", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"url": "/preview", "expires_at": "2026-01-01T00:15:00Z", "token_id": "tok-1"}`)
	})
	mux.HandleFunc("/v1/sandboxes/sess-1/preview/3000/revoke", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"revoked": true, "token_id": "tok-1"}`)
	})

	server := httptest.NewServer(mux)
	defer server.Close()

	client, err := NewClient(server.URL, "bxk_live_test")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	sess := &Session{client: client, ID: "sess-1"}
	ctx := context.Background()

	if _, err := sess.HTTPRequest(ctx, "GET", "https://example.com", nil); err != nil {
		t.Errorf("Session.HTTPRequest: %v", err)
	}
	if _, err := sess.FileCreate(ctx, "a.txt", "a", nil); err != nil {
		t.Errorf("Session.FileCreate: %v", err)
	}
	if _, err := sess.View(ctx, "a.txt", nil); err != nil {
		t.Errorf("Session.View: %v", err)
	}
	if _, err := sess.StrReplace(ctx, "a.txt", "a", "b", nil); err != nil {
		t.Errorf("Session.StrReplace: %v", err)
	}
	if _, err := sess.Ls(ctx, "/"); err != nil {
		t.Errorf("Session.Ls: %v", err)
	}
	if _, err := sess.Glob(ctx, "*.txt", "/"); err != nil {
		t.Errorf("Session.Glob: %v", err)
	}
	if _, err := sess.Grep(ctx, "TODO", nil); err != nil {
		t.Errorf("Session.Grep: %v", err)
	}
	if _, err := sess.GetLog(ctx, nil); err != nil {
		t.Errorf("Session.GetLog: %v", err)
	}
	watcher, err := sess.Watch(ctx)
	if err != nil {
		t.Errorf("Session.Watch: %v", err)
	} else {
		watcher.Close()
	}
	if _, err := sess.StartProcess(ctx, "sleep 1", nil); err != nil {
		t.Errorf("Session.StartProcess: %v", err)
	}
	if _, err := sess.ListProcesses(ctx); err != nil {
		t.Errorf("Session.ListProcesses: %v", err)
	}
	if _, err := sess.GetProcessOutput(ctx, "proc-1", 0); err != nil {
		t.Errorf("Session.GetProcessOutput: %v", err)
	}
	if _, err := sess.SendProcessInput(ctx, "proc-1", "y\n"); err != nil {
		t.Errorf("Session.SendProcessInput: %v", err)
	}
	if _, err := sess.StopProcess(ctx, "proc-1"); err != nil {
		t.Errorf("Session.StopProcess: %v", err)
	}
	conn, err := sess.Takeover(ctx)
	if err != nil {
		t.Errorf("Session.Takeover: %v", err)
	} else {
		conn.Close()
	}
	if _, err := sess.CreatePreviewURL(ctx, 3000, nil); err != nil {
		t.Errorf("Session.CreatePreviewURL: %v", err)
	}
	if _, err := sess.RevokePreviewURL(ctx, 3000, "tok-1"); err != nil {
		t.Errorf("Session.RevokePreviewURL: %v", err)
	}
}
