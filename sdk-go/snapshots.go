package boxxkite

import (
	"context"
	"fmt"
	"net/url"
	"time"
)

const SnapshotOperationTimeout = 10 * time.Minute

type SnapshotStatus string

const (
	SnapshotStatusPending   SnapshotStatus = "pending"
	SnapshotStatusCompleted SnapshotStatus = "completed"
	SnapshotStatusFailed    SnapshotStatus = "failed"
)

type CreateSnapshotRequest struct {
	Label *string `json:"label,omitempty"`
}

type RestoreSnapshotRequest struct {
	Label *string `json:"label,omitempty"`
}

type Snapshot struct {
	ID               string         `json:"id"`
	SessionID        *string        `json:"session_id"`
	Label            *string        `json:"label"`
	Status           SnapshotStatus `json:"status"`
	StorageKeyPrefix string         `json:"storage_key_prefix"`
	SizeBytes        int64          `json:"size_bytes"`
	CreatedAt        string         `json:"created_at"`
	DeletedAt        *string        `json:"deleted_at"`
}

func (c *Client) CreateSnapshot(ctx context.Context, sessionID string, req CreateSnapshotRequest) (*Snapshot, error) {
	var out Snapshot
	path := fmt.Sprintf("/v1/sandboxes/%s/snapshots", url.PathEscape(sessionID))
	opts := &requestOptions{timeout: SnapshotOperationTimeout}
	if err := c.doJSON(ctx, "POST", path, req, &out, opts); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) ListSnapshots(ctx context.Context, sessionID string) ([]Snapshot, error) {
	var out []Snapshot
	path := fmt.Sprintf("/v1/sandboxes/%s/snapshots", url.PathEscape(sessionID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Snapshot{}
	}
	return out, nil
}

func (c *Client) GetSnapshot(ctx context.Context, snapshotID string) (*Snapshot, error) {
	var out Snapshot
	path := fmt.Sprintf("/v1/snapshots/%s", url.PathEscape(snapshotID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) RestoreSnapshot(ctx context.Context, snapshotID string, req RestoreSnapshotRequest) (*Sandbox, error) {
	var out Sandbox
	path := fmt.Sprintf("/v1/snapshots/%s/restore", url.PathEscape(snapshotID))
	opts := &requestOptions{timeout: SnapshotOperationTimeout}
	if err := c.doJSON(ctx, "POST", path, req, &out, opts); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) DeleteSnapshot(ctx context.Context, snapshotID string) error {
	path := fmt.Sprintf("/v1/snapshots/%s", url.PathEscape(snapshotID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}
