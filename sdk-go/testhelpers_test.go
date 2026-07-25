package boxkite

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// newTestClient starts an httptest.Server driven by handler and returns a
// *Client pointed at it, using the same "bxk_live_test" API key every test
// below asserts against. The caller must call the returned func to shut
// the server down.
func newTestClient(t *testing.T, handler http.HandlerFunc) (*Client, func()) {
	t.Helper()
	server := httptest.NewServer(handler)
	client, err := NewClient(server.URL, "bxk_live_test")
	if err != nil {
		server.Close()
		t.Fatalf("NewClient: %v", err)
	}
	return client, server.Close
}

func writeJSON(t *testing.T, w http.ResponseWriter, status int, body string) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write([]byte(body)); err != nil {
		t.Fatalf("writing response body: %v", err)
	}
}
