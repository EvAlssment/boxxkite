package boxkite

import (
	"context"

	"github.com/gorilla/websocket"
)

// Session is a thin, session-scoped view over Client -- every method
// forwards to the identically-named Client method with this Session's ID
// as the sessionID argument. Obtained via WithSandbox; mirrors
// sdk-python's SandboxSession / sdk-js's SandboxSession.
type Session struct {
	client *Client
	ID     string
}

// WithSandbox is the create-on-enter, destroy-on-exit convenience --
// mirroring sdk-python's `with client.sandbox() as sb:` context manager /
// sdk-js's withSandbox. It creates a sandbox from req, invokes fn with a
// *Session bound to it, and destroys the sandbox when fn returns (even if
// fn returns an error) -- teardown itself is best-effort, mirroring both
// sibling SDKs' "an already-gone session shouldn't raise on cleanup"
// behavior.
func (c *Client) WithSandbox(ctx context.Context, req CreateSandboxRequest, fn func(*Session) error) error {
	sandbox, err := c.CreateSandbox(ctx, req)
	if err != nil {
		return err
	}
	sess := &Session{client: c, ID: sandbox.ID}
	defer func() {
		_ = c.DestroySandbox(ctx, sandbox.ID)
	}()
	return fn(sess)
}

func (s *Session) Exec(ctx context.Context, command string, opts *ExecOptions) (*ExecResult, error) {
	return s.client.Exec(ctx, s.ID, command, opts)
}

func (s *Session) HTTPRequest(ctx context.Context, method, targetURL string, opts *HTTPRequestOptions) (*HTTPRequestResult, error) {
	return s.client.HTTPRequest(ctx, s.ID, method, targetURL, opts)
}

func (s *Session) FileCreate(ctx context.Context, path, content string, opts *FileOptions) (*FileCreateResult, error) {
	return s.client.FileCreate(ctx, s.ID, path, content, opts)
}

func (s *Session) View(ctx context.Context, path string, opts *ViewOptions) (*FileViewResult, error) {
	return s.client.View(ctx, s.ID, path, opts)
}

func (s *Session) StrReplace(ctx context.Context, path, oldStr, newStr string, opts *StrReplaceOptions) (*StrReplaceResult, error) {
	return s.client.StrReplace(ctx, s.ID, path, oldStr, newStr, opts)
}

func (s *Session) Ls(ctx context.Context, path string) (*LsResult, error) {
	return s.client.Ls(ctx, s.ID, path)
}

func (s *Session) Glob(ctx context.Context, pattern, path string) (*GlobResult, error) {
	return s.client.Glob(ctx, s.ID, pattern, path)
}

func (s *Session) Grep(ctx context.Context, pattern string, opts *GrepOptions) (*GrepResult, error) {
	return s.client.Grep(ctx, s.ID, pattern, opts)
}

func (s *Session) GetLog(ctx context.Context, opts *GetLogOptions) (*GetLogResult, error) {
	return s.client.GetLog(ctx, s.ID, opts)
}

func (s *Session) Watch(ctx context.Context) (*LogWatcher, error) {
	return s.client.Watch(ctx, s.ID)
}

func (s *Session) StartProcess(ctx context.Context, command string, opts *StartProcessOptions) (*ProcessStartResult, error) {
	return s.client.StartProcess(ctx, s.ID, command, opts)
}

func (s *Session) ListProcesses(ctx context.Context) (*ProcessListResult, error) {
	return s.client.ListProcesses(ctx, s.ID)
}

func (s *Session) GetProcessOutput(ctx context.Context, processID string, sinceOffset int64) (*ProcessOutputResult, error) {
	return s.client.GetProcessOutput(ctx, s.ID, processID, sinceOffset)
}

func (s *Session) SendProcessInput(ctx context.Context, processID, data string) (*ProcessInputResult, error) {
	return s.client.SendProcessInput(ctx, s.ID, processID, data)
}

func (s *Session) StopProcess(ctx context.Context, processID string) (*ProcessStopResult, error) {
	return s.client.StopProcess(ctx, s.ID, processID)
}

func (s *Session) Takeover(ctx context.Context) (*websocket.Conn, error) {
	return s.client.Takeover(ctx, s.ID)
}

func (s *Session) DesktopTakeover(ctx context.Context) (*websocket.Conn, error) {
	return s.client.DesktopTakeover(ctx, s.ID)
}

func (s *Session) CreatePreviewURL(ctx context.Context, port int, ttlSeconds *int) (*PreviewURL, error) {
	return s.client.CreatePreviewURL(ctx, s.ID, port, ttlSeconds)
}

func (s *Session) RevokePreviewURL(ctx context.Context, port int, tokenID string) (*PreviewRevokeResult, error) {
	return s.client.RevokePreviewURL(ctx, s.ID, port, tokenID)
}
