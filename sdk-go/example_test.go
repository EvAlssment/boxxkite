package boxkite

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
)

// exampleServer starts an httptest.Server driven by mux and returns a
// *Client pointed at it plus the server for the caller to Close. Distinct
// from newTestClient (testhelpers_test.go): Example functions take no
// *testing.T, so they can't use a helper that calls t.Helper()/t.Fatalf.
func exampleServer(mux *http.ServeMux) (*Client, *httptest.Server) {
	server := httptest.NewServer(mux)
	client, err := NewClient(server.URL, "bxk_live_example")
	if err != nil {
		panic(err)
	}
	return client, server
}

func ExampleClient_CreateSandbox() {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sandboxes", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"id":"sbx_123","status":"running","label":"demo","created_at":"2026-01-01T00:00:00Z"}`)
	})
	client, server := exampleServer(mux)
	defer server.Close()

	sandbox, err := client.CreateSandbox(context.Background(), CreateSandboxRequest{Label: Ptr("demo")})
	if err != nil {
		fmt.Println("error:", err)
		return
	}
	fmt.Println(sandbox.Status)
	// Output: running
}

func ExampleClient_Exec() {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sandboxes/sbx_123/exec", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"exit_code":0,"stdout":"2\n","stderr":""}`)
	})
	client, server := exampleServer(mux)
	defer server.Close()

	result, err := client.Exec(context.Background(), "sbx_123", "python3 -c 'print(1 + 1)'", nil)
	if err != nil {
		fmt.Println("error:", err)
		return
	}
	fmt.Println(strings.TrimSpace(result.Stdout))
	// Output: 2
}

func ExampleClient_WithSandbox() {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/sandboxes", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"id":"sbx_123","status":"running","created_at":"2026-01-01T00:00:00Z"}`)
	})
	mux.HandleFunc("POST /v1/sandboxes/{id}/exec", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"exit_code":0,"stdout":"2\n","stderr":""}`)
	})
	mux.HandleFunc("POST /v1/sandboxes/{id}/files", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"path":"hello.txt","size":32,"created":true}`)
	})
	mux.HandleFunc("POST /v1/sandboxes/{id}/files/view", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"content":"hello from boxkite-client (go)\n","lines":1,"is_directory":false}`)
	})
	mux.HandleFunc("DELETE /v1/sandboxes/{id}", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	client, server := exampleServer(mux)
	defer server.Close()

	ctx := context.Background()
	err := client.WithSandbox(ctx, CreateSandboxRequest{Label: Ptr("sdk-go-example")}, func(sb *Session) error {
		result, err := sb.Exec(ctx, "python3 -c 'print(1 + 1)'", nil)
		if err != nil {
			return err
		}
		fmt.Println(strings.TrimSpace(result.Stdout))

		if _, err := sb.FileCreate(ctx, "hello.txt", "hello from boxkite-client (go)\n", nil); err != nil {
			return err
		}
		viewed, err := sb.View(ctx, "hello.txt", nil)
		if err != nil {
			return err
		}
		fmt.Println(strings.TrimSpace(viewed.Content))
		return nil
	})
	if err != nil {
		fmt.Println("error:", err)
		return
	}
	// Output:
	// 2
	// hello from boxkite-client (go)
}
