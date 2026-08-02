// Runnable end-to-end example against a real hosted control-plane.
//
//	BOXXKITE_BASE_URL=https://your-control-plane BOXXKITE_API_KEY=bxk_live_... \
//	    go run ./examples/quickstart.go
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"strings"

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

	account, err := client.Account(ctx)
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf("Signed in as %s\n", account.Email)

	usage, err := client.Usage(ctx)
	if err != nil {
		exitOnError(err)
	}
	fmt.Printf(
		"Usage: %.1f/%.1f sandbox-hours, %d/%d concurrent\n",
		usage.MonthlySandboxHoursUsed, usage.MonthlySandboxHoursLimit,
		usage.ConcurrentSandboxes, usage.ConcurrentSandboxesLimit,
	)

	req := boxxkite.CreateSandboxRequest{Label: boxxkite.Ptr("sdk-go-quickstart")}
	err = client.WithSandbox(ctx, req, func(sb *boxxkite.Session) error {
		fmt.Printf("Created sandbox %s\n", sb.ID)

		result, err := sb.Exec(ctx, "python3 -c 'print(1 + 1)'", nil)
		if err != nil {
			return err
		}
		fmt.Printf("exec result: %s\n", strings.TrimSpace(result.Stdout))

		if _, err := sb.FileCreate(ctx, "hello.txt", "hello from boxxkite-client (go)\n", nil); err != nil {
			return err
		}
		viewed, err := sb.View(ctx, "hello.txt", nil)
		if err != nil {
			return err
		}
		fmt.Printf("file contents: %s\n", strings.TrimSpace(viewed.Content))
		return nil
	})
	if err != nil {
		exitOnError(err)
	}
	fmt.Println("Sandbox destroyed.")
}

func exitOnError(err error) {
	var apiErr *boxxkite.APIError
	if errors.As(err, &apiErr) {
		fmt.Fprintf(os.Stderr, "API error: %s [%s]\n", apiErr.Message, apiErr.Code)
		os.Exit(1)
	}
	log.Fatal(err)
}
