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

// LogEntry is one row of a session's exec/file-operation audit trail, as
// returned by GetLog and streamed by Watch
// (docs/SANDBOX-OBSERVABILITY-DESIGN.md).
type LogEntry struct {
	ID              string          `json:"id"`
	SessionID       string          `json:"session_id"`
	Source          string          `json:"source"`
	Operation       string          `json:"operation"`
	Detail          json.RawMessage `json:"detail"`
	ExitCode        *int            `json:"exit_code"`
	OutputTruncated string          `json:"output_truncated"`
	StartedAt       string          `json:"started_at"`
	DurationMs      *int64          `json:"duration_ms"`
	RowHash         *string         `json:"row_hash"`
	PrevHash        *string         `json:"prev_hash"`
}

// GetLogResult is the response shape from GetLog.
type GetLogResult struct {
	Entries []LogEntry `json:"entries"`
	Limit   int        `json:"limit"`
	Offset  int        `json:"offset"`
	Total   int        `json:"total"`
}

// GetLogOptions carries the optional pagination parameters for GetLog.
type GetLogOptions struct {
	Limit  *int
	Offset *int
}

// GetLog returns a page of audit-log entries for this session, oldest
// first (GET /v1/sandboxes/{id}/log).
func (c *Client) GetLog(ctx context.Context, sessionID string, opts *GetLogOptions) (*GetLogResult, error) {
	q := newQuery()
	if opts != nil {
		if opts.Limit != nil {
			q.Set("limit", strconv.Itoa(*opts.Limit))
		}
		if opts.Offset != nil {
			q.Set("offset", strconv.Itoa(*opts.Offset))
		}
	}
	reqOpts := &requestOptions{query: q}
	var out GetLogResult
	path := fmt.Sprintf("/v1/sandboxes/%s/log", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, reqOpts); err != nil {
		return nil, err
	}
	return &out, nil
}

// LogWatcher streams new audit-log entries for a session as they're
// written (GET /v1/sandboxes/{id}/watch), one LogEntry per Server-Sent
// Event `data:` line. Use it like bufio.Scanner:
//
//	watcher, err := client.Watch(ctx, sessionID)
//	if err != nil { ... }
//	defer watcher.Close()
//	for watcher.Next() {
//		entry := watcher.Entry()
//		// ...
//	}
//	if err := watcher.Err(); err != nil { ... }
//
// Next blocks until either a new entry arrives, the stream ends, or ctx is
// canceled. This is a live feed of exec/file operations as the
// control-plane logs them, not a live terminal -- see Takeover for that.
type LogWatcher struct {
	resp    *http.Response
	scanner *bufio.Scanner
	current LogEntry
	err     error
}

// Watch opens the audit-log SSE stream for a session
// (GET /v1/sandboxes/{id}/watch). The caller must call Close on the
// returned *LogWatcher once done.
func (c *Client) Watch(ctx context.Context, sessionID string) (*LogWatcher, error) {
	path := fmt.Sprintf("/v1/sandboxes/%s/watch", url.PathEscape(sessionID))
	req, err := http.NewRequestWithContext(ctx, "GET", c.baseURL+path, nil)
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
	// ExecLogEntry rows can carry a sizable output_truncated/detail
	// payload -- widen past bufio.Scanner's 64KB default token limit
	// rather than silently truncating a long SSE line.
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	return &LogWatcher{resp: resp, scanner: scanner}, nil
}

// Next advances to the next streamed LogEntry, blocking until one arrives.
// It returns false when the stream ends (server closed it, context
// canceled, or a decode error) -- check Err to distinguish a clean end
// from an error.
func (w *LogWatcher) Next() bool {
	var dataLines []string
	for w.scanner.Scan() {
		line := w.scanner.Text()
		if line == "" {
			if len(dataLines) == 0 {
				continue
			}
			joined := strings.Join(dataLines, "\n")
			var entry LogEntry
			if err := json.Unmarshal([]byte(joined), &entry); err != nil {
				w.err = fmt.Errorf("boxkite: decoding SSE event: %w", err)
				return false
			}
			w.current = entry
			return true
		}
		if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := w.scanner.Err(); err != nil {
		w.err = err
	}
	return false
}

// Entry returns the most recent entry produced by Next.
func (w *LogWatcher) Entry() LogEntry {
	return w.current
}

// Err returns the first error encountered while streaming, if any (nil if
// the stream simply ended).
func (w *LogWatcher) Err() error {
	return w.err
}

// Close closes the underlying HTTP response body, ending the stream.
func (w *LogWatcher) Close() error {
	return w.resp.Body.Close()
}
