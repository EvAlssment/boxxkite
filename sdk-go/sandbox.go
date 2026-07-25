package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// CreateSandboxRequest is the request body for CreateSandbox
// (POST /v1/sandboxes). All fields are optional -- a zero-value
// CreateSandboxRequest{} creates one default "small" sandbox. Use Ptr(...)
// to populate the pointer-typed fields, e.g.
// boxkite.CreateSandboxRequest{Size: boxkite.Ptr("medium")}.
type CreateSandboxRequest struct {
	// Label is an optional human-readable label for the sandbox.
	Label *string `json:"label,omitempty"`
	// Size is one of "small", "medium", "large". Defaults to "small"
	// server-side when omitted.
	Size *string `json:"size,omitempty"`
	// StorageGB is the requested persistent storage in GB.
	StorageGB *float64 `json:"storage_gb,omitempty"`
	// LifetimeMinutes is the maximum lifetime of the sandbox in minutes
	// before automatic teardown.
	LifetimeMinutes *int `json:"lifetime_minutes,omitempty"`
	// Count is the number of sandboxes to create in this request.
	Count *int `json:"count,omitempty"`
	// SecretNames names this account's secrets (see docs/SECRETS-DESIGN.md)
	// this session should be granted access to via the sidecar's
	// secrets-broker HTTPRequest tool. A name that doesn't exist for this
	// account 404s before any sandbox is created.
	SecretNames []string `json:"secret_names,omitempty"`
	// ImageID is the id of a completed custom image built via CreateImage.
	// If set, the sandbox uses that digest-pinned image instead of the
	// operator's default. 404s if not owned by this account or not yet
	// status "completed".
	ImageID *string `json:"image_id,omitempty"`
	// MCPConnectionNames names this account's outbound-MCP connections
	// (see CreateMCPConnection) this session should be granted network
	// egress to. A name that doesn't exist for this account 404s before
	// any sandbox is created, same precedent as SecretNames.
	MCPConnectionNames []string `json:"mcp_connection_names,omitempty"`
	// VolumeMounts is an optional {volume_id: mount_path} mapping of
	// independent PVC-backed volumes (see CreateVolume) to mount into
	// this sandbox.
	VolumeMounts map[string]string `json:"volume_mounts,omitempty"`
	// GPUCount is opt-in and experimental (docs/GPU-SUPPORT-SCOPING.md) --
	// it requests this many GPUs as a Kubernetes extended-resource limit.
	// 422s (gpu_support_disabled) unless the deployment has
	// BOXKITE_GPU_ENABLED set and a GPU-equipped node pool with a device
	// plugin provisioned; not verified against real GPU hardware in this
	// codebase.
	GPUCount *int `json:"gpu_count,omitempty"`
}

// SandboxConnectInfo is opaque connection metadata for operators with
// cluster access -- external callers operate on a session exclusively
// through the exec/files/* routes, never this directly.
type SandboxConnectInfo struct {
	PodName string `json:"pod_name"`
	Note    string `json:"note"`
}

// Sandbox is one sandbox session (GET/POST /v1/sandboxes response shape).
type Sandbox struct {
	ID          string              `json:"id"`
	Status      string              `json:"status"`
	Label       *string             `json:"label"`
	CreatedAt   string              `json:"created_at"`
	DestroyedAt *string             `json:"destroyed_at"`
	ExpiresAt   *string             `json:"expires_at"`
	Connect     *SandboxConnectInfo `json:"connect,omitempty"`
	Usage       *Usage              `json:"usage,omitempty"`
}

// CreateSandbox creates a new sandbox session (POST /v1/sandboxes), or a
// batch of sandbox sessions when req.Count is greater than 1. Each session
// in a batch is created and limit-checked one at a time, so a later item
// can still fail the concurrent-sandbox or monthly-usage cap even if
// earlier items succeeded -- see CreateSandboxes for the batch form.
func (c *Client) CreateSandbox(ctx context.Context, req CreateSandboxRequest) (*Sandbox, error) {
	var out Sandbox
	if err := c.doJSON(ctx, "POST", "/v1/sandboxes", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// CreateSandboxes creates a batch of sandbox sessions in one call
// (POST /v1/sandboxes with req.Count > 1) and decodes the resulting array
// response. Use CreateSandbox instead for the common req.Count == 1 (or
// unset) case, which returns a single object rather than an array.
func (c *Client) CreateSandboxes(ctx context.Context, req CreateSandboxRequest) ([]Sandbox, error) {
	var out []Sandbox
	if err := c.doJSON(ctx, "POST", "/v1/sandboxes", req, &out, nil); err != nil {
		return nil, err
	}
	return out, nil
}

// GetSandbox fetches one sandbox session by id (GET /v1/sandboxes/{id}).
// Unlike the exec/file routes, this resolves destroyed sessions too -- it
// is a lookup, not an operational route that requires a live pod.
func (c *Client) GetSandbox(ctx context.Context, sessionID string) (*Sandbox, error) {
	var out Sandbox
	path := fmt.Sprintf("/v1/sandboxes/%s", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListSandboxes lists sandbox sessions owned by the authenticated account
// (GET /v1/sandboxes). Pass activeOnly=true to restrict to currently-active
// sessions.
func (c *Client) ListSandboxes(ctx context.Context, activeOnly bool) ([]Sandbox, error) {
	var out []Sandbox
	q := newQuery()
	q.Set("active_only", boolQueryValue(activeOnly))
	opts := &requestOptions{query: q}
	if err := c.doJSON(ctx, "GET", "/v1/sandboxes", nil, &out, opts); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Sandbox{}
	}
	return out, nil
}

// DestroySandbox tears down a sandbox session (DELETE /v1/sandboxes/{id}).
func (c *Client) DestroySandbox(ctx context.Context, sessionID string) error {
	path := fmt.Sprintf("/v1/sandboxes/%s", url.PathEscape(sessionID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}
