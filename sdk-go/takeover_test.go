package boxkite

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gorilla/websocket"
)

func TestTakeover_SendsAuthorizationHeaderAndBridgesBytes(t *testing.T) {
	upgrader := websocket.Upgrader{}
	var gotAuthHeader string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/takeover" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		gotAuthHeader = r.Header.Get("Authorization")
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrading connection: %v", err)
			return
		}
		defer conn.Close()
		// Echo exactly one message back, mirroring the raw byte-bridging
		// contract -- no message envelope.
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return
		}
		_ = conn.WriteMessage(websocket.BinaryMessage, msg)
	}))
	defer server.Close()

	client, err := NewClient(server.URL, "bxk_live_test")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	conn, err := client.Takeover(context.Background(), "sess-1")
	if err != nil {
		t.Fatalf("Takeover: %v", err)
	}
	defer conn.Close()

	if gotAuthHeader != "Bearer bxk_live_test" {
		t.Errorf("expected Authorization header 'Bearer bxk_live_test', got %q", gotAuthHeader)
	}

	if err := conn.WriteMessage(websocket.BinaryMessage, []byte("echo hi\n")); err != nil {
		t.Fatalf("WriteMessage: %v", err)
	}
	_, reply, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("ReadMessage: %v", err)
	}
	if string(reply) != "echo hi\n" {
		t.Errorf("unexpected reply: %q", reply)
	}
}

func TestTakeover_ConnectionRefusedReturnsConnectionError(t *testing.T) {
	client, err := NewClient("http://localhost:1", "bxk_live_test")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	_, err = client.Takeover(context.Background(), "sess-1")
	if err == nil {
		t.Fatal("expected an error connecting to a closed port")
	}
	if _, ok := err.(*ConnectionError); !ok {
		t.Fatalf("expected *ConnectionError, got %T: %v", err, err)
	}
}
