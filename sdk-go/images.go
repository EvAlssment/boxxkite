package boxkite

import (
	"context"
	"fmt"
	"net/url"
)

// CreateImageRequest is the request body for CreateImage
// (POST /v1/images). All fields are optional -- a zero-value
// CreateImageRequest{} builds an unmodified "boxkite-default" image.
type CreateImageRequest struct {
	// Label is an optional human-readable label for the image.
	Label *string `json:"label,omitempty"`
	// Base is one of "boxkite-default", "boxkite-minimal", "boxkite-node",
	// "boxkite-go". Defaults to "boxkite-default" server-side when empty.
	// "boxkite-node" drops Python entirely (no PythonPackages installable,
	// only AptPackages/NpmPackages); "boxkite-go" drops both Python and
	// Node entirely (no PythonPackages or NpmPackages installable, only
	// AptPackages).
	Base string `json:"base,omitempty"`
	// PythonPackages are exact-version-pinned packages ("name==version",
	// no ranges) to install into the image.
	PythonPackages []string `json:"python_packages,omitempty"`
	// AptPackages are exact-version-pinned apt packages ("name==version",
	// no ranges) to install into the image.
	AptPackages []string `json:"apt_packages,omitempty"`
	// NpmPackages are exact-version-pinned npm packages ("name==version"
	// or "@scope/name==version", no ranges) to install into the image. Not
	// supported on Base="boxkite-go".
	NpmPackages []string `json:"npm_packages,omitempty"`
}

// Image is a custom sandbox image, as returned by CreateImage/GetImage/
// ListImages.
type Image struct {
	ID             string         `json:"id"`
	Label          *string        `json:"label"`
	Base           string         `json:"base"`
	PythonPackages []string       `json:"python_packages"`
	AptPackages    []string       `json:"apt_packages"`
	NpmPackages    []string       `json:"npm_packages"`
	Status         string         `json:"status"`
	Digest         *string        `json:"digest"`
	RegistryRef    *string        `json:"registry_ref"`
	ScanResult     map[string]any `json:"scan_result"`
	FailureReason  *string        `json:"failure_reason"`
	CreatedAt      string         `json:"created_at"`
	CompletedAt    *string        `json:"completed_at"`
}

// CreateImage queues a build of a custom sandbox image
// (POST /v1/images). Always asynchronous -- returns immediately with
// Status "queued"; poll GetImage for progress. 404s ("feature_disabled")
// if the deployment hasn't enabled the declarative builder
// (BOXKITE_IMAGE_BUILDER_ENABLED).
func (c *Client) CreateImage(ctx context.Context, req CreateImageRequest) (*Image, error) {
	var out Image
	if err := c.doJSON(ctx, "POST", "/v1/images", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetImage fetches one custom sandbox image's build status and details
// (GET /v1/images/{id}).
func (c *Client) GetImage(ctx context.Context, imageID string) (*Image, error) {
	var out Image
	path := fmt.Sprintf("/v1/images/%s", url.PathEscape(imageID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListImages lists every custom sandbox image built for this account
// (GET /v1/images).
func (c *Client) ListImages(ctx context.Context) ([]Image, error) {
	var out []Image
	if err := c.doJSON(ctx, "GET", "/v1/images", nil, &out, nil); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Image{}
	}
	return out, nil
}

// DeleteImage deletes a custom sandbox image (DELETE /v1/images/{id}).
func (c *Client) DeleteImage(ctx context.Context, imageID string) error {
	path := fmt.Sprintf("/v1/images/%s", url.PathEscape(imageID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}
