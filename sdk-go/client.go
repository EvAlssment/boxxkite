// Package boxkite is a Go client for a hosted boxkite control-plane -- the
// same v1 HTTP API sdk-python's BoxkiteClient and sdk-js's BoxkiteClient
// wrap (see docs/API.md in the boxkite repo). It is a thin request/response
// client: create sandboxes, run commands, edit files, stream the audit log,
// take over a sandbox's shell over a WebSocket -- all over HTTP against
// *someone else's* running control-plane. It is not a client for the
// boxkite package itself (SandboxManager, embedded directly against a
// Kubernetes cluster).
package boxkite

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// DefaultTimeout is the default per-request timeout, matching
// sdk-python/sdk-js's 30-second default.
const DefaultTimeout = 30 * time.Second

// ExecTimeoutHeadroom is added on top of a caller-supplied exec/http-request
// timeout so the HTTP client's own deadline never fires before the
// control-plane's -- mirrors sdk-python's EXEC_TIMEOUT_HEADROOM / sdk-js's
// EXEC_TIMEOUT_HEADROOM_MS.
const ExecTimeoutHeadroom = 15 * time.Second

var localhostHostnames = map[string]bool{
	"localhost": true,
	"127.0.0.1": true,
	"::1":       true,
}

// Client is a synchronous client for a hosted boxkite control-plane. Safe
// for concurrent use by multiple goroutines (it holds no mutable state
// beyond a shared *http.Client).
type Client struct {
	baseURL    string
	apiKey     string
	httpClient *http.Client
	timeout    time.Duration
	wsDialer   *websocket.Dialer
	retry      *RetryConfig
}

// Option configures a Client constructed via NewClient.
type Option func(*Client)

// WithHTTPClient overrides the *http.Client used for requests. Primarily
// useful for tests (point it at an httptest.Server) or for customizing
// transport-level behavior (proxies, custom TLS config).
func WithHTTPClient(hc *http.Client) Option {
	return func(c *Client) {
		c.httpClient = hc
	}
}

// WithTimeout overrides the default per-request timeout (DefaultTimeout).
func WithTimeout(d time.Duration) Option {
	return func(c *Client) {
		c.timeout = d
	}
}

// WithWebSocketDialer overrides the *websocket.Dialer used by Takeover.
// Primarily useful for tests.
func WithWebSocketDialer(d *websocket.Dialer) Option {
	return func(c *Client) {
		c.wsDialer = d
	}
}

// WithRetry enables automatic retry of transient failures. Retry is
// off by default -- pass this option (ideally starting from
// DefaultRetryConfig()) to turn it on. Only idempotent verbs are retried,
// and only on a connection failure or a retriable status (429 + transient
// 5xx); a non-idempotent POST is never retried, so this cannot
// double-create a resource. Zero-valued BackoffBase/BackoffMax/Statuses/
// Methods fields are filled in from the defaults, so
// WithRetry(RetryConfig{MaxRetries: 3}) is valid -- but RespectRetryAfter
// stays false unless set, so prefer DefaultRetryConfig() as a base.
func WithRetry(cfg RetryConfig) Option {
	return func(c *Client) {
		normalized := cfg
		if normalized.BackoffBase <= 0 {
			normalized.BackoffBase = defaultRetryBackoffBase
		}
		if normalized.BackoffMax <= 0 {
			normalized.BackoffMax = defaultRetryBackoffMax
		}
		if normalized.Statuses == nil {
			normalized.Statuses = defaultRetryStatuses
		}
		if normalized.Methods == nil {
			normalized.Methods = idempotentMethods
		}
		c.retry = &normalized
	}
}

// NewClient constructs a Client for a hosted control-plane at baseURL,
// authenticating every request with apiKey (a boxkite account API key,
// `bxk_live_...`, sent as `Authorization: Bearer <apiKey>`).
//
// baseURL must be https://, or http://localhost (local dev only) -- an
// apiKey is a full-privilege, long-lived credential, so anything else would
// put it on the wire in cleartext. Mirrors sdk-python's
// _validate_base_url_scheme / sdk-js's validateBaseUrlScheme.
func NewClient(baseURL, apiKey string, opts ...Option) (*Client, error) {
	if err := validateBaseURLScheme(baseURL); err != nil {
		return nil, err
	}
	c := &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		timeout: DefaultTimeout,
	}
	for _, opt := range opts {
		opt(c)
	}
	if c.httpClient == nil {
		c.httpClient = &http.Client{Timeout: c.timeout}
	}
	if c.wsDialer == nil {
		c.wsDialer = websocket.DefaultDialer
	}
	return c, nil
}

func validateBaseURLScheme(baseURL string) error {
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return fmt.Errorf("invalid base_url %q: %w", baseURL, err)
	}
	if parsed.Scheme == "https" {
		return nil
	}
	if parsed.Scheme == "http" && localhostHostnames[parsed.Hostname()] {
		return nil
	}
	return fmt.Errorf(
		"refusing to use non-https base_url %q: this would send your API key in "+
			"cleartext. Use an https:// URL, or http://localhost (local dev only)",
		baseURL,
	)
}

// wsURL rewrites the client's baseURL (https:// or http://, already
// validated by validateBaseURLScheme) plus a path suffix into the
// corresponding wss:///ws:// URL.
func (c *Client) wsURL(path string) string {
	switch {
	case strings.HasPrefix(c.baseURL, "https://"):
		return "wss://" + strings.TrimPrefix(c.baseURL, "https://") + path
	case strings.HasPrefix(c.baseURL, "http://"):
		return "ws://" + strings.TrimPrefix(c.baseURL, "http://") + path
	default:
		// Unreachable: validateBaseURLScheme already restricted baseURL to
		// one of the two schemes above.
		return c.baseURL + path
	}
}

// errorEnvelope mirrors the control-plane's `{"error": {code, message}}`
// error response shape (docs/API.md's "Error codes" section).
type errorEnvelope struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// requestOptions carries the handful of per-call overrides needed by exec
// and http_request (a longer read timeout, an Authorization override for
// resend_verification's dashboard-JWT call).
type requestOptions struct {
	timeout      time.Duration
	authOverride string
	query        url.Values
}

// doJSON issues an HTTP request against the control-plane and decodes a
// successful JSON response body into out (a pointer; pass nil for routes
// with no response body, e.g. a 204). reqBody, if non-nil, is marshaled as
// the JSON request body. When retry is enabled (see WithRetry) a transient
// failure on an idempotent verb is re-attempted with backoff, honoring ctx
// cancellation between attempts.
func (c *Client) doJSON(ctx context.Context, method, path string, reqBody any, out any, opts *requestOptions) error {
	var encoded []byte
	if reqBody != nil {
		var err error
		encoded, err = json.Marshal(reqBody)
		if err != nil {
			return fmt.Errorf("boxkite: encoding request body: %w", err)
		}
	}

	fullURL := c.baseURL + path
	if opts != nil && len(opts.query) > 0 {
		fullURL += "?" + opts.query.Encode()
	}

	httpClient := c.httpClient
	if opts != nil && opts.timeout > 0 {
		clientCopy := *c.httpClient
		clientCopy.Timeout = opts.timeout
		httpClient = &clientCopy
	}

	for attempt := 0; ; attempt++ {
		var bodyReader io.Reader
		if encoded != nil {
			bodyReader = bytes.NewReader(encoded)
		}
		req, err := http.NewRequestWithContext(ctx, method, fullURL, bodyReader)
		if err != nil {
			return &ConnectionError{Message: err.Error(), Err: err}
		}
		authValue := "Bearer " + c.apiKey
		if opts != nil && opts.authOverride != "" {
			authValue = "Bearer " + opts.authOverride
		}
		req.Header.Set("Authorization", authValue)
		if encoded != nil {
			req.Header.Set("Content-Type", "application/json")
		}

		resp, err := httpClient.Do(req)
		if err != nil {
			// status 0 == no response reached us (DNS/TLS/timeout/refused).
			if ctx.Err() == nil && shouldRetry(c.retry, method, attempt, 0) {
				if serr := sleepWithContext(ctx, retryDelay(c.retry, attempt, -1)); serr == nil {
					continue
				}
			}
			return &ConnectionError{Message: err.Error(), Err: err}
		}

		respBody, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			return &ConnectionError{Message: readErr.Error(), Err: readErr}
		}

		if resp.StatusCode >= 400 {
			if shouldRetry(c.retry, method, attempt, resp.StatusCode) {
				delay := retryDelay(c.retry, attempt, retryAfterFrom(c.retry, resp))
				if serr := sleepWithContext(ctx, delay); serr == nil {
					continue
				}
			}
			return apiErrorFromResponse(resp.StatusCode, respBody)
		}

		if out != nil && len(respBody) > 0 {
			if err := json.Unmarshal(respBody, out); err != nil {
				return fmt.Errorf("boxkite: decoding response body: %w", err)
			}
		}
		return nil
	}
}

func apiErrorFromResponse(statusCode int, body []byte) error {
	code := "error"
	message := fmt.Sprintf("HTTP %d", statusCode)
	var envelope errorEnvelope
	if err := json.Unmarshal(body, &envelope); err == nil && envelope.Error.Code != "" {
		code = envelope.Error.Code
		message = envelope.Error.Message
	}
	return &APIError{StatusCode: statusCode, Code: code, Message: message}
}

const (
	defaultRetryMaxRetries  = 2
	defaultRetryBackoffBase = 500 * time.Millisecond
	defaultRetryBackoffMax  = 30 * time.Second
)

// defaultRetryStatuses is 429 (rate limited) plus the transient 5xx family.
// Retrying a 500 that reflects a deterministic server bug won't help, but
// these are the codes a control-plane returns for load/restart/upstream
// blips worth re-attempting.
var defaultRetryStatuses = map[int]bool{429: true, 500: true, 502: true, 503: true, 504: true}

// idempotentMethods are the only verbs safe to blind-retry: a retried POST
// could double-create a sandbox/secret/webhook. PUT/DELETE on this API are
// idempotent by resource id, so they stay in.
var idempotentMethods = map[string]bool{"GET": true, "HEAD": true, "PUT": true, "DELETE": true, "OPTIONS": true}

// RetryConfig configures automatic retry of transient failures. Enable it
// via WithRetry; retry is off entirely when no RetryConfig is set.
type RetryConfig struct {
	// MaxRetries is the number of retries after the initial attempt (so a
	// total of MaxRetries+1 attempts). 0 disables retry.
	MaxRetries int
	// BackoffBase is the base of the exponential backoff (delay for the
	// first retry is drawn from [0, BackoffBase)).
	BackoffBase time.Duration
	// BackoffMax caps any single backoff (and any honored Retry-After).
	BackoffMax time.Duration
	// Statuses is the set of response status codes that trigger a retry.
	Statuses map[int]bool
	// Methods is the set of (idempotent) HTTP verbs eligible for retry.
	Methods map[string]bool
	// RespectRetryAfter honors a server-supplied Retry-After header (delta
	// seconds or HTTP-date) in preference to computed backoff.
	RespectRetryAfter bool
}

// DefaultRetryConfig is the recommended starting point for WithRetry: 2
// retries, exponential backoff with full jitter (500ms base, 30s cap),
// retry on 429/500/502/503/504 for idempotent verbs, Retry-After honored.
func DefaultRetryConfig() RetryConfig {
	return RetryConfig{
		MaxRetries:        defaultRetryMaxRetries,
		BackoffBase:       defaultRetryBackoffBase,
		BackoffMax:        defaultRetryBackoffMax,
		Statuses:          defaultRetryStatuses,
		Methods:           idempotentMethods,
		RespectRetryAfter: true,
	}
}

func shouldRetry(cfg *RetryConfig, method string, attempt, status int) bool {
	if cfg == nil || attempt >= cfg.MaxRetries {
		return false
	}
	if !cfg.Methods[strings.ToUpper(method)] {
		return false
	}
	if status == 0 {
		return true
	}
	return cfg.Statuses[status]
}

// retryDelay returns the wait before the next attempt (0-indexed attempt). A
// non-negative retryAfter (from the server) wins over computed backoff;
// otherwise full-jitter exponential backoff, capped at BackoffMax.
func retryDelay(cfg *RetryConfig, attempt int, retryAfter time.Duration) time.Duration {
	if retryAfter >= 0 {
		if retryAfter > cfg.BackoffMax {
			return cfg.BackoffMax
		}
		return retryAfter
	}
	ceiling := cfg.BackoffBase << uint(attempt)
	if ceiling <= 0 || ceiling > cfg.BackoffMax {
		ceiling = cfg.BackoffMax
	}
	return time.Duration(rand.Float64() * float64(ceiling))
}

func retryAfterFrom(cfg *RetryConfig, resp *http.Response) time.Duration {
	if cfg == nil || !cfg.RespectRetryAfter {
		return -1
	}
	return parseRetryAfter(resp)
}

// parseRetryAfter parses a Retry-After header (delta-seconds or HTTP-date)
// into a non-negative wait, or -1 when absent/unparseable.
func parseRetryAfter(resp *http.Response) time.Duration {
	value := strings.TrimSpace(resp.Header.Get("Retry-After"))
	if value == "" {
		return -1
	}
	if secs, err := strconv.Atoi(value); err == nil {
		if secs < 0 {
			secs = 0
		}
		return time.Duration(secs) * time.Second
	}
	if t, err := http.ParseTime(value); err == nil {
		if d := time.Until(t); d > 0 {
			return d
		}
		return 0
	}
	return -1
}

// sleepWithContext waits d, returning early with ctx.Err() if the context is
// cancelled first -- honoring ctx cancellation between retry attempts.
func sleepWithContext(ctx context.Context, d time.Duration) error {
	if d < 0 {
		d = 0
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
