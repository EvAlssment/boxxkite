package boxkite

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestWithHTTPClient_OverridesTransport(t *testing.T) {
	roundTripCalled := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"id": "acct-1", "email": "a@example.com", "created_at": "2026-01-01T00:00:00Z"}`)
	}))
	defer server.Close()

	transport := &recordingTransport{inner: http.DefaultTransport, called: &roundTripCalled}
	client, err := NewClient(server.URL, "bxk_live_test", WithHTTPClient(&http.Client{Transport: transport}))
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	if _, err := client.Account(context.Background()); err != nil {
		t.Fatalf("Account: %v", err)
	}
	if !roundTripCalled {
		t.Error("expected the custom http.Client's Transport to be used")
	}
}

type recordingTransport struct {
	inner  http.RoundTripper
	called *bool
}

func (t *recordingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	*t.called = true
	return t.inner.RoundTrip(req)
}

func TestWithTimeout_APIErrorsSurfaceAsConnectionErrorOnTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(50 * time.Millisecond)
		// The client-side timeout below has already fired and disconnected
		// by the time this handler wakes up, so this write is expected to
		// race a closed connection -- ignore any error here rather than
		// calling t.Fatalf from this handler goroutine (unsafe per the
		// testing package's own rules).
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"id": "acct-1", "email": "a@example.com", "created_at": "2026-01-01T00:00:00Z"}`))
	}))
	defer server.Close()

	client, err := NewClient(server.URL, "bxk_live_test", WithTimeout(1*time.Millisecond))
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	_, err = client.Account(context.Background())
	if err == nil {
		t.Fatal("expected a timeout error")
	}
	if _, ok := err.(*ConnectionError); !ok {
		t.Fatalf("expected *ConnectionError from a client-side timeout, got %T: %v", err, err)
	}
}

func TestWithWebSocketDialer_OverridesDefaultDialer(t *testing.T) {
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		conn.Close()
	}))
	defer server.Close()

	dialCalled := false
	dialer := *websocket.DefaultDialer
	dialer.NetDialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		dialCalled = true
		return net.Dial(network, addr)
	}

	client, err := NewClient(server.URL, "bxk_live_test", WithWebSocketDialer(&dialer))
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	conn, err := client.Takeover(context.Background(), "sess-1")
	if err != nil {
		t.Fatalf("Takeover: %v", err)
	}
	defer conn.Close()

	if !dialCalled {
		t.Error("expected the custom websocket.Dialer to be used")
	}
}
