package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// PreviewURL is the response shape from CreatePreviewURL.
type PreviewURL struct {
	URL       string `json:"url"`
	ExpiresAt string `json:"expires_at"`
	TokenID   string `json:"token_id"`
}

// CreatePreviewURL mints a signed, time-limited URL that proxies HTTP
// traffic to a port a background process opened inside this session (see
// StartProcessOptions.ExposePort) (POST /v1/sandboxes/{id}/preview/{port}).
// The returned URL carries its own authorization -- no API key is required
// to use it, only to mint it.
//
// ttlSeconds bounds how long the minted URL stays valid (30-86400).
// Defaults to 900 (15 minutes) server-side when nil.
func (c *Client) CreatePreviewURL(ctx context.Context, sessionID string, port int, ttlSeconds *int) (*PreviewURL, error) {
	body := map[string]any{}
	if ttlSeconds != nil {
		body["ttl_seconds"] = *ttlSeconds
	}
	var out PreviewURL
	path := fmt.Sprintf("/v1/sandboxes/%s/preview/%d", url.PathEscape(sessionID), port)
	if err := c.doJSON(ctx, "POST", path, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// PreviewRevokeResult is the response shape from RevokePreviewURL.
type PreviewRevokeResult struct {
	Revoked bool   `json:"revoked"`
	TokenID string `json:"token_id"`
}

// RevokePreviewURL invalidates one specific preview-URL token (its
// TokenID from CreatePreviewURL) before its TTL expires, without tearing
// down the sandbox session and without affecting any other preview token
// minted for the same session/port
// (POST /v1/sandboxes/{id}/preview/{port}/revoke). Idempotent: revoking
// an already-revoked, already-expired, or unrecognized tokenID still
// returns Revoked=true rather than erroring.
func (c *Client) RevokePreviewURL(ctx context.Context, sessionID string, port int, tokenID string) (*PreviewRevokeResult, error) {
	body := map[string]string{"token_id": tokenID}
	var out PreviewRevokeResult
	path := fmt.Sprintf("/v1/sandboxes/%s/preview/%d/revoke", url.PathEscape(sessionID), port)
	if err := c.doJSON(ctx, "POST", path, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}
