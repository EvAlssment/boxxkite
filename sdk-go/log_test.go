package boxkite

import (
	"bufio"
	"context"
	"net/http"
	"testing"
)

func TestGetLog_SendsLimitAndOffset(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/log" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		q := r.URL.Query()
		if q.Get("limit") != "50" || q.Get("offset") != "10" {
			t.Errorf("unexpected query: %v", q)
		}
		writeJSON(t, w, 200, `{
			"entries": [{"id": "e1", "session_id": "sess-1", "source": "agent", "operation": "exec",
				"detail": {"command": "echo hi"}, "exit_code": 0, "output_truncated": "hi\n",
				"started_at": "2026-01-01T00:00:00Z", "duration_ms": 12, "row_hash": "abc", "prev_hash": null}],
			"limit": 50, "offset": 10, "total": 1
		}`)
	})
	defer closeServer()

	result, err := client.GetLog(context.Background(), "sess-1", &GetLogOptions{Limit: Ptr(50), Offset: Ptr(10)})
	if err != nil {
		t.Fatalf("GetLog: %v", err)
	}
	if len(result.Entries) != 1 || result.Entries[0].Operation != "exec" {
		t.Errorf("unexpected result: %+v", result)
	}
	if result.Entries[0].ExitCode == nil || *result.Entries[0].ExitCode != 0 {
		t.Errorf("unexpected exit_code: %+v", result.Entries[0].ExitCode)
	}
}

func TestGetLog_NotFoundMapsToAPIError(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 404, `{"error": {"code": "not_found", "message": "Session not found."}}`)
	})
	defer closeServer()

	_, err := client.GetLog(context.Background(), "sess-missing", nil)
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "not_found" {
		t.Errorf("unexpected code: %s", apiErr.Code)
	}
}

func TestWatch_StreamsSSEEventsAsLogEntries(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/watch" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer bxk_live_test" {
			t.Errorf("unexpected Authorization header: %q", got)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		flusher, _ := w.(http.Flusher)

		fw := bufio.NewWriter(w)
		_, _ = fw.WriteString("data: {\"id\": \"e1\", \"session_id\": \"sess-1\", \"source\": \"agent\", \"operation\": \"exec\", \"detail\": {}, \"exit_code\": 0, \"output_truncated\": \"hi\\n\", \"started_at\": \"2026-01-01T00:00:00Z\"}\n\n")
		_ = fw.Flush()
		if flusher != nil {
			flusher.Flush()
		}
		_, _ = fw.WriteString("data: {\"id\": \"e2\", \"session_id\": \"sess-1\", \"source\": \"agent\", \"operation\": \"file_create\", \"detail\": {}, \"exit_code\": null, \"output_truncated\": \"\", \"started_at\": \"2026-01-01T00:00:01Z\"}\n\n")
		_ = fw.Flush()
		if flusher != nil {
			flusher.Flush()
		}
	})
	defer closeServer()

	watcher, err := client.Watch(context.Background(), "sess-1")
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer watcher.Close()

	var entries []LogEntry
	for watcher.Next() {
		entries = append(entries, watcher.Entry())
	}
	if err := watcher.Err(); err != nil {
		t.Fatalf("watcher.Err(): %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(entries))
	}
	if entries[0].ID != "e1" || entries[0].Operation != "exec" {
		t.Errorf("unexpected first entry: %+v", entries[0])
	}
	if entries[1].ID != "e2" || entries[1].Operation != "file_create" {
		t.Errorf("unexpected second entry: %+v", entries[1])
	}
}

func TestWatch_NotFoundBeforeStreamOpensMapsToAPIError(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 404, `{"error": {"code": "not_found", "message": "Session not found."}}`)
	})
	defer closeServer()

	_, err := client.Watch(context.Background(), "sess-missing")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "not_found" {
		t.Errorf("unexpected code: %s", apiErr.Code)
	}
}
