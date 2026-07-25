package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// CreateSecretRequest is the request body for CreateSecret (POST
// /v1/secrets).
type CreateSecretRequest struct {
	// Name is the unique (per-account) name used to reference this secret
	// from CreateSandboxRequest.SecretNames and from an agent tool call as
	// {{secret:name}} in a POST /http-request body/header.
	Name string `json:"name"`
	// Value is the real credential value. Write-only -- accepted here and
	// never returned by this or any other route, including this response.
	Value string `json:"value"`
	// AllowedHosts are the destination hostnames this secret may be used
	// against via POST /http-request. Required, not optional -- an
	// unscoped secret usable against any destination defeats the point of
	// this feature. A host that resolves to a private/link-local/loopback/
	// metadata address is rejected at creation time (a best-effort
	// backstop; see docs/SECRETS-DESIGN.md §5 for why the real control is
	// the sidecar's request-time check).
	AllowedHosts []string `json:"allowed_hosts"`
	// TrustTier is only meaningful for wallet/private-key-style secrets
	// (docs/WALLET-SECRETS-DESIGN.md) -- omit for an ordinary API-key-style
	// secret. The only accepted value today is "testnet"; "mainnet" is
	// refused (422).
	TrustTier *string `json:"trust_tier,omitempty"`
}

// Secret is a secret's metadata, as returned by CreateSecret/ListSecrets.
// The raw value is never included here or anywhere else after creation.
type Secret struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	AllowedHosts []string `json:"allowed_hosts"`
	TrustTier    *string  `json:"trust_tier"`
	CreatedAt    string   `json:"created_at"`
	LastUsedAt   *string  `json:"last_used_at"`
}

// CreateSecret registers a new org-scoped secret for the
// proxy-substitution secrets broker (POST /v1/secrets,
// docs/SECRETS-DESIGN.md). Returns the created secret's metadata -- never
// the raw value.
func (c *Client) CreateSecret(ctx context.Context, req CreateSecretRequest) (*Secret, error) {
	var out Secret
	if err := c.doJSON(ctx, "POST", "/v1/secrets", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListSecrets lists secrets registered for this account
// (GET /v1/secrets). Raw values are never returned here.
func (c *Client) ListSecrets(ctx context.Context) ([]Secret, error) {
	var out []Secret
	if err := c.doJSON(ctx, "GET", "/v1/secrets", nil, &out, nil); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Secret{}
	}
	return out, nil
}

// DeleteSecret deletes a secret owned by this account
// (DELETE /v1/secrets/{id}). 404s if already gone or never owned by this
// account.
func (c *Client) DeleteSecret(ctx context.Context, secretID string) error {
	path := fmt.Sprintf("/v1/secrets/%s", url.PathEscape(secretID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}
