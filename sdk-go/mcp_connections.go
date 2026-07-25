package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// MCPConnection is an outbound-MCP connection grant, as returned by
// CreateMCPConnection/ListMCPConnections (GitHub issues #116/#117,
// docs/OUTBOUND-MCP-DESIGN.md).
type MCPConnection struct {
	ID         string  `json:"id"`
	Label      string  `json:"label"`
	CatalogID  string  `json:"catalog_id"`
	Host       string  `json:"host"`
	CreatedAt  string  `json:"created_at"`
	LastUsedAt *string `json:"last_used_at"`
}

// CreateMCPConnection grants this account access to one curated
// outbound-MCP catalog entry (POST /v1/mcp-connections). label is a
// unique (per-account) name for this connection -- pass it in
// CreateSandboxRequest.MCPConnectionNames to grant a session network
// egress to it. catalogID is one of "slack", "notion", "linear", "github"
// -- restricted to boxkite's own reviewed allowlist, never a
// caller-supplied hostname.
//
// Note: this only widens a granted session's per-pod NetworkPolicy
// egress allowlist to the connection's catalog hostname -- there is no
// MCP-proxy transport yet, so this does not yet let an agent speak MCP
// protocol to the destination.
func (c *Client) CreateMCPConnection(ctx context.Context, label, catalogID string) (*MCPConnection, error) {
	body := map[string]string{"label": label, "catalog_id": catalogID}
	var out MCPConnection
	if err := c.doJSON(ctx, "POST", "/v1/mcp-connections", body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListMCPConnections lists outbound-MCP connection grants for this
// account (GET /v1/mcp-connections).
func (c *Client) ListMCPConnections(ctx context.Context) ([]MCPConnection, error) {
	var out []MCPConnection
	if err := c.doJSON(ctx, "GET", "/v1/mcp-connections", nil, &out, nil); err != nil {
		return nil, err
	}
	if out == nil {
		out = []MCPConnection{}
	}
	return out, nil
}

// DeleteMCPConnection deletes an outbound-MCP connection grant owned by
// this account (DELETE /v1/mcp-connections/{id}). 404s if already gone or
// never owned by this account.
func (c *Client) DeleteMCPConnection(ctx context.Context, connectionID string) error {
	path := fmt.Sprintf("/v1/mcp-connections/%s", url.PathEscape(connectionID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}
