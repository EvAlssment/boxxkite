package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// FileOptions carries the optional description field shared by every file
// operation below.
type FileOptions struct {
	Description *string
}

// FileCreateResult is the response shape from FileCreate.
type FileCreateResult struct {
	Path    string `json:"path"`
	Size    int    `json:"size"`
	Created bool   `json:"created"`
}

// FileCreate creates or overwrites a file in the session's sandbox
// workspace (POST /v1/sandboxes/{id}/files).
func (c *Client) FileCreate(ctx context.Context, sessionID, path, content string, opts *FileOptions) (*FileCreateResult, error) {
	body := map[string]any{"path": path, "content": content}
	applyDescription(body, opts)
	var out FileCreateResult
	reqPath := fmt.Sprintf("/v1/sandboxes/%s/files", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", reqPath, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ViewOptions carries the optional parameters for View.
type ViewOptions struct {
	// ViewRange is an optional [start_line, end_line] pair, 1-indexed.
	ViewRange   []int
	Description *string
}

// FileViewResult is the response shape from View. Entries is populated
// (Content/Lines left zero) when the target path is a directory.
type FileViewResult struct {
	Content     string     `json:"content"`
	Lines       int        `json:"lines"`
	IsDirectory bool       `json:"is_directory"`
	Entries     []DirEntry `json:"entries"`
}

// DirEntry is one entry returned by View (for a directory path), Ls, or
// Glob.
type DirEntry struct {
	Path  string `json:"path"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
}

// View reads a text file's contents (optionally a line range), or lists a
// directory's entries (POST /v1/sandboxes/{id}/files/view). Binary/image
// files are not supported.
func (c *Client) View(ctx context.Context, sessionID, path string, opts *ViewOptions) (*FileViewResult, error) {
	body := map[string]any{"path": path}
	if opts != nil {
		if opts.ViewRange != nil {
			body["view_range"] = opts.ViewRange
		}
		if opts.Description != nil {
			body["description"] = *opts.Description
		}
	}
	var out FileViewResult
	reqPath := fmt.Sprintf("/v1/sandboxes/%s/files/view", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", reqPath, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// StrReplaceOptions carries the optional parameters for StrReplace.
type StrReplaceOptions struct {
	ReplaceAll  bool
	Description *string
}

// StrReplaceResult is the response shape from StrReplace.
type StrReplaceResult struct {
	Path        string `json:"path"`
	Replaced    bool   `json:"replaced"`
	Occurrences int    `json:"occurrences"`
}

// StrReplace replaces oldStr with newStr in a file; oldStr must appear
// exactly once unless opts.ReplaceAll is set
// (POST /v1/sandboxes/{id}/files/str-replace).
func (c *Client) StrReplace(ctx context.Context, sessionID, path, oldStr, newStr string, opts *StrReplaceOptions) (*StrReplaceResult, error) {
	body := map[string]any{
		"path":        path,
		"old_str":     oldStr,
		"new_str":     newStr,
		"replace_all": false,
	}
	if opts != nil {
		body["replace_all"] = opts.ReplaceAll
		if opts.Description != nil {
			body["description"] = *opts.Description
		}
	}
	var out StrReplaceResult
	reqPath := fmt.Sprintf("/v1/sandboxes/%s/files/str-replace", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", reqPath, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// LsResult is the response shape from Ls.
type LsResult struct {
	Entries []DirEntry `json:"entries"`
}

// Ls lists the direct children of a directory in the session's sandbox
// workspace (POST /v1/sandboxes/{id}/files/ls). path defaults to "/".
func (c *Client) Ls(ctx context.Context, sessionID string, path string) (*LsResult, error) {
	if path == "" {
		path = "/"
	}
	body := map[string]any{"path": path}
	var out LsResult
	reqPath := fmt.Sprintf("/v1/sandboxes/%s/files/ls", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", reqPath, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// GlobResult is the response shape from Glob.
type GlobResult struct {
	Matches []DirEntry `json:"matches"`
}

// Glob finds files matching a glob pattern (e.g. "**/*.py") under a
// directory in the session's sandbox workspace
// (POST /v1/sandboxes/{id}/files/glob). path defaults to "/".
func (c *Client) Glob(ctx context.Context, sessionID, pattern string, path string) (*GlobResult, error) {
	if path == "" {
		path = "/"
	}
	body := map[string]any{"pattern": pattern, "path": path}
	var out GlobResult
	reqPath := fmt.Sprintf("/v1/sandboxes/%s/files/glob", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", reqPath, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// GrepOptions carries the optional parameters for Grep.
type GrepOptions struct {
	// Path defaults to "/".
	Path string
	// Glob optionally restricts matches to files whose name matches this
	// glob pattern.
	Glob string
	// MaxMatches defaults to 500 and is capped at 5000 server-side.
	MaxMatches int
}

// GrepMatch is one match returned by Grep.
type GrepMatch struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Text string `json:"text"`
}

// GrepResult is the response shape from Grep.
type GrepResult struct {
	Matches   []GrepMatch `json:"matches"`
	Error     *string     `json:"error"`
	Truncated bool        `json:"truncated"`
}

// Grep searches file contents by regex pattern under a directory in the
// session's sandbox workspace, optionally restricted to files matching
// opts.Glob (POST /v1/sandboxes/{id}/files/grep).
func (c *Client) Grep(ctx context.Context, sessionID, pattern string, opts *GrepOptions) (*GrepResult, error) {
	path := "/"
	maxMatches := 500
	body := map[string]any{"pattern": pattern}
	if opts != nil {
		if opts.Path != "" {
			path = opts.Path
		}
		if opts.Glob != "" {
			body["glob"] = opts.Glob
		}
		if opts.MaxMatches != 0 {
			maxMatches = opts.MaxMatches
		}
	}
	body["path"] = path
	body["max_matches"] = maxMatches
	var out GrepResult
	reqPath := fmt.Sprintf("/v1/sandboxes/%s/files/grep", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "POST", reqPath, body, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

func applyDescription(body map[string]any, opts *FileOptions) {
	if opts != nil && opts.Description != nil {
		body["description"] = *opts.Description
	}
}
