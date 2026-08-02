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
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s [%s] (HTTP %d)", e.Message, e.Code, e.StatusCode)
}

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
