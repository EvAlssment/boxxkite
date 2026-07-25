package boxkite

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// StartProcessOptions carries the optional parameters for StartProcess.
type StartProcessOptions struct {
	Description *string
	// MaxRuntimeSeconds is a hard ceiling on how long the process may run
	// before being force-killed. Defaults to 3600.
	MaxRuntimeSeconds int
	// ExposePort, if set, makes this process's listening port reachable
	// via a preview URL (see CreatePreviewURL).
	ExposePort *int
}

// ProcessStartResult is the response shape from StartProcess.
type ProcessStartResult struct {
	ProcessID string `json:"process_id"`
	Status    string `json:"status"`
	StartedAt string `json:"started_at"`
}

// StartProcess starts a background process that keeps running after this
// call returns (POST /v1/sandboxes/{id}/processes). Distinct from Exec,
// which is one-shot request/response bounded by its own timeout: poll the
// returned ProcessID's output with GetProcessOutput, feed it input with
// SendProcessInput, and stop it with StopProcess.
func (c *Client) StartProcess(ctx context.Context, sessionID, command string, opts *StartProcessOptions) (*ProcessStartResult, error) {
	maxRuntimeSeconds := 3600
	body := map[string]any{"command": command}
	if opts != nil {
		if opts.MaxRuntimeSeconds != 0 {
			maxRuntimeSeconds = opts.MaxRuntimeSeconds
		}
		if opts.Description != nil {
			body["description"] = *opts.Description
		}
		if opts.ExposePort != nil {
			body["expose_port"] = *opts.ExposePort
		}
	}
	body["max_runtime_seconds"] = maxRuntimeSeconds
	var out ProcessStartResult
	path := fmt.Sprintf("/v1/sandboxes/%s/processes", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", path, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ProcessInfo is one tracked background process, as returned by
// ListProcesses.
type ProcessInfo struct {
	ProcessID   string  `json:"process_id"`
	Command     string  `json:"command"`
	Description *string `json:"description"`
	Status      string  `json:"status"`
	StartedAt   string  `json:"started_at"`
	ExitCode    *int    `json:"exit_code"`
}

// ProcessListResult is the response shape from ListProcesses.
type ProcessListResult struct {
	Processes []ProcessInfo `json:"processes"`
}

// ListProcesses returns every background process currently tracked for
// this session (GET /v1/sandboxes/{id}/processes).
func (c *Client) ListProcesses(ctx context.Context, sessionID string) (*ProcessListResult, error) {
	var out ProcessListResult
	path := fmt.Sprintf("/v1/sandboxes/%s/processes", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ProcessOutputResult is the response shape from GetProcessOutput.
type ProcessOutputResult struct {
	Status      string `json:"status"`
	StdoutChunk string `json:"stdout_chunk"`
	NextOffset  int64  `json:"next_offset"`
	Truncated   bool   `json:"truncated"`
	ExitCode    *int   `json:"exit_code"`
}

// GetProcessOutput polls a background process's output since a given byte
// offset (GET /v1/sandboxes/{id}/processes/{processId}/output).
// Polling-style, not streaming. sinceOffset (from a previous call's
// NextOffset, or 0 the first time) lets you fetch only the new output
// since your last check.
func (c *Client) GetProcessOutput(ctx context.Context, sessionID, processID string, sinceOffset int64) (*ProcessOutputResult, error) {
	q := newQuery()
	q.Set("since_offset", strconv.FormatInt(sinceOffset, 10))
	opts := &requestOptions{query: q}
	var out ProcessOutputResult
	path := fmt.Sprintf("/v1/sandboxes/%s/processes/%s/output", url.PathEscape(sessionID), url.PathEscape(processID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, opts); err != nil {
		return nil, err
	}
	return &out, nil
}

// ProcessStreamEvent is one event from StreamProcessOutput. Type is "output"
// (StdoutChunk/NextOffset/Truncated populated) for each new chunk, then "exit"
// (Status/ExitCode populated) once the process finishes.
type ProcessStreamEvent struct {
	Type        string `json:"type"`
	StdoutChunk string `json:"stdout_chunk"`
	NextOffset  int64  `json:"next_offset"`
	Truncated   bool   `json:"truncated"`
	Status      string `json:"status"`
	ExitCode    *int   `json:"exit_code"`
}

// ProcessStream streams a background process's stdout as Server-Sent Events
// (GET /v1/sandboxes/{id}/processes/{processId}/stream), the streaming
// counterpart to GetProcessOutput. Use it like bufio.Scanner:
//
//	stream, err := client.StreamProcessOutput(ctx, sessionID, processID, 0)
//	if err != nil { ... }
//	defer stream.Close()
//	for stream.Next() {
//		ev := stream.Event()
//		// ev.Type == "output" | "exit"
//	}
//	if err := stream.Err(); err != nil { ... }
type ProcessStream struct {
	resp    *http.Response
	scanner *bufio.Scanner
	current ProcessStreamEvent
	err     error
}

// StreamProcessOutput opens the SSE output stream for a background process.
// The caller must call Close on the returned *ProcessStream once done.
func (c *Client) StreamProcessOutput(ctx context.Context, sessionID, processID string, sinceOffset int64) (*ProcessStream, error) {
	path := fmt.Sprintf("/v1/sandboxes/%s/processes/%s/stream", url.PathEscape(sessionID), url.PathEscape(processID))
	reqURL := c.baseURL + path + "?since_offset=" + strconv.FormatInt(sinceOffset, 10)
	req, err := http.NewRequestWithContext(ctx, "GET", reqURL, nil)
	if err != nil {
		return nil, &ConnectionError{Message: err.Error(), Err: err}
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, &ConnectionError{Message: err.Error(), Err: err}
	}
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return nil, apiErrorFromResponse(resp.StatusCode, body)
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	return &ProcessStream{resp: resp, scanner: scanner}, nil
}

// Next advances to the next streamed event, blocking until one arrives. It
// returns false when the stream ends -- check Err to distinguish a clean end
// from an error.
func (s *ProcessStream) Next() bool {
	var dataLines []string
	for s.scanner.Scan() {
		line := s.scanner.Text()
		if line == "" {
			if len(dataLines) == 0 {
				continue
			}
			joined := strings.Join(dataLines, "\n")
			var ev ProcessStreamEvent
			if err := json.Unmarshal([]byte(joined), &ev); err != nil {
				s.err = fmt.Errorf("boxkite: decoding SSE event: %w", err)
				return false
			}
			s.current = ev
			return true
		}
		if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := s.scanner.Err(); err != nil {
		s.err = err
	}
	return false
}

// Event returns the most recent event produced by Next.
func (s *ProcessStream) Event() ProcessStreamEvent { return s.current }

// Err returns the first error encountered while streaming, if any.
func (s *ProcessStream) Err() error { return s.err }

// Close closes the underlying HTTP response body, ending the stream.
func (s *ProcessStream) Close() error { return s.resp.Body.Close() }

// ProcessInputResult is the response shape from SendProcessInput.
type ProcessInputResult struct {
	BytesWritten int `json:"bytes_written"`
}

// SendProcessInput writes to a tracked background process's stdin pipe
// (POST /v1/sandboxes/{id}/processes/{processId}/input).
func (c *Client) SendProcessInput(ctx context.Context, sessionID, processID, data string) (*ProcessInputResult, error) {
	body := map[string]any{"data": data}
	var out ProcessInputResult
	path := fmt.Sprintf("/v1/sandboxes/%s/processes/%s/input", url.PathEscape(sessionID), url.PathEscape(processID))
	if err := c.doJSON(ctx, "POST", path, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ProcessStopResult is the response shape from StopProcess.
type ProcessStopResult struct {
	Status   string `json:"status"`
	ExitCode *int   `json:"exit_code"`
}

// StopProcess stops a tracked background process (SIGTERM, then SIGKILL
// if it doesn't exit within a short grace period)
// (POST /v1/sandboxes/{id}/processes/{processId}/stop).
func (c *Client) StopProcess(ctx context.Context, sessionID, processID string) (*ProcessStopResult, error) {
	var out ProcessStopResult
	path := fmt.Sprintf("/v1/sandboxes/%s/processes/%s/stop", url.PathEscape(sessionID), url.PathEscape(processID))
	if err := c.doJSON(ctx, "POST", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}
