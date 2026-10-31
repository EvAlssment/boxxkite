//go:build ignore

package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"

	boxxkite "github.com/EvAlssment/boxxkite/sdk-go"
)

func main() {
	baseURL := os.Getenv("BOXXKITE_BASE_URL")
	apiKey := os.Getenv("BOXXKITE_API_KEY")
	if baseURL == "" || apiKey == "" {
		fmt.Fprintln(os.Stderr, "Set BOXXKITE_BASE_URL and BOXXKITE_API_KEY first.")
		os.Exit(1)
	}

	client, err := boxxkite.NewClient(baseURL, apiKey)
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()

	sandbox, err := client.CreateSandbox(ctx, boxxkite.CreateSandboxRequest{Label: boxxkite.Ptr("snapshot-example")})
	if err != nil {
		exitOnError(err)
	}

	snapshot, err := client.CreateSnapshot(ctx, sandbox.ID, boxxkite.CreateSnapshotRequest{Label: boxxkite.Ptr("example")})
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf("Created snapshot %s (%s)\n", snapshot.ID, snapshot.Status)

	snapshots, err := client.ListSnapshots(ctx, sandbox.ID)
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf("Session has %d snapshot(s)\n", len(snapshots))

	snapshot, err = client.GetSnapshot(ctx, snapshot.ID)
	if err != nil {
		exitOnError(err)
	}

	restored, err := client.RestoreSnapshot(ctx, snapshot.ID, boxxkite.RestoreSnapshotRequest{Label: boxxkite.Ptr("restored-example")})
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf("Restored sandbox %s (%s)\n", restored.ID, restored.Status)

	if err := client.DestroySandbox(ctx, restored.ID); err != nil {
		exitOnError(err)
	}
	if err := client.DestroySandbox(ctx, sandbox.ID); err != nil {
		exitOnError(err)
	}
	if err := client.DeleteSnapshot(ctx, snapshot.ID); err != nil {
		exitOnError(err)
	}
}

func exitOnError(err error) {
	var apiErr *boxxkite.APIError
	if errors.As(err, &apiErr) {
		fmt.Fprintf(os.Stderr, "API error: %s [%s]\n", apiErr.Message, apiErr.Code)
		os.Exit(1)
	}
	log.Fatal(err)
}
