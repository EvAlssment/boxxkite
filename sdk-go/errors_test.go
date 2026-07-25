package boxkite

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
