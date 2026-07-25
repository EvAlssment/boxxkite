package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateSecret_SendsNameValueAndAllowedHosts(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/secrets" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["name"] != "stripe-key" {
			t.Errorf("unexpected name: %v", body["name"])
		}
		if body["value"] != "sk_test_abc123" {
			t.Errorf("unexpected value: %v", body["value"])
		}
		allowedHosts, _ := body["allowed_hosts"].([]any)
		if len(allowedHosts) != 1 || allowedHosts[0] != "api.stripe.com" {
			t.Errorf("unexpected allowed_hosts: %v", body["allowed_hosts"])
		}
		if _, ok := body["trust_tier"]; ok {
			t.Errorf("trust_tier should be omitted when not given, got: %v", body["trust_tier"])
		}
		writeJSON(t, w, 201, `{
			"id": "secret-1", "name": "stripe-key", "allowed_hosts": ["api.stripe.com"],
			"trust_tier": null, "created_at": "2026-01-01T00:00:00Z", "last_used_at": null
		}`)
	})
	defer closeServer()

	secret, err := client.CreateSecret(context.Background(), CreateSecretRequest{
		Name:         "stripe-key",
		Value:        "sk_test_abc123",
		AllowedHosts: []string{"api.stripe.com"},
	})
	if err != nil {
		t.Fatalf("CreateSecret: %v", err)
	}
	if secret.ID != "secret-1" {
		t.Errorf("unexpected id: %+v", secret)
	}
}

func TestCreateSecret_SendsTrustTierWhenGiven(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["trust_tier"] != "testnet" {
			t.Errorf("unexpected trust_tier: %v", body["trust_tier"])
		}
		writeJSON(t, w, 201, `{
			"id": "secret-2", "name": "wallet-key", "allowed_hosts": ["rpc.example.com"],
			"trust_tier": "testnet", "created_at": "2026-01-01T00:00:00Z", "last_used_at": null
		}`)
	})
	defer closeServer()

	secret, err := client.CreateSecret(context.Background(), CreateSecretRequest{
		Name:         "wallet-key",
		Value:        "0xabc",
		AllowedHosts: []string{"rpc.example.com"},
		TrustTier:    Ptr("testnet"),
	})
	if err != nil {
		t.Fatalf("CreateSecret: %v", err)
	}
	if secret.TrustTier == nil || *secret.TrustTier != "testnet" {
		t.Errorf("expected trust_tier to round-trip, got: %+v", secret)
	}
}

func TestListSecrets_ValueNeverPopulated(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[{
			"id": "secret-1", "name": "stripe-key", "allowed_hosts": ["api.stripe.com"],
			"trust_tier": null, "created_at": "2026-01-01T00:00:00Z", "last_used_at": null
		}]`)
	})
	defer closeServer()

	secrets, err := client.ListSecrets(context.Background())
	if err != nil {
		t.Fatalf("ListSecrets: %v", err)
	}
	if len(secrets) != 1 || secrets[0].Name != "stripe-key" {
		t.Errorf("unexpected secrets: %+v", secrets)
	}
}

func TestListSecrets_ReturnsEmptySliceWhenNone(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[]`)
	})
	defer closeServer()

	secrets, err := client.ListSecrets(context.Background())
	if err != nil {
		t.Fatalf("ListSecrets: %v", err)
	}
	if secrets == nil || len(secrets) != 0 {
		t.Errorf("expected empty (non-nil) slice, got: %+v", secrets)
	}
}

func TestDeleteSecret(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/secrets/secret-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.DeleteSecret(context.Background(), "secret-1"); err != nil {
		t.Fatalf("DeleteSecret: %v", err)
	}
}
