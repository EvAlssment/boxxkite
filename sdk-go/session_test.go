package boxkite

import (
	"context"
	"errors"
	"net/http"
	"testing"
)

func TestWithSandbox_CreatesAndDestroysOnSuccess(t *testing.T) {
	var created, destroyed bool
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sandboxes":
			created = true
			writeJSON(t, w, 201, `{"id": "sess-1", "status": "active", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": null, "expires_at": null}`)
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/sandboxes/sess-1":
			destroyed = true
			w.WriteHeader(204)
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sandboxes/sess-1/exec":
			writeJSON(t, w, 200, `{"exit_code": 0, "stdout": "2\n", "stderr": ""}`)
		default:
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
	})
	defer closeServer()

	var stdout string
	err := client.WithSandbox(context.Background(), CreateSandboxRequest{Label: Ptr("demo")}, func(sb *Session) error {
		if sb.ID != "sess-1" {
			t.Errorf("unexpected session id: %s", sb.ID)
		}
		result, err := sb.Exec(context.Background(), "python3 -c 'print(1+1)'", nil)
		if err != nil {
			return err
		}
		stdout = result.Stdout
		return nil
	})
	if err != nil {
		t.Fatalf("WithSandbox: %v", err)
	}
	if !created || !destroyed {
		t.Errorf("expected both create and destroy to be called: created=%v destroyed=%v", created, destroyed)
	}
	if stdout != "2\n" {
		t.Errorf("unexpected stdout: %q", stdout)
	}
}

func TestWithSandbox_DestroysEvenWhenCallbackErrors(t *testing.T) {
	destroyed := false
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sandboxes":
			writeJSON(t, w, 201, `{"id": "sess-2", "status": "active", "label": null, "created_at": "2026-01-01T00:00:00Z", "destroyed_at": null, "expires_at": null}`)
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/sandboxes/sess-2":
			destroyed = true
			w.WriteHeader(204)
		default:
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
	})
	defer closeServer()

	wantErr := errors.New("callback failed")
	err := client.WithSandbox(context.Background(), CreateSandboxRequest{}, func(sb *Session) error {
		return wantErr
	})
	if !errors.Is(err, wantErr) {
		t.Fatalf("expected the callback's own error to propagate, got: %v", err)
	}
	if !destroyed {
		t.Error("expected the sandbox to be destroyed even though the callback errored")
	}
}
