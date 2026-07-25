package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// CreateVolumeRequest is the request body for CreateVolume
// (POST /v1/volumes).
type CreateVolumeRequest struct {
	// Label is an optional human-readable label for the volume.
	Label *string `json:"label,omitempty"`
	// SizeGB is the requested volume size in GB (max 1024).
	SizeGB float64 `json:"size_gb"`
}

// Volume is an independent, PVC-backed storage volume, as returned by
// CreateVolume/GetVolume/ListVolumes.
type Volume struct {
	ID            string  `json:"id"`
	Label         *string `json:"label"`
	SizeGB        float64 `json:"size_gb"`
	Status        string  `json:"status"`
	PVCName       *string `json:"pvc_name"`
	FailureReason *string `json:"failure_reason"`
	CreatedAt     string  `json:"created_at"`
}

// CreateVolume creates an independent, PVC-backed storage volume
// (POST /v1/volumes) that can later be mounted into one or more sandboxes
// via CreateSandboxRequest.VolumeMounts. Always asynchronous -- returns
// immediately with Status "queued"; poll GetVolume for progress. 404s
// ("feature_disabled") if the deployment hasn't enabled volumes
// (BOXKITE_VOLUMES_ENABLED).
func (c *Client) CreateVolume(ctx context.Context, req CreateVolumeRequest) (*Volume, error) {
	var out Volume
	if err := c.doJSON(ctx, "POST", "/v1/volumes", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetVolume fetches one storage volume's status and details
// (GET /v1/volumes/{id}).
func (c *Client) GetVolume(ctx context.Context, volumeID string) (*Volume, error) {
	var out Volume
	path := fmt.Sprintf("/v1/volumes/%s", url.PathEscape(volumeID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListVolumes lists every storage volume created for this account
// (GET /v1/volumes).
func (c *Client) ListVolumes(ctx context.Context) ([]Volume, error) {
	var out []Volume
	if err := c.doJSON(ctx, "GET", "/v1/volumes", nil, &out, nil); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Volume{}
	}
	return out, nil
}

// DeleteVolume deletes a storage volume (DELETE /v1/volumes/{id}).
func (c *Client) DeleteVolume(ctx context.Context, volumeID string) error {
	path := fmt.Sprintf("/v1/volumes/%s", url.PathEscape(volumeID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}
