//go:build ignore

// Runnable end-to-end example against a real hosted control-plane. The
// //go:build ignore tag keeps this out of `go build ./...` (it is a second
// package main alongside quickstart.go) while still allowing:
//
//	BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
//	    go run ./examples/webhooks.go
package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	boxkite "github.com/EvAlssment/boxkite/sdk-go"
)

// verifySignature verifies an X-Boxkite-Webhook-Signature header, per
// docs/WEBHOOKS-DESIGN.md §6.
func verifySignature(secret, signatureHeader string, rawBody []byte, toleranceSeconds int64) bool {
	parts := map[string]string{}
	for _, p := range strings.Split(signatureHeader, ",") {
		kv := strings.SplitN(p, "=", 2)
		if len(kv) == 2 {
			parts[kv[0]] = kv[1]
		}
	}
	timestamp, err := strconv.ParseInt(parts["t"], 10, 64)
	if err != nil {
		return false
	}
	if delta := time.Now().Unix() - timestamp; delta > toleranceSeconds || delta < -toleranceSeconds {
		return false
	}
	signature, err := hex.DecodeString(parts["v1"])
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(fmt.Sprintf("%d.", timestamp)))
	mac.Write(rawBody)
	return hmac.Equal(mac.Sum(nil), signature)
}

func main() {
	baseURL := os.Getenv("BOXKITE_BASE_URL")
	apiKey := os.Getenv("BOXKITE_API_KEY")
	if baseURL == "" || apiKey == "" {
		fmt.Fprintln(os.Stderr, "Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.")
		os.Exit(1)
	}

	client, err := boxkite.NewClient(baseURL, apiKey)
	if err != nil {
		log.Fatal(err)
	}

	ctx := context.Background()

	webhook, err := client.CreateWebhook(ctx, boxkite.CreateWebhookRequest{
		URL:         "https://example.com/boxkite-webhook",
		EventTypes:  []string{"sandbox.created", "sandbox.destroyed", "audit_log.entry"},
		Description: boxkite.Ptr("webhooks example"),
	})
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf("Created webhook %s\n", webhook.ID)
	fmt.Printf("Signing secret (shown once, save it now): %s\n", webhook.Secret)

	webhooks, err := client.ListWebhooks(ctx)
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf("Account has %d webhook(s)\n", len(webhooks))

	// Simulate a delivery to prove verifySignature works, without a real
	// receiver: sign a synthetic payload locally with the just-returned
	// secret, then verify it the same way a receiver would.
	rawBody, _ := json.Marshal(map[string]string{"event_type": "sandbox.created", "event_id": "evt_demo"})
	timestamp := time.Now().Unix()
	mac := hmac.New(sha256.New, []byte(webhook.Secret))
	mac.Write([]byte(fmt.Sprintf("%d.", timestamp)))
	mac.Write(rawBody)
	signatureHeader := fmt.Sprintf("t=%d,v1=%s", timestamp, hex.EncodeToString(mac.Sum(nil)))

	fmt.Printf("Locally signed payload verifies: %t\n", verifySignature(webhook.Secret, signatureHeader, rawBody, 300))

	if err := client.DeleteWebhook(ctx, webhook.ID); err != nil {
		exitOnError(err)
	}
	fmt.Println("Webhook deleted.")
}

func exitOnError(err error) {
	var apiErr *boxkite.APIError
	if errors.As(err, &apiErr) {
		fmt.Fprintf(os.Stderr, "API error: %s [%s]\n", apiErr.Message, apiErr.Code)
		os.Exit(1)
	}
	log.Fatal(err)
}
