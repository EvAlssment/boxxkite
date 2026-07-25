package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestFileCreate_SendsPathAndContent(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/files" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["path"] != "hello.txt" || body["content"] != "hello\n" {
			t.Errorf("unexpected body: %v", body)
		}
		writeJSON(t, w, 200, `{"path": "hello.txt", "size": 6, "created": true}`)
	})
	defer closeServer()

	result, err := client.FileCreate(context.Background(), "sess-1", "hello.txt", "hello\n", nil)
	if err != nil {
		t.Fatalf("FileCreate: %v", err)
	}
	if !result.Created || result.Size != 6 {
		t.Errorf("unexpected result: %+v", result)
	}
}

func TestView_SendsViewRange(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sandboxes/sess-1/files/view" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		viewRange, _ := body["view_range"].([]any)
		if len(viewRange) != 2 || viewRange[0] != float64(1) || viewRange[1] != float64(10) {
			t.Errorf("unexpected view_range: %v", body["view_range"])
		}
		writeJSON(t, w, 200, `{"content": "hello\n", "lines": 1, "is_directory": false, "entries": null}`)
	})
	defer closeServer()

	result, err := client.View(context.Background(), "sess-1", "hello.txt", &ViewOptions{ViewRange: []int{1, 10}})
	if err != nil {
		t.Fatalf("View: %v", err)
	}
	if result.Content != "hello\n" {
		t.Errorf("unexpected content: %q", result.Content)
	}
}

func TestStrReplace_DefaultsReplaceAllToFalse(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["replace_all"] != false {
			t.Errorf("expected replace_all=false by default, got %v", body["replace_all"])
		}
		if body["old_str"] != "hello" || body["new_str"] != "hi" {
			t.Errorf("unexpected old_str/new_str: %v", body)
		}
		writeJSON(t, w, 200, `{"path": "hello.txt", "replaced": true, "occurrences": 1}`)
	})
	defer closeServer()

	result, err := client.StrReplace(context.Background(), "sess-1", "hello.txt", "hello", "hi", nil)
	if err != nil {
		t.Fatalf("StrReplace: %v", err)
	}
	if result.Occurrences != 1 {
		t.Errorf("unexpected occurrences: %d", result.Occurrences)
	}
}

func TestStrReplace_ReplaceAllTrue(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["replace_all"] != true {
			t.Errorf("expected replace_all=true, got %v", body["replace_all"])
		}
		writeJSON(t, w, 200, `{"path": "hello.txt", "replaced": true, "occurrences": 3}`)
	})
	defer closeServer()

	_, err := client.StrReplace(context.Background(), "sess-1", "hello.txt", "a", "b", &StrReplaceOptions{ReplaceAll: true})
	if err != nil {
		t.Fatalf("StrReplace: %v", err)
	}
}

func TestLs_DefaultsPathToRoot(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["path"] != "/" {
			t.Errorf("expected default path '/', got %v", body["path"])
		}
		writeJSON(t, w, 200, `{"entries": [{"path": "hello.txt", "is_dir": false, "size": 20}]}`)
	})
	defer closeServer()

	result, err := client.Ls(context.Background(), "sess-1", "")
	if err != nil {
		t.Fatalf("Ls: %v", err)
	}
	if len(result.Entries) != 1 || result.Entries[0].Path != "hello.txt" {
		t.Errorf("unexpected entries: %+v", result.Entries)
	}
}

func TestGlob_SendsPatternAndPath(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["pattern"] != "**/*.py" || body["path"] != "/" {
			t.Errorf("unexpected body: %v", body)
		}
		writeJSON(t, w, 200, `{"matches": [{"path": "main.py", "is_dir": false, "size": 120}]}`)
	})
	defer closeServer()

	result, err := client.Glob(context.Background(), "sess-1", "**/*.py", "")
	if err != nil {
		t.Fatalf("Glob: %v", err)
	}
	if len(result.Matches) != 1 {
		t.Errorf("unexpected matches: %+v", result.Matches)
	}
}

func TestGrep_DefaultsMaxMatchesTo500(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["max_matches"] != float64(500) {
			t.Errorf("expected default max_matches=500, got %v", body["max_matches"])
		}
		if _, hasGlob := body["glob"]; hasGlob {
			t.Errorf("expected no glob field when unset, got %v", body["glob"])
		}
		writeJSON(t, w, 200, `{"matches": [{"path": "main.py", "line": 12, "text": "# TODO"}], "error": null, "truncated": false}`)
	})
	defer closeServer()

	result, err := client.Grep(context.Background(), "sess-1", "TODO", nil)
	if err != nil {
		t.Fatalf("Grep: %v", err)
	}
	if len(result.Matches) != 1 || result.Matches[0].Line != 12 {
		t.Errorf("unexpected matches: %+v", result.Matches)
	}
}

func TestGrep_CustomOptions(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["path"] != "/src" || body["glob"] != "*.py" || body["max_matches"] != float64(10) {
			t.Errorf("unexpected body: %v", body)
		}
		writeJSON(t, w, 200, `{"matches": [], "error": null, "truncated": false}`)
	})
	defer closeServer()

	_, err := client.Grep(context.Background(), "sess-1", "TODO", &GrepOptions{Path: "/src", Glob: "*.py", MaxMatches: 10})
	if err != nil {
		t.Fatalf("Grep: %v", err)
	}
}
