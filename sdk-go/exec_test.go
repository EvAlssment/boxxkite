package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestExec_SendsCommandTimeoutAndDescription(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/exec" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["command"] != "echo hi" {
			t.Errorf("unexpected command: %v", body["command"])
		}
		if body["timeout"] != float64(30) {
			t.Errorf("unexpected timeout: %v", body["timeout"])
		}
		if body["description"] != "greet" {
			t.Errorf("unexpected description: %v", body["description"])
		}
		writeJSON(t, w, 200, `{"exit_code": 0, "stdout": "hi\n", "stderr": ""}`)
	})
	defer closeServer()

	result, err := client.Exec(context.Background(), "sess-1", "echo hi", &ExecOptions{
		Timeout:     Ptr(30),
		Description: Ptr("greet"),
	})
	if err != nil {
		t.Fatalf("Exec: %v", err)
	}
	if result.ExitCode != 0 || result.Stdout != "hi\n" {
		t.Errorf("unexpected result: %+v", result)
	}
}

func TestExec_CommandNotAllowedMapsToAPIError(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 403, `{"error": {"code": "command_not_allowed", "message": "Command not in allowlist."}}`)
	})
	defer closeServer()

	_, err := client.Exec(context.Background(), "sess-1", "rm -rf /", nil)
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "command_not_allowed" || apiErr.StatusCode != 403 {
		t.Errorf("unexpected APIError: %+v", apiErr)
	}
}

func TestHTTPRequest_SendsMethodURLHeadersAndBody(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/http-request" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["method"] != "GET" || body["url"] != "https://api.example.com/v1/me" {
			t.Errorf("unexpected method/url: %v", body)
		}
		headers, _ := body["headers"].(map[string]any)
		if headers["Authorization"] != "Bearer {{secret:prod-api-key}}" {
			t.Errorf("unexpected headers: %v", headers)
		}
		writeJSON(t, w, 200, `{"status_code": 200, "headers": {"content-type": "application/json"}, "body": "{}", "truncated": false}`)
	})
	defer closeServer()

	result, err := client.HTTPRequest(context.Background(), "sess-1", "GET", "https://api.example.com/v1/me", &HTTPRequestOptions{
		Headers: map[string]string{"Authorization": "Bearer {{secret:prod-api-key}}"},
	})
	if err != nil {
		t.Fatalf("HTTPRequest: %v", err)
	}
	if result.StatusCode != 200 {
		t.Errorf("unexpected status code: %d", result.StatusCode)
	}
}
