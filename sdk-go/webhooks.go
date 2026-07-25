package boxkite

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
)

// CreateWebhookRequest is the request body for CreateWebhook
// (POST /v1/webhooks).
type CreateWebhookRequest struct {
	// URL is the HTTPS (or HTTP, for local testing) URL the control plane
	// will POST events to. Checked at registration time against the same
	// private/link-local/loopback/metadata-address denylist
	// CreateSandboxRequest's secrets use for allowed_hosts.
	URL string `json:"url"`
	// EventTypes are the event types this subscription should receive (at
	// least one required) -- "sandbox.created", "sandbox.destroyed", or
	// "audit_log.entry" (added per GitHub issue #125 for SIEM/audit-log
	// export). See docs/WEBHOOKS-DESIGN.md for the full event catalog.
	EventTypes []string `json:"event_types"`
	// Description is an optional caller-supplied label for this
	// subscription (e.g. "Slack notifier").
	Description *string `json:"description,omitempty"`
}

// Webhook is a webhook subscription, as returned by CreateWebhook/
// ListWebhooks. Secret is only ever populated on CreateWebhook's response
// -- it is never returned by any other route.
type Webhook struct {
	ID              string   `json:"id"`
	URL             string   `json:"url"`
	EventTypes      []string `json:"event_types"`
	Description     *string  `json:"description"`
	IsActive        bool     `json:"is_active"`
	CreatedAt       string   `json:"created_at"`
	LastTriggeredAt *string  `json:"last_triggered_at"`
	// Secret is the raw signing secret, shown exactly once on the
	// CreateWebhook response. Use it to verify the
	// X-Boxkite-Webhook-Signature header on every delivery; it cannot be
	// retrieved again after this response.
	Secret string `json:"secret,omitempty"`
}

// CreateWebhook registers a webhook subscription (POST /v1/webhooks).
// Returns the subscription plus a Secret field -- the raw signing secret,
// shown exactly once.
func (c *Client) CreateWebhook(ctx context.Context, req CreateWebhookRequest) (*Webhook, error) {
	var out Webhook
	if err := c.doJSON(ctx, "POST", "/v1/webhooks", req, &out, nil); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListWebhooks lists webhook subscriptions for this account
// (GET /v1/webhooks). The signing secret is never returned here.
func (c *Client) ListWebhooks(ctx context.Context) ([]Webhook, error) {
	var out []Webhook
	if err := c.doJSON(ctx, "GET", "/v1/webhooks", nil, &out, nil); err != nil {
		return nil, err
	}
	if out == nil {
		out = []Webhook{}
	}
	return out, nil
}

// DeleteWebhook deletes a webhook subscription owned by this account
// (DELETE /v1/webhooks/{id}). 404s if already gone or never owned by this
// account.
func (c *Client) DeleteWebhook(ctx context.Context, subscriptionID string) error {
	path := fmt.Sprintf("/v1/webhooks/%s", url.PathEscape(subscriptionID))
	return c.doJSON(ctx, "DELETE", path, nil, nil, nil)
}

// WebhookDelivery is one delivery attempt for a webhook subscription, as
// returned by ListWebhookDeliveries.
type WebhookDelivery struct {
	ID                 string  `json:"id"`
	EventType          string  `json:"event_type"`
	Status             string  `json:"status"`
	AttemptCount       int     `json:"attempt_count"`
	NextAttemptAt      string  `json:"next_attempt_at"`
	LastAttemptAt      *string `json:"last_attempt_at"`
	ResponseStatusCode *int    `json:"response_status_code"`
	FailureReason      *string `json:"failure_reason"`
	CreatedAt          string  `json:"created_at"`
	DeliveredAt        *string `json:"delivered_at"`
}

// ListWebhookDeliveriesOptions carries the optional pagination parameters
// for ListWebhookDeliveries.
type ListWebhookDeliveriesOptions struct {
	// Limit is the maximum number of entries to return (server default
	// 20, max 100).
	Limit *int
	// Offset is the number of entries to skip, newest-first.
	Offset *int
}

// ListWebhookDeliveries returns recent delivery attempts
// (pending/delivered/failed) for this subscription, newest first
// (GET /v1/webhooks/{id}/deliveries).
func (c *Client) ListWebhookDeliveries(ctx context.Context, subscriptionID string, opts *ListWebhookDeliveriesOptions) ([]WebhookDelivery, error) {
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
	var out []WebhookDelivery
	path := fmt.Sprintf("/v1/webhooks/%s/deliveries", url.PathEscape(subscriptionID))
	if err := c.doJSON(ctx, "GET", path, nil, &out, reqOpts); err != nil {
		return nil, err
	}
	if out == nil {
		out = []WebhookDelivery{}
	}
	return out, nil
}
