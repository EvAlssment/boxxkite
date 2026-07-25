package boxkite

import (
	"context"
	"encoding/json"
	"net/url"
	"strconv"
)

// Account is the identity for the API key in use (GET /v1/account).
type Account struct {
	ID        string `json:"id"`
	Email     string `json:"email"`
	CreatedAt string `json:"created_at"`
}

// Usage is the current usage against fair-use limits (GET /v1/usage), also
// returned inline on CreateSandbox's response.
type Usage struct {
	MonthlySandboxHoursUsed  float64 `json:"monthly_sandbox_hours_used"`
	MonthlySandboxHoursLimit float64 `json:"monthly_sandbox_hours_limit"`
	ConcurrentSandboxes      int     `json:"concurrent_sandboxes"`
	ConcurrentSandboxesLimit int     `json:"concurrent_sandboxes_limit"`
}

// MessageResponse is a generic ack body (e.g. password-reset request,
// which always returns the same message regardless of whether the email
// is registered).
type MessageResponse struct {
	Message string `json:"message"`
}

// Account returns the account identity for the API key in use.
func (c *Client) Account(ctx context.Context) (*Account, error) {
	var out Account
	if err := c.doJSON(ctx, "GET", "/v1/account", nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// Usage returns current usage against fair-use limits.
func (c *Client) Usage(ctx context.Context) (*Usage, error) {
	var out Usage
	if err := c.doJSON(ctx, "GET", "/v1/usage", nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// RequestPasswordReset requests a password-reset email
// (POST /v1/auth/password-reset/request). Opt-in on the control-plane
// (BOXKITE_PASSWORD_RESET_ENABLED); returns an *APIError with
// StatusCode 404 and Code "feature_disabled" if the deployment hasn't
// enabled it. Always returns the same message whether or not the email is
// registered, so this call can never be used to enumerate accounts.
func (c *Client) RequestPasswordReset(ctx context.Context, email string) (*MessageResponse, error) {
	var out MessageResponse
	body := map[string]string{"email": email}
	if err := c.doJSON(ctx, "POST", "/v1/auth/password-reset/request", body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ConfirmPasswordReset consumes a single-use token minted by
// RequestPasswordReset and sets a new password
// (POST /v1/auth/password-reset/confirm). Also revokes every outstanding
// refresh token for the account, if refresh tokens are enabled
// server-side.
func (c *Client) ConfirmPasswordReset(ctx context.Context, token, newPassword string) (*MessageResponse, error) {
	var out MessageResponse
	body := map[string]string{"token": token, "new_password": newPassword}
	if err := c.doJSON(ctx, "POST", "/v1/auth/password-reset/confirm", body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// VerifyEmail consumes a single-use email-verification token
// (POST /v1/auth/verify-email), opt-in
// (BOXKITE_EMAIL_VERIFICATION_ENABLED).
func (c *Client) VerifyEmail(ctx context.Context, token string) (*MessageResponse, error) {
	var out MessageResponse
	body := map[string]string{"token": token}
	if err := c.doJSON(ctx, "POST", "/v1/auth/verify-email", body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ResendVerification re-sends the verification email for the
// dashboard-JWT-authenticated account (POST /v1/auth/resend-verification).
// accessToken is a dashboard session token (the JWT returned by
// /v1/auth/login or /v1/auth/signup) -- a different, non-interchangeable
// credential type from this Client's own apiKey, so it overrides this
// call's Authorization header rather than using the Client's apiKey.
func (c *Client) ResendVerification(ctx context.Context, accessToken string) (*MessageResponse, error) {
	var out MessageResponse
	opts := &requestOptions{authOverride: accessToken}
	if err := c.doJSON(ctx, "POST", "/v1/auth/resend-verification", nil, &out, opts); err != nil {
		return nil, err
	}
	return &out, nil
}

// TokenPair is the response shape from RefreshToken -- a brand new
// access_token + refresh_token pair, plus the account identity.
type TokenPair struct {
	AccessToken  string  `json:"access_token"`
	TokenType    string  `json:"token_type"`
	ExpiresIn    int     `json:"expires_in"`
	RefreshToken *string `json:"refresh_token"`
	Account      Account `json:"account"`
}

// RefreshToken exchanges a still-valid refresh token for a brand new
// access_token + refresh_token pair (POST /v1/auth/refresh), opt-in
// (BOXKITE_REFRESH_TOKENS_ENABLED). Revokes the presented token in the
// same request (rotation, not reuse) -- store the new RefreshToken from
// the response and discard the one presented here.
func (c *Client) RefreshToken(ctx context.Context, refreshToken string) (*TokenPair, error) {
	var out TokenPair
	body := map[string]string{"refresh_token": refreshToken}
	if err := c.doJSON(ctx, "POST", "/v1/auth/refresh", body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// Logout revokes one refresh token immediately (POST /v1/auth/logout),
// opt-in (BOXKITE_REFRESH_TOKENS_ENABLED). Always succeeds (204) whether
// or not the token was valid -- never leaks which.
func (c *Client) Logout(ctx context.Context, refreshToken string) error {
	body := map[string]string{"refresh_token": refreshToken}
	return c.doJSON(ctx, "POST", "/v1/auth/logout", body, nil, nil)
}

// AllowedCommandRule is one account-level command allowlist rule -- either
// a bare command name (Command set, ArgsAllow/ArgsDeny both empty) or a
// command name plus argument allow/deny regex lists. The control-plane's
// own schema (AllowedCommandsRequest.rules: list[str | AllowedCommandRule]
// in control-plane/src/control_plane/schemas.py) accepts and returns rules
// in either shape -- UnmarshalJSON below decodes both into the same Go
// struct; MarshalJSON always emits the object form, which the
// control-plane accepts identically to the bare-string form for a rule
// with no argument constraints.
type AllowedCommandRule struct {
	Command   string   `json:"command"`
	ArgsAllow []string `json:"args_allow,omitempty"`
	ArgsDeny  []string `json:"args_deny,omitempty"`
}

// allowedCommandRuleAlias avoids infinite recursion when UnmarshalJSON
// below delegates the object-shaped case back to encoding/json.
type allowedCommandRuleAlias AllowedCommandRule

// UnmarshalJSON accepts either a bare JSON string (a plain command name,
// no argument constraints) or a JSON object
// ({"command", "args_allow"?, "args_deny"?}).
func (r *AllowedCommandRule) UnmarshalJSON(data []byte) error {
	var command string
	if err := json.Unmarshal(data, &command); err == nil {
		r.Command = command
		r.ArgsAllow = nil
		r.ArgsDeny = nil
		return nil
	}
	var alias allowedCommandRuleAlias
	if err := json.Unmarshal(data, &alias); err != nil {
		return err
	}
	*r = AllowedCommandRule(alias)
	return nil
}

// AllowedCommandsResponse is the body shape shared by
// GetAllowedCommands/SetAllowedCommands.
type AllowedCommandsResponse struct {
	Rules []AllowedCommandRule `json:"rules"`
}

// GetAllowedCommands returns the current per-account command allowlist
// (GET /v1/account/allowed-commands). An empty Rules means unrestricted --
// the default for every account. This allowlist is an opt-in guardrail,
// not a sandbox-escape boundary.
func (c *Client) GetAllowedCommands(ctx context.Context) (*AllowedCommandsResponse, error) {
	var out AllowedCommandsResponse
	if err := c.doJSON(ctx, "GET", "/v1/account/allowed-commands", nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// SetAllowedCommands replaces the per-account command allowlist wholesale
// (PUT /v1/account/allowed-commands). rules must be non-empty -- use
// ClearAllowedCommands to reset to unrestricted.
func (c *Client) SetAllowedCommands(ctx context.Context, rules []AllowedCommandRule) (*AllowedCommandsResponse, error) {
	var out AllowedCommandsResponse
	body := AllowedCommandsResponse{Rules: rules}
	if err := c.doJSON(ctx, "PUT", "/v1/account/allowed-commands", body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ClearAllowedCommands removes the per-account command allowlist
// (DELETE /v1/account/allowed-commands), back to the unrestricted default.
func (c *Client) ClearAllowedCommands(ctx context.Context) error {
	return c.doJSON(ctx, "DELETE", "/v1/account/allowed-commands", nil, nil, nil)
}

func boolQueryValue(v bool) string {
	return strconv.FormatBool(v)
}

func newQuery() url.Values {
	return url.Values{}
}
