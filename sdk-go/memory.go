package boxxkite

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
)

// Memory is one durable, account-scoped memory (MemoryBase --
// POST/GET /v1/memory).
type Memory struct {
	ID              string         `json:"id"`
	Scope           string         `json:"scope"`
	Kind            string         `json:"kind"`
	Content         string         `json:"content"`
	Metadata        map[string]any `json:"metadata"`
	SourceSessionID *string        `json:"source_session_id"`
	CreatedAt       string         `json:"created_at"`
	UpdatedAt       string         `json:"updated_at"`
	ExpiresAt       *string        `json:"expires_at"`
	SourceContent   *string        `json:"source_content"`
	DocumentDate    *string        `json:"document_date"`
	EventDates      []string       `json:"event_dates"`
	Importance      float64        `json:"importance"`
	AccessCount     int            `json:"access_count"`
	LastAccessedAt  *string        `json:"last_accessed_at"`
	SupersededAt    *string        `json:"superseded_at"`
	SupersededByID  *string        `json:"superseded_by_id"`
}

// MemorySearchHit is a memory returned from Recall, with its retrieval score.
type MemorySearchHit struct {
	Memory
	Score float64 `json:"score"`
}

// MemorySearchResponse is the response from Recall (GET /v1/memory/search).
type MemorySearchResponse struct {
	Query    string            `json:"query"`
	Memories []MemorySearchHit `json:"memories"`
}

// MemoryIngestResponse is the response from IngestMemory and ImportMemories.
type MemoryIngestResponse struct {
	SourceSessionID *string  `json:"source_session_id"`
	Memories        []Memory `json:"memories"`
}

// MemoryProfileResponse is the response from MemoryProfile
// (GET /v1/memory/profile) -- stable, long-lived memories separated from
// recently-touched ones.
type MemoryProfileResponse struct {
	Static  []Memory `json:"static"`
	Dynamic []Memory `json:"dynamic"`
}

// MemoryRelation is one edge from MemoryRelations.
type MemoryRelation struct {
	SourceMemoryID string  `json:"source_memory_id"`
	TargetMemoryID string  `json:"target_memory_id"`
	RelationType   string  `json:"relation_type"`
	Confidence     float64 `json:"confidence"`
	CreatedAt      string  `json:"created_at"`
}

// MemoryRelationsResponse is the response from MemoryRelations
// (GET /v1/memory/{id}/relations).
type MemoryRelationsResponse struct {
	MemoryID  string           `json:"memory_id"`
	Relations []MemoryRelation `json:"relations"`
}

// RememberRequest is the request body for Remember (POST /v1/memory).
type RememberRequest struct {
	Content         string         `json:"content"`
	Scope           string         `json:"scope,omitempty"`
	Kind            string         `json:"kind,omitempty"`
	Metadata        map[string]any `json:"metadata,omitempty"`
	SourceSessionID *string        `json:"source_session_id,omitempty"`
	Importance      *float64       `json:"importance,omitempty"`
}

// Remember stores one durable, account-scoped memory directly
// (POST /v1/memory). See the MemoryBase developer guide for the full model
// (opt-in, self-hosted retrieval by default, no third-party memory API).
func (c *Client) Remember(ctx context.Context, req RememberRequest) (*Memory, error) {
	if req.Scope == "" {
		req.Scope = "default"
	}
	if req.Kind == "" {
		req.Kind = "fact"
	}
	var out Memory
	if err := c.doJSON(ctx, "POST", "/v1/memory", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// IngestMemoryRequest is the request body for IngestMemory
// (POST /v1/memory/ingest).
type IngestMemoryRequest struct {
	Content         string         `json:"content"`
	Scope           string         `json:"scope,omitempty"`
	SourceSessionID *string        `json:"source_session_id,omitempty"`
	Metadata        map[string]any `json:"metadata,omitempty"`
}

// IngestMemory splits a bounded document or conversation into atomic
// memories (POST /v1/memory/ingest). Works with no model provider
// configured -- the default extractor is deterministic.
func (c *Client) IngestMemory(ctx context.Context, req IngestMemoryRequest) (*MemoryIngestResponse, error) {
	if req.Scope == "" {
		req.Scope = "default"
	}
	var out MemoryIngestResponse
	if err := c.doJSON(ctx, "POST", "/v1/memory/ingest", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// RecallOptions configures Recall.
type RecallOptions struct {
	Scope string
	Limit int
}

// Recall ranks live account memories against a query using lexical
// evidence, phrase match, recency, and importance (GET /v1/memory/search).
func (c *Client) Recall(ctx context.Context, query string, opts RecallOptions) (*MemorySearchResponse, error) {
	limit := opts.Limit
	if limit <= 0 {
		limit = 10
	}
	q := newQuery()
	q.Set("q", query)
	q.Set("limit", strconv.Itoa(limit))
	if opts.Scope != "" {
		q.Set("scope", opts.Scope)
	}
	var out MemorySearchResponse
	if err := c.doJSON(ctx, "GET", "/v1/memory/search", nil, &out, &requestOptions{query: q}); err != nil {
		return nil, err
	}
	return &out, nil
}

// MemoryProfile builds a compact memory profile: stable, long-lived
// memories separated from recently-touched ones (GET /v1/memory/profile).
func (c *Client) MemoryProfile(ctx context.Context, scope string, limit int) (*MemoryProfileResponse, error) {
	if limit <= 0 {
		limit = 20
	}
	q := newQuery()
	q.Set("limit", strconv.Itoa(limit))
	if scope != "" {
		q.Set("scope", scope)
	}
	var out MemoryProfileResponse
	if err := c.doJSON(ctx, "GET", "/v1/memory/profile", nil, &out, &requestOptions{query: q}); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListMemoriesOptions configures ListMemories.
type ListMemoriesOptions struct {
	Scope             string
	IncludeSuperseded bool
	Limit             int
	Offset            int
}

// ListMemories lists live memories for this account (GET /v1/memory).
func (c *Client) ListMemories(ctx context.Context, opts ListMemoriesOptions) ([]Memory, error) {
	limit := opts.Limit
	if limit <= 0 {
		limit = 50
	}
	q := newQuery()
	q.Set("include_superseded", boolQueryValue(opts.IncludeSuperseded))
	q.Set("limit", strconv.Itoa(limit))
	q.Set("offset", strconv.Itoa(opts.Offset))
	if opts.Scope != "" {
		q.Set("scope", opts.Scope)
	}
	var out []Memory
	if err := c.doJSON(ctx, "GET", "/v1/memory", nil, &out, &requestOptions{query: q}); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Memory{}
	}
	return out, nil
}

// GetMemory fetches one live memory owned by this account
// (GET /v1/memory/{id}).
func (c *Client) GetMemory(ctx context.Context, memoryID string) (*Memory, error) {
	var out Memory
	path := fmt.Sprintf("/v1/memory/%s", url.PathEscape(memoryID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// MemoryRelations lists other memories related to this one
// (GET /v1/memory/{id}/relations -- updates/extends/related/derives edges).
func (c *Client) MemoryRelations(ctx context.Context, memoryID string, limit int) (*MemoryRelationsResponse, error) {
	if limit <= 0 {
		limit = 20
	}
	q := newQuery()
	q.Set("limit", strconv.Itoa(limit))
	path := fmt.Sprintf("/v1/memory/%s/relations", url.PathEscape(memoryID))
	var out MemoryRelationsResponse
	if err := c.doJSON(ctx, "GET", path, nil, &out, &requestOptions{query: q}); err != nil {
		return nil, err
	}
	return &out, nil
}

// ForgetMemory permanently deletes one memory (DELETE /v1/memory/{id}). Not
// a soft-delete -- a forgotten memory cannot be recalled again.
func (c *Client) ForgetMemory(ctx context.Context, memoryID string) error {
	path := fmt.Sprintf("/v1/memory/%s", url.PathEscape(memoryID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}

// ExportMemories exports live memories (and their relations) for this
// account, for backup or migration (GET /v1/memory/export).
func (c *Client) ExportMemories(ctx context.Context, scope string) (map[string]any, error) {
	q := newQuery()
	if scope != "" {
		q.Set("scope", scope)
	}
	var out map[string]any
	if err := c.doJSON(ctx, "GET", "/v1/memory/export", nil, &out, &requestOptions{query: q}); err != nil {
		return nil, err
	}
	return out, nil
}

// ImportMemoriesRequest is the request body for ImportMemories
// (POST /v1/memory/import).
type ImportMemoriesRequest struct {
	Memories  []map[string]any `json:"memories"`
	Relations []map[string]any `json:"relations,omitempty"`
}

// ImportMemories imports memories (and optionally their relations)
// previously produced by ExportMemories (POST /v1/memory/import).
func (c *Client) ImportMemories(ctx context.Context, req ImportMemoriesRequest) (*MemoryIngestResponse, error) {
	var out MemoryIngestResponse
	if err := c.doJSON(ctx, "POST", "/v1/memory/import", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}
