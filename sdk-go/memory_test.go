package boxxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestRemember_SendsContentScopeAndKind(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["content"] != "The user prefers TypeScript." {
			t.Errorf("unexpected content: %v", body["content"])
		}
		if body["scope"] != "default" {
			t.Errorf("unexpected scope: %v", body["scope"])
		}
		if body["kind"] != "fact" {
			t.Errorf("unexpected kind: %v", body["kind"])
		}
		writeJSON(t, w, 201, `{"id": "mem-1", "content": "The user prefers TypeScript."}`)
	})
	defer closeServer()

	mem, err := client.Remember(context.Background(), RememberRequest{Content: "The user prefers TypeScript."})
	if err != nil {
		t.Fatalf("Remember: %v", err)
	}
	if mem.ID != "mem-1" {
		t.Errorf("unexpected id: %+v", mem)
	}
}

func TestIngestMemory_SendsContentAndScope(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/ingest" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["content"] != "long transcript" {
			t.Errorf("unexpected content: %v", body["content"])
		}
		writeJSON(t, w, 201, `{"source_session_id": null, "memories": []}`)
	})
	defer closeServer()

	resp, err := client.IngestMemory(context.Background(), IngestMemoryRequest{Content: "long transcript"})
	if err != nil {
		t.Fatalf("IngestMemory: %v", err)
	}
	if len(resp.Memories) != 0 {
		t.Errorf("expected no memories, got: %+v", resp.Memories)
	}
}

func TestRecall_SendsQueryAndLimit(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/search" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if r.URL.Query().Get("q") != "TypeScript" {
			t.Errorf("unexpected q: %v", r.URL.Query().Get("q"))
		}
		if r.URL.Query().Get("limit") != "10" {
			t.Errorf("unexpected limit: %v", r.URL.Query().Get("limit"))
		}
		writeJSON(t, w, 200, `{"query": "TypeScript", "memories": []}`)
	})
	defer closeServer()

	resp, err := client.Recall(context.Background(), "TypeScript", RecallOptions{})
	if err != nil {
		t.Fatalf("Recall: %v", err)
	}
	if resp.Query != "TypeScript" {
		t.Errorf("unexpected query: %+v", resp)
	}
}

func TestMemoryProfile_ReturnsStaticAndDynamic(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/profile" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"static": [], "dynamic": []}`)
	})
	defer closeServer()

	resp, err := client.MemoryProfile(context.Background(), "", 0)
	if err != nil {
		t.Fatalf("MemoryProfile: %v", err)
	}
	if len(resp.Static) != 0 || len(resp.Dynamic) != 0 {
		t.Errorf("unexpected profile: %+v", resp)
	}
}

func TestListMemories_ReturnsEmptySliceWhenNone(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/memory" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		writeJSON(t, w, 200, `[]`)
	})
	defer closeServer()

	memories, err := client.ListMemories(context.Background(), ListMemoriesOptions{})
	if err != nil {
		t.Fatalf("ListMemories: %v", err)
	}
	if memories == nil || len(memories) != 0 {
		t.Errorf("expected empty (non-nil) slice, got: %+v", memories)
	}
}

func TestGetMemory_ReturnsResult(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/mem-1" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"id": "mem-1"}`)
	})
	defer closeServer()

	mem, err := client.GetMemory(context.Background(), "mem-1")
	if err != nil {
		t.Fatalf("GetMemory: %v", err)
	}
	if mem.ID != "mem-1" {
		t.Errorf("unexpected memory: %+v", mem)
	}
}

func TestMemoryRelations_ReturnsResult(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/mem-1/relations" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"memory_id": "mem-1", "relations": []}`)
	})
	defer closeServer()

	resp, err := client.MemoryRelations(context.Background(), "mem-1", 0)
	if err != nil {
		t.Fatalf("MemoryRelations: %v", err)
	}
	if resp.MemoryID != "mem-1" {
		t.Errorf("unexpected response: %+v", resp)
	}
}

func TestForgetMemory(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/memory/mem-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.ForgetMemory(context.Background(), "mem-1"); err != nil {
		t.Fatalf("ForgetMemory: %v", err)
	}
}

func TestExportMemories_ReturnsResult(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/export" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"memories": [], "relations": []}`)
	})
	defer closeServer()

	resp, err := client.ExportMemories(context.Background(), "")
	if err != nil {
		t.Fatalf("ExportMemories: %v", err)
	}
	if resp == nil {
		t.Errorf("expected a non-nil export payload")
	}
}

func TestImportMemories_SendsMemoriesAndRelations(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory/import" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		memories, _ := body["memories"].([]any)
		if len(memories) != 1 {
			t.Errorf("unexpected memories: %v", body["memories"])
		}
		writeJSON(t, w, 201, `{"source_session_id": null, "memories": [{"id": "mem-1"}]}`)
	})
	defer closeServer()

	resp, err := client.ImportMemories(context.Background(), ImportMemoriesRequest{
		Memories: []map[string]any{{"content": "x"}},
	})
	if err != nil {
		t.Fatalf("ImportMemories: %v", err)
	}
	if len(resp.Memories) != 1 || resp.Memories[0].ID != "mem-1" {
		t.Errorf("unexpected response: %+v", resp)
	}
}
