package boxkite

import (
	"context"
	"net/http"
	"strings"
	"testing"
)

func TestNewClient_RejectsPlainHTTPToRemoteHost(t *testing.T) {
	_, err := NewClient("http://cp.example.com", "bxk_live_test")
	if err == nil {
		t.Fatal("expected an error for a plain http:// remote base_url")
	}
	if !strings.Contains(err.Error(), "cleartext") {
		t.Fatalf("expected error to mention cleartext, got: %v", err)
	}
}

func TestNewClient_AllowsHTTPLocalhostForLocalDev(t *testing.T) {
	client, err := NewClient("http://localhost:8090", "bxk_live_test")
	if err != nil {
		t.Fatalf("expected http://localhost to be allowed, got: %v", err)
	}
	if client == nil {
		t.Fatal("expected a non-nil client")
	}
}

func TestNewClient_AllowsHTTPS(t *testing.T) {
	client, err := NewClient("https://cp.example.com", "bxk_live_test")
	if err != nil {
		t.Fatalf("expected https:// to be allowed, got: %v", err)
	}
	if client == nil {
		t.Fatal("expected a non-nil client")
	}
}

func TestAccount_ReturnsParsedBodyAndAuthHeader(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/account" {
			t.Errorf("expected path /v1/account, got %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer bxk_live_test" {
			t.Errorf("expected Authorization header 'Bearer bxk_live_test', got %q", got)
		}
		writeJSON(t, w, 200, `{"id": "acct-1", "email": "a@example.com", "created_at": "2026-01-01T00:00:00Z"}`)
	})
	defer closeServer()

	account, err := client.Account(context.Background())
	if err != nil {
		t.Fatalf("Account: %v", err)
	}
	if account.ID != "acct-1" || account.Email != "a@example.com" {
		t.Errorf("unexpected account: %+v", account)
	}
}

func TestDoJSON_ReturnsAPIErrorOnErrorEnvelope(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 404, `{"error": {"code": "not_found", "message": "Session not found."}}`)
	})
	defer closeServer()

	_, err := client.GetSandbox(context.Background(), "sess-missing")
	if err == nil {
		t.Fatal("expected an error")
	}
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T: %v", err, err)
	}
	if apiErr.StatusCode != 404 || apiErr.Code != "not_found" {
		t.Errorf("unexpected APIError: %+v", apiErr)
	}
}

func TestDoJSON_SynthesizesErrorForNonEnvelopeBody(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		_, _ = w.Write([]byte("internal server error"))
	})
	defer closeServer()

	_, err := client.GetSandbox(context.Background(), "sess-1")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T: %v", err, err)
	}
	if apiErr.StatusCode != 500 || apiErr.Code != "error" {
		t.Errorf("unexpected fallback APIError: %+v", apiErr)
	}
}

func TestDoJSON_ReturnsConnectionErrorWhenUnreachable(t *testing.T) {
	client, err := NewClient("http://localhost:1", "bxk_live_test")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	_, err = client.Account(context.Background())
	if err == nil {
		t.Fatal("expected a connection error")
	}
	if _, ok := err.(*ConnectionError); !ok {
		t.Fatalf("expected *ConnectionError, got %T: %v", err, err)
	}
}
