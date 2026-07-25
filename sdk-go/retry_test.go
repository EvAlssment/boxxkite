package boxkite

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// fastRetry is DefaultRetryConfig with sub-millisecond backoff so the suite
// never actually waits.
func fastRetry() RetryConfig {
	return RetryConfig{
		MaxRetries:        2,
		BackoffBase:       time.Millisecond,
		BackoffMax:        2 * time.Millisecond,
		RespectRetryAfter: true,
	}
}

func retryTestClient(t *testing.T, handler http.HandlerFunc, cfg RetryConfig) (*Client, func()) {
	t.Helper()
	server := httptest.NewServer(handler)
	client, err := NewClient(server.URL, "bxk_live_test", WithRetry(cfg))
	if err != nil {
		server.Close()
		t.Fatalf("NewClient: %v", err)
	}
	return client, server.Close
}

func TestRetry_RetriesTransient5xxThenSucceeds(t *testing.T) {
	var calls atomic.Int32
	client, closeFn := retryTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			writeJSON(t, w, 503, `{"error":{"code":"unavailable","message":"down"}}`)
			return
		}
		writeJSON(t, w, 200, `{"id":"acct-1","email":"a@example.com","created_at":"2026-01-01T00:00:00Z"}`)
	}, fastRetry())
	defer closeFn()

	acct, err := client.Account(context.Background())
	if err != nil {
		t.Fatalf("Account: %v", err)
	}
	if acct.ID != "acct-1" {
		t.Fatalf("unexpected account: %+v", acct)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("expected 2 attempts, got %d", got)
	}
}

func TestRetry_Retries429(t *testing.T) {
	var calls atomic.Int32
	client, closeFn := retryTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			writeJSON(t, w, 429, `{"error":{"code":"rate_limited","message":"slow"}}`)
			return
		}
		writeJSON(t, w, 200, `{"id":"acct-1","email":"a@example.com","created_at":"2026-01-01T00:00:00Z"}`)
	}, fastRetry())
	defer closeFn()

	if _, err := client.Account(context.Background()); err != nil {
		t.Fatalf("Account: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("expected 2 attempts, got %d", got)
	}
}

func TestRetry_GivesUpAfterMaxRetries(t *testing.T) {
	var calls atomic.Int32
	client, closeFn := retryTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		writeJSON(t, w, 503, `{"error":{"code":"unavailable","message":"down"}}`)
	}, fastRetry())
	defer closeFn()

	_, err := client.Account(context.Background())
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T: %v", err, err)
	}
	if apiErr.StatusCode != 503 {
		t.Fatalf("expected 503, got %d", apiErr.StatusCode)
	}
	if got := calls.Load(); got != 3 { // initial + 2 retries
		t.Fatalf("expected 3 attempts, got %d", got)
	}
}

func TestRetry_DisabledByDefault(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		writeJSON(t, w, 503, `{"error":{"code":"unavailable","message":"down"}}`)
	}))
	defer server.Close()

	client, err := NewClient(server.URL, "bxk_live_test")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if _, err := client.Account(context.Background()); err == nil {
		t.Fatal("expected an error")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("expected 1 attempt when retry disabled, got %d", got)
	}
}

func TestRetry_DoesNotRetryNonIdempotentPOST(t *testing.T) {
	var calls atomic.Int32
	client, closeFn := retryTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		writeJSON(t, w, 503, `{"error":{"code":"unavailable","message":"down"}}`)
	}, fastRetry())
	defer closeFn()

	if _, err := client.CreateSandbox(context.Background(), CreateSandboxRequest{}); err == nil {
		t.Fatal("expected an error")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("POST must not be blind-retried; expected 1 attempt, got %d", got)
	}
}

func TestRetry_DoesNotRetry4xxOtherThan429(t *testing.T) {
	var calls atomic.Int32
	client, closeFn := retryTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		writeJSON(t, w, 404, `{"error":{"code":"not_found","message":"gone"}}`)
	}, fastRetry())
	defer closeFn()

	if _, err := client.GetSandbox(context.Background(), "s1"); err == nil {
		t.Fatal("expected an error")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("expected 1 attempt for a 404, got %d", got)
	}
}

func TestRetry_RetriesConnectionError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"id":"acct-1","email":"a@example.com","created_at":"2026-01-01T00:00:00Z"}`)
	}))
	defer server.Close()

	transport := &flakyTransport{failures: 1, inner: http.DefaultTransport}
	client, err := NewClient(server.URL, "bxk_live_test",
		WithHTTPClient(&http.Client{Transport: transport}), WithRetry(fastRetry()))
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	if _, err := client.Account(context.Background()); err != nil {
		t.Fatalf("Account: %v", err)
	}
	if transport.calls != 2 {
		t.Fatalf("expected the connection error to be retried once (2 calls), got %d", transport.calls)
	}
}

func TestRetry_HonorsContextCancellationBetweenAttempts(t *testing.T) {
	client, closeFn := retryTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 503, `{"error":{"code":"unavailable","message":"down"}}`)
	}, RetryConfig{MaxRetries: 5, BackoffBase: time.Second, BackoffMax: 5 * time.Second})
	defer closeFn()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := client.Account(ctx)
	elapsed := time.Since(start)

	if _, ok := err.(*APIError); !ok {
		t.Fatalf("expected the last *APIError to surface, got %T: %v", err, err)
	}
	if elapsed > 500*time.Millisecond {
		t.Fatalf("ctx cancellation should have cut backoff short; took %v", elapsed)
	}
}

type flakyTransport struct {
	failures int
	calls    int
	inner    http.RoundTripper
}

func (t *flakyTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	t.calls++
	if t.calls <= t.failures {
		return nil, fmt.Errorf("dial tcp: connection refused")
	}
	return t.inner.RoundTrip(req)
}

func TestShouldRetry(t *testing.T) {
	cfg := DefaultRetryConfig()
	cases := []struct {
		method string
		attempt,
		status int
		want bool
	}{
		{"GET", 0, 503, true},
		{"GET", 0, 0, true}, // connection error
		{"POST", 0, 503, false},
		{"GET", 2, 503, false}, // attempt == MaxRetries
		{"GET", 0, 404, false},
		{"delete", 0, 500, true}, // case-insensitive
	}
	for _, tc := range cases {
		if got := shouldRetry(&cfg, tc.method, tc.attempt, tc.status); got != tc.want {
			t.Errorf("shouldRetry(%s, attempt=%d, status=%d) = %t, want %t", tc.method, tc.attempt, tc.status, got, tc.want)
		}
	}
	if shouldRetry(nil, "GET", 0, 503) {
		t.Error("shouldRetry(nil, ...) must be false")
	}
}

func TestRetryDelay_PrefersRetryAfterAndCaps(t *testing.T) {
	cfg := RetryConfig{BackoffBase: time.Second, BackoffMax: 10 * time.Second}
	if got := retryDelay(&cfg, 3, 4*time.Second); got != 4*time.Second {
		t.Errorf("expected Retry-After 4s to be used, got %v", got)
	}
	if got := retryDelay(&cfg, 3, 100*time.Second); got != 10*time.Second {
		t.Errorf("expected Retry-After to be capped at BackoffMax, got %v", got)
	}
	for i := 0; i < 100; i++ {
		if got := retryDelay(&cfg, 20, -1); got < 0 || got > cfg.BackoffMax {
			t.Fatalf("backoff out of range: %v", got)
		}
	}
}

func TestParseRetryAfter(t *testing.T) {
	mk := func(v string) *http.Response {
		resp := &http.Response{Header: http.Header{}}
		if v != "" {
			resp.Header.Set("Retry-After", v)
		}
		return resp
	}
	if got := parseRetryAfter(mk("5")); got != 5*time.Second {
		t.Errorf("seconds: got %v", got)
	}
	if got := parseRetryAfter(mk("")); got != -1 {
		t.Errorf("absent header should be -1, got %v", got)
	}
	if got := parseRetryAfter(mk("garbage")); got != -1 {
		t.Errorf("unparseable header should be -1, got %v", got)
	}
	if got := parseRetryAfter(mk("Wed, 21 Oct 2015 07:28:00 GMT")); got != 0 {
		t.Errorf("past HTTP-date should clamp to 0, got %v", got)
	}
}

func TestWithRetry_NormalizesZeroFields(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `{"id":"acct-1","email":"a@example.com","created_at":"2026-01-01T00:00:00Z"}`)
	}))
	defer server.Close()

	client, err := NewClient(server.URL, "bxk_live_test", WithRetry(RetryConfig{MaxRetries: 3}))
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if client.retry.BackoffBase != defaultRetryBackoffBase {
		t.Errorf("BackoffBase not normalized: %v", client.retry.BackoffBase)
	}
	if client.retry.BackoffMax != defaultRetryBackoffMax {
		t.Errorf("BackoffMax not normalized: %v", client.retry.BackoffMax)
	}
	if !client.retry.Statuses[503] {
		t.Error("Statuses not normalized to defaults")
	}
	if !client.retry.Methods["GET"] {
		t.Error("Methods not normalized to defaults")
	}
}
