package boxkite

import (
	"context"
	"fmt"
	"net/url"
	"time"
)

// ExecOptions carries the optional parameters for Exec.
type ExecOptions struct {
	// Timeout bounds the command's runtime in seconds (1-100, server
	// default 30). A command that outlives it is killed and reported as
	// failed.
	Timeout *int
	// Description is an optional caller-supplied label for this exec call,
	// surfaced in the audit log (GetLog/Watch).
	Description *string
}

// ExecResult is the response shape from Exec
// (POST /v1/sandboxes/{id}/exec).
type ExecResult struct {
	ExitCode int    `json:"exit_code"`
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
}

// Exec runs a shell command inside the session's sandbox and returns its
// exit code, stdout, and stderr (POST /v1/sandboxes/{id}/exec). Commands
// run synchronously; there is no streaming of partial output.
func (c *Client) Exec(ctx context.Context, sessionID, command string, opts *ExecOptions) (*ExecResult, error) {
	body := map[string]any{"command": command}
	reqOpts := &requestOptions{}
	if opts != nil {
		if opts.Timeout != nil {
			body["timeout"] = *opts.Timeout
			reqOpts.timeout = time.Duration(*opts.Timeout)*time.Second + ExecTimeoutHeadroom
		}
		if opts.Description != nil {
			body["description"] = *opts.Description
		}
	}
	var out ExecResult
	path := fmt.Sprintf("/v1/sandboxes/%s/exec", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", path, body, &out, reqOpts); err != nil {
		return nil, err
	}
	return &out, nil
}

// HTTPRequestOptions carries the optional parameters for HTTPRequest.
type HTTPRequestOptions struct {
	Headers map[string]string
	Body    *string
	Timeout *int
}

// HTTPRequestResult is the response shape from HTTPRequest
// (POST /v1/sandboxes/{id}/http-request).
type HTTPRequestResult struct {
	StatusCode int               `json:"status_code"`
	Headers    map[string]string `json:"headers"`
	Body       string            `json:"body"`
	Truncated  bool              `json:"truncated"`
}

// HTTPRequest issues the secrets-broker HTTP request
// (POST /v1/sandboxes/{id}/http-request, docs/SECRETS-DESIGN.md).
// opts.Headers/opts.Body may contain a literal `{{secret:name}}` reference
// for a secret granted to this session via CreateSandboxRequest.SecretNames;
// the sidecar substitutes the real value in-process -- this SDK never sees
// it.
func (c *Client) HTTPRequest(ctx context.Context, sessionID, method, targetURL string, opts *HTTPRequestOptions) (*HTTPRequestResult, error) {
	body := map[string]any{"method": method, "url": targetURL}
	reqOpts := &requestOptions{}
	if opts != nil {
		if opts.Headers != nil {
			body["headers"] = opts.Headers
		}
		if opts.Body != nil {
			body["body"] = *opts.Body
		}
		if opts.Timeout != nil {
			body["timeout"] = *opts.Timeout
			reqOpts.timeout = time.Duration(*opts.Timeout)*time.Second + ExecTimeoutHeadroom
		}
	}
	var out HTTPRequestResult
	path := fmt.Sprintf("/v1/sandboxes/%s/http-request", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", path, body, &out, reqOpts); err != nil {
		return nil, err
	}
	return &out, nil
}
