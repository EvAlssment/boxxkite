package boxxkite

import "fmt"

// APIError is returned when the control-plane responds with a non-2xx
// status and an `{"error": {code, message}}` envelope (or, failing that, a
// synthesized code/message from the bare HTTP status). Mirrors
// sdk-python's BoxxkiteApiError / sdk-js's BoxxkiteApiError.
type APIError struct {
	StatusCode int
	Code       string
	Message    string
	Retryable  bool
	Remediation string
	Details    any
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s [%s] (HTTP %d)", e.Message, e.Code, e.StatusCode)
}

// Named errors allow callers to use errors.As for the most actionable
// sandbox failures while APIError remains the common compatibility shape.
type QuotaExceededError struct{ APIError }
type EgressDeniedError struct{ APIError }
type SandboxNotReadyError struct{ APIError }
type CapabilityDeniedError struct{ APIError }
type ReadonlyFilesystemError struct{ APIError }
type SandboxCrashedError struct{ APIError }
type ServiceUnavailableError struct{ APIError }

func (e *QuotaExceededError) Error() string        { return e.APIError.Error() }
func (e *EgressDeniedError) Error() string         { return e.APIError.Error() }
func (e *SandboxNotReadyError) Error() string      { return e.APIError.Error() }
func (e *CapabilityDeniedError) Error() string     { return e.APIError.Error() }
func (e *ReadonlyFilesystemError) Error() string   { return e.APIError.Error() }
func (e *SandboxCrashedError) Error() string       { return e.APIError.Error() }
func (e *ServiceUnavailableError) Error() string   { return e.APIError.Error() }

// ConnectionError wraps a failure to reach the control-plane at all (DNS,
// TLS, timeout, connection refused) -- as opposed to a reachable server
// returning an error response (see APIError). Mirrors sdk-python's
// BoxxkiteConnectionError / sdk-js's BoxxkiteConnectionError.
type ConnectionError struct {
	Message string
	Err     error
}

func (e *ConnectionError) Error() string {
	return e.Message
}

func (e *ConnectionError) Unwrap() error {
	return e.Err
}
