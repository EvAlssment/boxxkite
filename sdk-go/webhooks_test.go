package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateWebhook_SendsURLEventTypesAndDescription(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/webhooks" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["url"] != "https://example.com/hooks/boxkite" {
			t.Errorf("unexpected url: %v", body["url"])
		}
		eventTypes, _ := body["event_types"].([]any)
		if len(eventTypes) != 2 || eventTypes[0] != "sandbox.created" {
			t.Errorf("unexpected event_types: %v", body["event_types"])
		}
		if body["description"] != "Slack notifier" {
			t.Errorf("unexpected description: %v", body["description"])
		}
		writeJSON(t, w, 201, `{
			"id": "wh-1", "url": "https://example.com/hooks/boxkite",
			"event_types": ["sandbox.created", "sandbox.destroyed"], "description": "Slack notifier",
			"is_active": true, "created_at": "2026-01-01T00:00:00Z", "last_triggered_at": null,
			"secret": "whsec_abc123"
		}`)
	})
	defer closeServer()

	webhook, err := client.CreateWebhook(context.Background(), CreateWebhookRequest{
		URL:         "https://example.com/hooks/boxkite",
		EventTypes:  []string{"sandbox.created", "sandbox.destroyed"},
		Description: Ptr("Slack notifier"),
	})
	if err != nil {
		t.Fatalf("CreateWebhook: %v", err)
	}
	if webhook.Secret != "whsec_abc123" {
		t.Errorf("expected the raw secret to be returned, got: %+v", webhook)
	}
}

func TestCreateWebhook_AcceptsAuditLogEntryEventType(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/webhooks" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		eventTypes, _ := body["event_types"].([]any)
		if len(eventTypes) != 1 || eventTypes[0] != "audit_log.entry" {
			t.Errorf("unexpected event_types: %v", body["event_types"])
		}
		writeJSON(t, w, 201, `{
			"id": "wh-2", "url": "https://example.com/hooks/boxkite",
			"event_types": ["audit_log.entry"], "description": null,
			"is_active": true, "created_at": "2026-01-01T00:00:00Z", "last_triggered_at": null,
			"secret": "whsec_def456"
		}`)
	})
	defer closeServer()

	webhook, err := client.CreateWebhook(context.Background(), CreateWebhookRequest{
		URL:        "https://example.com/hooks/boxkite",
		EventTypes: []string{"audit_log.entry"},
	})
	if err != nil {
		t.Fatalf("CreateWebhook: %v", err)
	}
	if len(webhook.EventTypes) != 1 || webhook.EventTypes[0] != "audit_log.entry" {
		t.Errorf("expected event_types to round-trip audit_log.entry, got: %+v", webhook.EventTypes)
	}
}

func TestListWebhooks_SecretNeverPopulated(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[{
			"id": "wh-1", "url": "https://example.com/hooks", "event_types": ["sandbox.created"],
			"description": null, "is_active": true, "created_at": "2026-01-01T00:00:00Z", "last_triggered_at": null
		}]`)
	})
	defer closeServer()

	webhooks, err := client.ListWebhooks(context.Background())
	if err != nil {
		t.Fatalf("ListWebhooks: %v", err)
	}
	if len(webhooks) != 1 || webhooks[0].Secret != "" {
		t.Errorf("expected no secret on ListWebhooks, got: %+v", webhooks)
	}
}

func TestDeleteWebhook(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/webhooks/wh-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.DeleteWebhook(context.Background(), "wh-1"); err != nil {
		t.Fatalf("DeleteWebhook: %v", err)
	}
}

func TestListWebhookDeliveries_SendsLimitAndOffset(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/webhooks/wh-1/deliveries" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		q := r.URL.Query()
		if q.Get("limit") != "20" || q.Get("offset") != "0" {
			t.Errorf("unexpected query: %v", q)
		}
		writeJSON(t, w, 200, `[{
			"id": "del-1", "event_type": "sandbox.created", "status": "delivered", "attempt_count": 1,
			"next_attempt_at": "2026-01-01T00:00:05Z", "last_attempt_at": "2026-01-01T00:00:05Z",
			"response_status_code": 200, "failure_reason": null,
			"created_at": "2026-01-01T00:00:00Z", "delivered_at": "2026-01-01T00:00:05Z"
		}]`)
	})
	defer closeServer()

	deliveries, err := client.ListWebhookDeliveries(context.Background(), "wh-1", &ListWebhookDeliveriesOptions{Limit: Ptr(20), Offset: Ptr(0)})
	if err != nil {
		t.Fatalf("ListWebhookDeliveries: %v", err)
	}
	if len(deliveries) != 1 || deliveries[0].Status != "delivered" {
		t.Errorf("unexpected deliveries: %+v", deliveries)
	}
}
