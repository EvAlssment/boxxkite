package boxxkite

import (
	"errors"
	"strings"
	"testing"
)

func TestAPIError_ErrorStringIncludesCodeAndStatus(t *testing.T) {
	err := &APIError{StatusCode: 404, Code: "not_found", Message: "Session not found."}
	got := err.Error()
	if !strings.Contains(got, "not_found") || !strings.Contains(got, "404") || !strings.Contains(got, "Session not found.") {
		t.Errorf("unexpected error string: %q", got)
	}
}

func TestAPIErrorCompatibilityPathsExposeTaxonomy(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		want   string
		as     any
	}{
		{
			name:   "command not allowed",
			status: 403,
			body:   `{"error":{"code":"command_not_allowed","message":"Command not in allowlist."}}`,
			want:   "capability_denied",
			as:     new(*CapabilityDeniedError),
		},
		{
			name:   "generic server error",
			status: 502,
			body:   `{"error":{"code":"upstream_error","message":"Upstream unavailable."}}`,
			want:   "service_unavailable",
			as:     new(*ServiceUnavailableError),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := apiErrorFromResponse(tt.status, []byte(tt.body))
			apiErr, ok := err.(*APIError)
			if !ok {
				t.Fatalf("expected compatibility *APIError, got %T", err)
			}
			if apiErr.Taxonomy != tt.want {
				t.Fatalf("taxonomy = %q, want %q", apiErr.Taxonomy, tt.want)
			}
			if !errors.As(err, tt.as) {
				t.Fatalf("errors.As(%T) failed for %T", tt.as, err)
			}
		})
	}
}

func TestConnectionError_ErrorAndUnwrap(t *testing.T) {
	inner := errors.New("dial tcp: connection refused")
	err := &ConnectionError{Message: "boom", Err: inner}
	if err.Error() != "boom" {
		t.Errorf("unexpected Error(): %q", err.Error())
	}
	if !errors.Is(err, inner) {
		t.Error("expected errors.Is to unwrap to the inner error")
	}
}
