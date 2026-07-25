package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestUsage(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/usage" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"monthly_sandbox_hours_used": 1.5, "monthly_sandbox_hours_limit": 20, "concurrent_sandboxes": 1, "concurrent_sandboxes_limit": 3}`)
	})
	defer closeServer()

	usage, err := client.Usage(context.Background())
	if err != nil {
		t.Fatalf("Usage: %v", err)
	}
	if usage.MonthlySandboxHoursUsed != 1.5 || usage.ConcurrentSandboxesLimit != 3 {
		t.Errorf("unexpected usage: %+v", usage)
	}
}

func TestRequestPasswordReset_PostsEmail(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/auth/password-reset/request" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["email"] != "user@example.com" {
			t.Errorf("unexpected email: %v", body["email"])
		}
		writeJSON(t, w, 200, `{"message": "If an account with that email exists, a password reset link has been sent."}`)
	})
	defer closeServer()

	result, err := client.RequestPasswordReset(context.Background(), "user@example.com")
	if err != nil {
		t.Fatalf("RequestPasswordReset: %v", err)
	}
	if result.Message == "" {
		t.Error("expected a non-empty message")
	}
}

func TestConfirmPasswordReset_RaisesOnInvalidToken(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 400, `{"error": {"code": "invalid_or_expired_token", "message": "This password reset link is invalid or has expired."}}`)
	})
	defer closeServer()

	_, err := client.ConfirmPasswordReset(context.Background(), "bad-tok", "new-hunter2")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "invalid_or_expired_token" {
		t.Errorf("unexpected code: %s", apiErr.Code)
	}
}

func TestVerifyEmail_PostsToken(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["token"] != "verify-tok" {
			t.Errorf("unexpected token: %v", body["token"])
		}
		writeJSON(t, w, 200, `{"message": "Email verified."}`)
	})
	defer closeServer()

	result, err := client.VerifyEmail(context.Background(), "verify-tok")
	if err != nil {
		t.Fatalf("VerifyEmail: %v", err)
	}
	if result.Message != "Email verified." {
		t.Errorf("unexpected message: %s", result.Message)
	}
}

func TestResendVerification_OverridesAuthorizationWithAccessToken(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer dashboard-jwt-123" {
			t.Errorf("expected the dashboard JWT to replace the client's api_key, got %q", got)
		}
		writeJSON(t, w, 200, `{"message": "Verification email sent."}`)
	})
	defer closeServer()

	result, err := client.ResendVerification(context.Background(), "dashboard-jwt-123")
	if err != nil {
		t.Fatalf("ResendVerification: %v", err)
	}
	if result.Message != "Verification email sent." {
		t.Errorf("unexpected message: %s", result.Message)
	}
}

func TestRefreshToken_PostsRefreshTokenAndReturnsNewPair(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["refresh_token"] != "old-refresh" {
			t.Errorf("unexpected refresh_token: %v", body["refresh_token"])
		}
		writeJSON(t, w, 200, `{
			"access_token": "new-jwt", "token_type": "bearer", "expires_in": 3600, "refresh_token": "new-refresh",
			"account": {"id": "acct-1", "email": "a@example.com", "created_at": "2026-01-01T00:00:00Z"}
		}`)
	})
	defer closeServer()

	pair, err := client.RefreshToken(context.Background(), "old-refresh")
	if err != nil {
		t.Fatalf("RefreshToken: %v", err)
	}
	if pair.AccessToken != "new-jwt" || pair.RefreshToken == nil || *pair.RefreshToken != "new-refresh" {
		t.Errorf("unexpected pair: %+v", pair)
	}
}

func TestRefreshToken_RaisesOnReusedToken(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 401, `{"error": {"code": "refresh_token_reused", "message": "This refresh token has already been used."}}`)
	})
	defer closeServer()

	_, err := client.RefreshToken(context.Background(), "already-used")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != "refresh_token_reused" || apiErr.StatusCode != 401 {
		t.Errorf("unexpected APIError: %+v", apiErr)
	}
}

func TestLogout_PostsRefreshTokenAndReturnsNoError(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["refresh_token"] != "some-refresh" {
			t.Errorf("unexpected refresh_token: %v", body["refresh_token"])
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.Logout(context.Background(), "some-refresh"); err != nil {
		t.Fatalf("Logout: %v", err)
	}
}

func TestGetAllowedCommands(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/account/allowed-commands" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"rules": ["git", {"command": "python3", "args_allow": ["^-c .*"], "args_deny": []}]}`)
	})
	defer closeServer()

	result, err := client.GetAllowedCommands(context.Background())
	if err != nil {
		t.Fatalf("GetAllowedCommands: %v", err)
	}
	if len(result.Rules) != 2 {
		t.Fatalf("expected 2 rules, got %d", len(result.Rules))
	}
	if result.Rules[0].Command != "git" {
		t.Errorf("unexpected first rule: %+v", result.Rules[0])
	}
	if result.Rules[1].Command != "python3" || len(result.Rules[1].ArgsAllow) != 1 {
		t.Errorf("unexpected second rule: %+v", result.Rules[1])
	}
}

func TestSetAllowedCommands_SendsRulesAndReturnsEcho(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut || r.URL.Path != "/v1/account/allowed-commands" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		rules, _ := body["rules"].([]any)
		if len(rules) != 1 {
			t.Fatalf("expected 1 rule, got %v", body["rules"])
		}
		writeJSON(t, w, 200, `{"rules": [{"command": "git", "args_allow": [], "args_deny": []}]}`)
	})
	defer closeServer()

	result, err := client.SetAllowedCommands(context.Background(), []AllowedCommandRule{{Command: "git"}})
	if err != nil {
		t.Fatalf("SetAllowedCommands: %v", err)
	}
	if len(result.Rules) != 1 || result.Rules[0].Command != "git" {
		t.Errorf("unexpected result: %+v", result)
	}
}

func TestClearAllowedCommands(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/account/allowed-commands" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.ClearAllowedCommands(context.Background()); err != nil {
		t.Fatalf("ClearAllowedCommands: %v", err)
	}
}
