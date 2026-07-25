import assert from "node:assert/strict";
import { test } from "node:test";
import { WebSocketServer } from "ws";

import { BoxkiteApiError, BoxkiteClient, BoxkiteConnectionError, SandboxSession } from "../src/index.js";

type Handler = (req: {
  method: string;
  url: string;
  body: unknown;
  headers: Record<string, string>;
}) => Response;

function fakeFetch(handler: Handler): typeof fetch {
  return (async (input: any, init?: any) => {
    const url = typeof input === "string" ? input : input.toString();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    return handler({ method: init?.method ?? "GET", url, body, headers: init?.headers ?? {} });
  }) as typeof fetch;
}

function clientWith(handler: Handler): BoxkiteClient {
  return new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: fakeFetch(handler),
  });
}

test("rejects a plain http baseUrl to a remote host", () => {
  // apiKey is a full-privilege, long-lived credential sent as
  // `Authorization: Bearer` on every request -- an http:// URL to anything
  // other than localhost would put it on the wire in cleartext.
  assert.throws(
    () => new BoxkiteClient({ baseUrl: "http://cp.example.com", apiKey: "bxk_live_test" }),
    /cleartext/,
  );
});

test("allows http localhost for local dev", () => {
  const client = new BoxkiteClient({ baseUrl: "http://localhost:8090", apiKey: "bxk_live_test" });
  assert.ok(client);
});

test("account returns parsed body", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/account");
    return new Response(JSON.stringify({ id: "acct-1", email: "a@example.com" }), { status: 200 });
  });

  const result = await client.account();
  assert.deepEqual(result, { id: "acct-1", email: "a@example.com" });
});

test("requestPasswordReset posts email", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/auth/password-reset/request");
    assert.deepEqual(body, { email: "user@example.com" });
    return new Response(
      JSON.stringify({ message: "If an account with that email exists, a password reset link has been sent." }),
      { status: 200 },
    );
  });

  const result = await client.requestPasswordReset("user@example.com");
  assert.ok(result.message.startsWith("If an account with that email exists"));
});

test("confirmPasswordReset posts token and newPassword", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/auth/password-reset/confirm");
    assert.deepEqual(body, { token: "reset-tok", new_password: "new-hunter2" });
    return new Response(
      JSON.stringify({ message: "Password has been reset. Please log in with your new password." }),
      { status: 200 },
    );
  });

  const result = await client.confirmPasswordReset("reset-tok", "new-hunter2");
  assert.ok(result.message.startsWith("Password has been reset"));
});

test("confirmPasswordReset throws BoxkiteApiError on invalid token", async () => {
  const client = clientWith(() =>
    new Response(
      JSON.stringify({
        error: { code: "invalid_or_expired_token", message: "This password reset link is invalid or has expired." },
      }),
      { status: 400 },
    ),
  );

  await assert.rejects(
    () => client.confirmPasswordReset("bad-tok", "new-hunter2"),
    (err: unknown) => err instanceof BoxkiteApiError && err.code === "invalid_or_expired_token",
  );
});

test("verifyEmail posts token", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/auth/verify-email");
    assert.deepEqual(body, { token: "verify-tok" });
    return new Response(JSON.stringify({ message: "Email verified." }), { status: 200 });
  });

  const result = await client.verifyEmail("verify-tok");
  assert.deepEqual(result, { message: "Email verified." });
});

test("resendVerification overrides Authorization with the access token", async () => {
  const client = clientWith(({ url, headers }) => {
    assert.equal(new URL(url).pathname, "/v1/auth/resend-verification");
    // The dashboard JWT must replace, not merely accompany, the client's
    // apiKey on this one call -- this control-plane rejects an apiKey on
    // a route that requires a user session token.
    assert.equal(headers.Authorization, "Bearer dashboard-jwt-123");
    return new Response(JSON.stringify({ message: "Verification email sent." }), { status: 200 });
  });

  const result = await client.resendVerification("dashboard-jwt-123");
  assert.deepEqual(result, { message: "Verification email sent." });
});

test("refreshToken posts refreshToken and returns a new pair", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/auth/refresh");
    assert.deepEqual(body, { refresh_token: "old-refresh" });
    return new Response(
      JSON.stringify({
        access_token: "new-jwt",
        token_type: "bearer",
        expires_in: 3600,
        refresh_token: "new-refresh",
        account: { id: "acct-1", email: "a@example.com", created_at: "2026-01-01T00:00:00Z" },
      }),
      { status: 200 },
    );
  });

  const result = await client.refreshToken("old-refresh");
  assert.equal(result.access_token, "new-jwt");
  assert.equal(result.refresh_token, "new-refresh");
});

test("refreshToken throws BoxkiteApiError on a reused token", async () => {
  const client = clientWith(() =>
    new Response(
      JSON.stringify({
        error: { code: "refresh_token_reused", message: "This refresh token has already been used." },
      }),
      { status: 401 },
    ),
  );

  await assert.rejects(
    () => client.refreshToken("already-used"),
    (err: unknown) => err instanceof BoxkiteApiError && err.code === "refresh_token_reused" && err.statusCode === 401,
  );
});

test("logout posts refreshToken and resolves on 204", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/auth/logout");
    assert.deepEqual(body, { refresh_token: "old-refresh" });
    return new Response(null, { status: 204 });
  });

  const result = await client.logout("old-refresh");
  assert.equal(result, undefined);
});

test("createSandbox sends label", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes");
    assert.deepEqual(body, { label: "demo" });
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  const result = await client.createSandbox({ label: "demo" });
  assert.equal(result.id, "sess-1");
});

test("createSandbox sends imageId", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes");
    assert.deepEqual(body, { image_id: "img-1" });
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  const result = await client.createSandbox({ imageId: "img-1" });
  assert.equal(result.id, "sess-1");
});

test("createSandbox sends mcpConnectionNames", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes");
    assert.deepEqual(body, { mcp_connection_names: ["team-slack"] });
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  const result = await client.createSandbox({ mcpConnectionNames: ["team-slack"] });
  assert.equal(result.id, "sess-1");
});

test("createSandbox sends volumeMounts", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes");
    assert.deepEqual(body, { volume_mounts: { "vol-1": "/mnt/data" } });
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  const result = await client.createSandbox({ volumeMounts: { "vol-1": "/mnt/data" } });
  assert.equal(result.id, "sess-1");
});

test("createSandbox omits volumeMounts when not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {});
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  await client.createSandbox();
});

test("createSandbox sends gpuCount", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes");
    assert.deepEqual(body, { gpu_count: 2 });
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  const result = await client.createSandbox({ gpuCount: 2 });
  assert.equal(result.id, "sess-1");
});

test("createSandbox omits gpuCount when not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {});
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  await client.createSandbox();
});

test("createImage sends label, base, pythonPackages, and aptPackages", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/images");
    assert.deepEqual(body, {
      label: "my-image",
      base: "boxkite-minimal",
      python_packages: ["requests==2.31.0"],
      apt_packages: ["curl==7.81.0-1"],
    });
    return new Response(JSON.stringify({ id: "img-1", label: "my-image", status: "pending" }), { status: 202 });
  });

  const result = await client.createImage({
    label: "my-image",
    base: "boxkite-minimal",
    pythonPackages: ["requests==2.31.0"],
    aptPackages: ["curl==7.81.0-1"],
  });
  assert.equal(result.id, "img-1");
});

test("createImage sends npmPackages and a boxkite-node base", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/images");
    assert.deepEqual(body, {
      base: "boxkite-node",
      npm_packages: ["@anthropic-ai/claude-code==2.0.1"],
    });
    return new Response(JSON.stringify({ id: "img-2", status: "pending" }), { status: 202 });
  });

  const result = await client.createImage({
    base: "boxkite-node",
    npmPackages: ["@anthropic-ai/claude-code==2.0.1"],
  });
  assert.equal(result.id, "img-2");
});

test("createImage accepts a boxkite-go base", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, { base: "boxkite-go" });
    return new Response(JSON.stringify({ id: "img-3", status: "pending" }), { status: 202 });
  });

  const result = await client.createImage({ base: "boxkite-go" });
  assert.equal(result.id, "img-3");
});

test("createImage omits npmPackages when not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, { base: "boxkite-minimal" });
    return new Response(JSON.stringify({ id: "img-4", status: "pending" }), { status: 202 });
  });

  await client.createImage({ base: "boxkite-minimal" });
});

test("createImage omits fields that are not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {});
    return new Response(JSON.stringify({ id: "img-1", status: "pending" }), { status: 202 });
  });

  await client.createImage();
});

test("getImage gets the image route", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/images/img-1");
    return new Response(
      JSON.stringify({ id: "img-1", label: "my-image", base: "boxkite-default", status: "ready" }),
      { status: 200 },
    );
  });

  const result = await client.getImage("img-1");
  assert.equal(result.status, "ready");
});

test("listImages returns the array", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/images");
    return new Response(JSON.stringify([{ id: "img-1" }]), { status: 200 });
  });

  const result = await client.listImages();
  assert.deepEqual(result, [{ id: "img-1" }]);
});

test("listImages returns empty array when there are none", async () => {
  const client = clientWith(() => new Response(JSON.stringify([]), { status: 200 }));

  const result = await client.listImages();
  assert.deepEqual(result, []);
});

test("deleteImage resolves on 204", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "DELETE");
    assert.equal(new URL(url).pathname, "/v1/images/img-1");
    return new Response(null, { status: 204 });
  });

  await assert.doesNotReject(client.deleteImage("img-1"));
});

test("createVolume sends label and sizeGb", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/volumes");
    assert.deepEqual(body, { size_gb: 10, label: "my-volume" });
    return new Response(
      JSON.stringify({ id: "vol-1", label: "my-volume", size_gb: 10, status: "queued" }),
      { status: 202 },
    );
  });

  const result = await client.createVolume({ label: "my-volume", sizeGb: 10 });
  assert.equal(result.id, "vol-1");
});

test("createVolume omits label when not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, { size_gb: 5 });
    return new Response(JSON.stringify({ id: "vol-2", size_gb: 5, status: "queued" }), { status: 202 });
  });

  await client.createVolume({ sizeGb: 5 });
});

test("getVolume gets the volume route", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/volumes/vol-1");
    return new Response(JSON.stringify({ id: "vol-1", size_gb: 10, status: "ready" }), { status: 200 });
  });

  const result = await client.getVolume("vol-1");
  assert.equal(result.status, "ready");
});

test("listVolumes returns the array", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/volumes");
    return new Response(JSON.stringify([{ id: "vol-1" }]), { status: 200 });
  });

  const result = await client.listVolumes();
  assert.deepEqual(result, [{ id: "vol-1" }]);
});

test("listVolumes returns empty array when there are none", async () => {
  const client = clientWith(() => new Response(JSON.stringify([]), { status: 200 }));

  const result = await client.listVolumes();
  assert.deepEqual(result, []);
});

test("deleteVolume resolves on 204", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "DELETE");
    assert.equal(new URL(url).pathname, "/v1/volumes/vol-1");
    return new Response(null, { status: 204 });
  });

  await assert.doesNotReject(client.deleteVolume("vol-1"));
});

test("createWebhook sends url and eventTypes", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/webhooks");
    assert.deepEqual(body, {
      url: "https://example.com/hook",
      event_types: ["sandbox.created"],
    });
    return new Response(
      JSON.stringify({
        id: "wh-1",
        url: "https://example.com/hook",
        event_types: ["sandbox.created"],
        description: null,
        is_active: true,
        created_at: "now",
        secret: "whsec_abc123",
      }),
      { status: 201 },
    );
  });

  const result = await client.createWebhook({
    url: "https://example.com/hook",
    eventTypes: ["sandbox.created"],
  });
  assert.equal(result.id, "wh-1");
  assert.equal(result.secret, "whsec_abc123");
});

test("createWebhook accepts audit_log.entry as an eventType", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/webhooks");
    assert.deepEqual(body, {
      url: "https://example.com/hook",
      event_types: ["audit_log.entry"],
    });
    return new Response(
      JSON.stringify({
        id: "wh-3",
        url: "https://example.com/hook",
        event_types: ["audit_log.entry"],
        description: null,
        is_active: true,
        created_at: "now",
        secret: "whsec_ghi789",
      }),
      { status: 201 },
    );
  });

  const result = await client.createWebhook({
    url: "https://example.com/hook",
    eventTypes: ["audit_log.entry"],
  });
  assert.equal(result.id, "wh-3");
  assert.deepEqual(result.event_types, ["audit_log.entry"]);
});

test("createWebhook sends description when given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {
      url: "https://example.com/hook",
      event_types: ["sandbox.destroyed"],
      description: "Slack notifier",
    });
    return new Response(
      JSON.stringify({
        id: "wh-2",
        url: "https://example.com/hook",
        event_types: ["sandbox.destroyed"],
        description: "Slack notifier",
        is_active: true,
        created_at: "now",
        secret: "whsec_def456",
      }),
      { status: 201 },
    );
  });

  const result = await client.createWebhook({
    url: "https://example.com/hook",
    eventTypes: ["sandbox.destroyed"],
    description: "Slack notifier",
  });
  assert.equal(result.description, "Slack notifier");
});

test("createWebhook omits description when not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {
      url: "https://example.com/hook",
      event_types: ["sandbox.created"],
    });
    return new Response(
      JSON.stringify({
        id: "wh-3",
        url: "https://example.com/hook",
        event_types: ["sandbox.created"],
        description: null,
        is_active: true,
        created_at: "now",
        secret: "whsec_ghi789",
      }),
      { status: 201 },
    );
  });

  await client.createWebhook({ url: "https://example.com/hook", eventTypes: ["sandbox.created"] });
});

test("listWebhooks returns the array", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/webhooks");
    return new Response(JSON.stringify([{ id: "wh-1" }]), { status: 200 });
  });

  const result = await client.listWebhooks();
  assert.deepEqual(result, [{ id: "wh-1" }]);
});

test("listWebhooks returns empty array when there are none", async () => {
  const client = clientWith(() => new Response(JSON.stringify([]), { status: 200 }));

  const result = await client.listWebhooks();
  assert.deepEqual(result, []);
});

test("deleteWebhook resolves on 204", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "DELETE");
    assert.equal(new URL(url).pathname, "/v1/webhooks/wh-1");
    return new Response(null, { status: 204 });
  });

  await assert.doesNotReject(client.deleteWebhook("wh-1"));
});

test("listWebhookDeliveries sends limit and offset", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    const parsed = new URL(url);
    assert.equal(parsed.pathname, "/v1/webhooks/wh-1/deliveries");
    assert.equal(parsed.searchParams.get("limit"), "10");
    assert.equal(parsed.searchParams.get("offset"), "5");
    return new Response(JSON.stringify([{ id: "del-1", status: "delivered" }]), { status: 200 });
  });

  const result = await client.listWebhookDeliveries("wh-1", { limit: 10, offset: 5 });
  assert.equal(result[0].status, "delivered");
});

test("listWebhookDeliveries omits params when not given", async () => {
  const client = clientWith(({ url }) => {
    const parsed = new URL(url);
    assert.equal(parsed.search, "");
    return new Response(JSON.stringify([]), { status: 200 });
  });

  const result = await client.listWebhookDeliveries("wh-1");
  assert.deepEqual(result, []);
});

test("createMcpConnection sends label and catalogId", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/mcp-connections");
    assert.deepEqual(body, { label: "team-slack", catalog_id: "slack" });
    return new Response(
      JSON.stringify({
        id: "mcpconn-1",
        label: "team-slack",
        catalog_id: "slack",
        host: "mcp.slack.com",
        created_at: "now",
        last_used_at: null,
      }),
      { status: 201 },
    );
  });

  const result = await client.createMcpConnection({ label: "team-slack", catalogId: "slack" });
  assert.equal(result.id, "mcpconn-1");
  assert.equal(result.host, "mcp.slack.com");
});

test("listMcpConnections returns the array", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/mcp-connections");
    return new Response(JSON.stringify([{ id: "mcpconn-1" }]), { status: 200 });
  });

  const result = await client.listMcpConnections();
  assert.deepEqual(result, [{ id: "mcpconn-1" }]);
});

test("listMcpConnections returns empty array when there are none", async () => {
  const client = clientWith(() => new Response(JSON.stringify([]), { status: 200 }));

  const result = await client.listMcpConnections();
  assert.deepEqual(result, []);
});

test("deleteMcpConnection resolves on 204", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "DELETE");
    assert.equal(new URL(url).pathname, "/v1/mcp-connections/mcpconn-1");
    return new Response(null, { status: 204 });
  });

  await assert.doesNotReject(client.deleteMcpConnection("mcpconn-1"));
});

test("createSecret sends name, value, and allowedHosts", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/secrets");
    assert.deepEqual(body, {
      name: "stripe-key",
      value: "sk_test_abc123",
      allowed_hosts: ["api.stripe.com"],
    });
    return new Response(
      JSON.stringify({
        id: "secret-1",
        name: "stripe-key",
        allowed_hosts: ["api.stripe.com"],
        trust_tier: null,
        created_at: "now",
        last_used_at: null,
      }),
      { status: 201 },
    );
  });

  const result = await client.createSecret({
    name: "stripe-key",
    value: "sk_test_abc123",
    allowedHosts: ["api.stripe.com"],
  });
  assert.equal(result.id, "secret-1");
  // The raw value is never returned -- Secret has no `value` field, so
  // assert its absence at runtime through an untyped view.
  assert.equal((result as unknown as Record<string, unknown>).value, undefined);
});

test("createSecret sends trustTier when given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {
      name: "wallet-key",
      value: "0xabc",
      allowed_hosts: ["rpc.example.com"],
      trust_tier: "testnet",
    });
    return new Response(
      JSON.stringify({
        id: "secret-2",
        name: "wallet-key",
        allowed_hosts: ["rpc.example.com"],
        trust_tier: "testnet",
        created_at: "now",
        last_used_at: null,
      }),
      { status: 201 },
    );
  });

  const result = await client.createSecret({
    name: "wallet-key",
    value: "0xabc",
    allowedHosts: ["rpc.example.com"],
    trustTier: "testnet",
  });
  assert.equal(result.trust_tier, "testnet");
});

test("listSecrets returns the array", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/secrets");
    return new Response(JSON.stringify([{ id: "secret-1" }]), { status: 200 });
  });

  const result = await client.listSecrets();
  assert.deepEqual(result, [{ id: "secret-1" }]);
});

test("listSecrets returns empty array when there are none", async () => {
  const client = clientWith(() => new Response(JSON.stringify([]), { status: 200 }));

  const result = await client.listSecrets();
  assert.deepEqual(result, []);
});

test("deleteSecret resolves on 204", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "DELETE");
    assert.equal(new URL(url).pathname, "/v1/secrets/secret-1");
    return new Response(null, { status: 204 });
  });

  await assert.doesNotReject(client.deleteSecret("secret-1"));
});

test("exec posts command", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/exec");
    return new Response(JSON.stringify({ exit_code: 0, stdout: "hi\n", stderr: "" }), { status: 200 });
  });

  const result = await client.exec("sess-1", "echo hi");
  assert.equal(result.exit_code, 0);
});

test("createSandbox sends secretNames", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes");
    assert.deepEqual(body, { secret_names: ["prod-stripe"] });
    return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
  });

  const result = await client.createSandbox({ secretNames: ["prod-stripe"] });
  assert.equal(result.id, "sess-1");
});

test("httpRequest posts method, url, headers, and body", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/http-request");
    assert.deepEqual(body, {
      method: "POST",
      url: "https://api.example.com/v1/charges",
      headers: { Authorization: "Bearer {{secret:prod-stripe}}" },
      body: "amount=2000",
    });
    return new Response(
      JSON.stringify({ status_code: 200, headers: { "content-type": "text/plain" }, body: "ok", truncated: false }),
      { status: 200 },
    );
  });

  const result = await client.httpRequest("sess-1", "POST", "https://api.example.com/v1/charges", {
    headers: { Authorization: "Bearer {{secret:prod-stripe}}" },
    body: "amount=2000",
  });
  assert.equal(result.status_code, 200);
  assert.equal(result.body, "ok");
});

test("SandboxSession wraps httpRequest", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/http-request");
    return new Response(JSON.stringify({ status_code: 200, headers: {}, body: "ok", truncated: false }), {
      status: 200,
    });
  });

  const sb = new SandboxSession(client, "sess-1");
  const result = await sb.httpRequest("GET", "https://api.example.com/");
  assert.equal(result.status_code, 200);
});

test("destroySandbox resolves on 204", async () => {
  const client = clientWith(({ method }) => {
    assert.equal(method, "DELETE");
    return new Response(null, { status: 204 });
  });

  await assert.doesNotReject(client.destroySandbox("sess-1"));
});

test("api error parses envelope", async () => {
  const client = clientWith(() =>
    new Response(JSON.stringify({ error: { code: "not_found", message: "Sandbox session not found" } }), {
      status: 404,
    }),
  );

  await assert.rejects(
    client.getSandbox("missing"),
    (err: unknown) => {
      assert.ok(err instanceof BoxkiteApiError);
      if (err instanceof BoxkiteApiError) {
        assert.equal(err.statusCode, 404);
        assert.equal(err.code, "not_found");
      }
      return true;
    },
  );
});

test("connection error wrapped", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: (async () => {
      throw new Error("boom");
    }) as unknown as typeof fetch,
  });

  await assert.rejects(client.account(), BoxkiteConnectionError);
});

test("withSandbox creates and destroys", async () => {
  const calls: string[] = [];
  const client = clientWith(({ method, url }) => {
    const path = new URL(url).pathname;
    calls.push(`${method} ${path}`);
    if (method === "POST" && path === "/v1/sandboxes") {
      return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
    }
    if (method === "DELETE") return new Response(null, { status: 204 });
    if (path === "/v1/sandboxes/sess-1/exec") {
      return new Response(JSON.stringify({ exit_code: 0, stdout: "ok\n", stderr: "" }), { status: 200 });
    }
    throw new Error(`unexpected call: ${method} ${path}`);
  });

  const result = await client.withSandbox(async (sb) => {
    assert.equal(sb.id, "sess-1");
    return sb.exec("echo hi");
  }, { label: "ctx-demo" });

  assert.equal(result.exit_code, 0);
  assert.ok(calls.includes("POST /v1/sandboxes"));
  assert.ok(calls.includes("DELETE /v1/sandboxes/sess-1"));
});

test("ls posts path", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/ls");
    assert.deepEqual(body, { path: "/workspace" });
    return new Response(JSON.stringify({ entries: [{ name: "a.txt", type: "file" }] }), { status: 200 });
  });

  const result = await client.ls("sess-1", { path: "/workspace" });
  assert.deepEqual(result.entries, [{ name: "a.txt", type: "file" }]);
});

test("glob posts pattern and path", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/glob");
    assert.deepEqual(body, { pattern: "**/*.py", path: "/src" });
    return new Response(JSON.stringify({ matches: [{ path: "/src/main.py" }] }), { status: 200 });
  });

  const result = await client.glob("sess-1", "**/*.py", { path: "/src" });
  assert.deepEqual(result.matches, [{ path: "/src/main.py" }]);
});

test("grep posts pattern, path, glob, and maxMatches", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/grep");
    assert.deepEqual(body, { pattern: "TODO", path: "/src", glob: "*.ts", max_matches: 10 });
    return new Response(JSON.stringify({ matches: [], error: null, truncated: false }), { status: 200 });
  });

  const result = await client.grep("sess-1", "TODO", { path: "/src", glob: "*.ts", maxMatches: 10 });
  assert.deepEqual(result, { matches: [], error: null, truncated: false });
});

test("getLog sends limit and offset as query params", async () => {
  const client = clientWith(({ url }) => {
    const parsed = new URL(url);
    assert.equal(parsed.pathname, "/v1/sandboxes/sess-1/log");
    assert.equal(parsed.searchParams.get("limit"), "50");
    assert.equal(parsed.searchParams.get("offset"), "10");
    return new Response(JSON.stringify({ entries: [{ operation: "exec" }] }), { status: 200 });
  });

  const result = await client.getLog("sess-1", { limit: 50, offset: 10 });
  assert.deepEqual(result, { entries: [{ operation: "exec" }] });
});

test("getLog omits query params when not given", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).search, "");
    return new Response(JSON.stringify({ entries: [] }), { status: 200 });
  });

  await client.getLog("sess-1");
});

test("startProcess posts command, description, and maxRuntimeSeconds", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes");
    assert.deepEqual(body, { command: "npm run dev", max_runtime_seconds: 1800, description: "dev server" });
    return new Response(JSON.stringify({ process_id: "proc_1", status: "running", started_at: "now" }), {
      status: 201,
    });
  });

  const result = await client.startProcess("sess-1", "npm run dev", {
    description: "dev server",
    maxRuntimeSeconds: 1800,
  });
  assert.equal(result.process_id, "proc_1");
});

test("startProcess defaults maxRuntimeSeconds to 3600", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, { command: "sleep 5", max_runtime_seconds: 3600 });
    return new Response(JSON.stringify({ process_id: "proc_1", status: "running", started_at: "now" }), {
      status: 201,
    });
  });

  await client.startProcess("sess-1", "sleep 5");
});

test("listProcesses gets the processes route", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "GET");
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes");
    return new Response(JSON.stringify({ processes: [{ process_id: "proc_1" }] }), { status: 200 });
  });

  const result = await client.listProcesses("sess-1");
  assert.deepEqual(result, { processes: [{ process_id: "proc_1" }] });
});

test("getProcessOutput passes sinceOffset as a query param", async () => {
  const client = clientWith(({ url }) => {
    const parsed = new URL(url);
    assert.equal(parsed.pathname, "/v1/sandboxes/sess-1/processes/proc_1/output");
    assert.equal(parsed.searchParams.get("since_offset"), "10");
    return new Response(
      JSON.stringify({ status: "running", stdout_chunk: "x", next_offset: 11, truncated: false, exit_code: null }),
      { status: 200 },
    );
  });

  const result = await client.getProcessOutput("sess-1", "proc_1", { sinceOffset: 10 });
  assert.equal(result.stdout_chunk, "x");
});

test("getProcessOutput defaults sinceOffset to 0", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).searchParams.get("since_offset"), "0");
    return new Response(
      JSON.stringify({ status: "running", stdout_chunk: "", next_offset: 0, truncated: false, exit_code: null }),
      { status: 200 },
    );
  });

  await client.getProcessOutput("sess-1", "proc_1");
});

test("sendProcessInput posts data", async () => {
  const client = clientWith(({ url, body }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes/proc_1/input");
    assert.deepEqual(body, { data: "y\n" });
    return new Response(JSON.stringify({ bytes_written: 2 }), { status: 200 });
  });

  const result = await client.sendProcessInput("sess-1", "proc_1", "y\n");
  assert.deepEqual(result, { bytes_written: 2 });
});

test("stopProcess posts to the stop route", async () => {
  const client = clientWith(({ method, url }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes/proc_1/stop");
    return new Response(JSON.stringify({ status: "stopped", exit_code: 143 }), { status: 200 });
  });

  const result = await client.stopProcess("sess-1", "proc_1");
  assert.deepEqual(result, { status: "stopped", exit_code: 143 });
});

test("SandboxSession wraps process methods", async () => {
  const client = clientWith(({ method, url }) => {
    const path = new URL(url).pathname;
    if (method === "POST" && path === "/v1/sandboxes/sess-1/processes") {
      return new Response(JSON.stringify({ process_id: "proc_1", status: "running", started_at: "now" }), {
        status: 201,
      });
    }
    if (method === "GET" && path === "/v1/sandboxes/sess-1/processes") {
      return new Response(JSON.stringify({ processes: [{ process_id: "proc_1" }] }), { status: 200 });
    }
    if (path === "/v1/sandboxes/sess-1/processes/proc_1/output") {
      return new Response(
        JSON.stringify({ status: "running", stdout_chunk: "hi", next_offset: 2, truncated: false, exit_code: null }),
        { status: 200 },
      );
    }
    if (path === "/v1/sandboxes/sess-1/processes/proc_1/input") {
      return new Response(JSON.stringify({ bytes_written: 1 }), { status: 200 });
    }
    if (path === "/v1/sandboxes/sess-1/processes/proc_1/stop") {
      return new Response(JSON.stringify({ status: "stopped", exit_code: 0 }), { status: 200 });
    }
    throw new Error(`unexpected call: ${method} ${path}`);
  });

  const sb = new SandboxSession(client, "sess-1");
  const started = await sb.startProcess("sleep 5");
  assert.equal(started.process_id, "proc_1");
  assert.deepEqual(await sb.listProcesses(), { processes: [{ process_id: "proc_1" }] });
  const output = await sb.getProcessOutput("proc_1");
  assert.equal(output.stdout_chunk, "hi");
  assert.deepEqual(await sb.sendProcessInput("proc_1", "x"), { bytes_written: 1 });
  assert.deepEqual(await sb.stopProcess("proc_1"), { status: "stopped", exit_code: 0 });
});

function sseFetch(body: string, status = 200): typeof fetch {
  return (async () =>
    new Response(body, { status, headers: { "content-type": "text/event-stream" } })) as unknown as typeof fetch;
}

test("watch yields parsed SSE entries", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: sseFetch(
      'data: {"operation":"exec","detail":{"command":"echo hi"}}\n\n' +
        'data: {"operation":"ls","detail":{"path":"/"}}\n\n',
    ),
  });

  const entries: unknown[] = [];
  for await (const entry of client.watch("sess-1")) {
    entries.push(entry);
  }

  assert.deepEqual(entries, [
    { operation: "exec", detail: { command: "echo hi" } },
    { operation: "ls", detail: { path: "/" } },
  ]);
});

test("watch raises BoxkiteApiError on an error status", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: (async () =>
      new Response(JSON.stringify({ error: { code: "not_found", message: "no such session" } }), {
        status: 404,
      })) as unknown as typeof fetch,
  });

  await assert.rejects(
    async () => {
      for await (const _entry of client.watch("sess-1")) {
        // draining is enough to trigger the rejection
      }
    },
    (err: unknown) => err instanceof BoxkiteApiError && err.statusCode === 404,
  );
});

test("streamProcessOutput yields output then exit events", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: sseFetch(
      'event: output\ndata: {"type":"output","stdout_chunk":"hi","next_offset":2,"truncated":false}\n\n' +
        'event: exit\ndata: {"type":"exit","status":"exited","exit_code":0}\n\n',
    ),
  });

  const events: unknown[] = [];
  for await (const ev of client.streamProcessOutput("sess-1", "proc-1")) {
    events.push(ev);
  }

  assert.deepEqual(events, [
    { type: "output", stdout_chunk: "hi", next_offset: 2, truncated: false },
    { type: "exit", status: "exited", exit_code: 0 },
  ]);
});

test("streamProcessOutput raises BoxkiteApiError on an error status", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: (async () =>
      new Response(JSON.stringify({ error: { code: "not_found", message: "Process not found" } }), {
        status: 404,
      })) as unknown as typeof fetch,
  });

  await assert.rejects(
    async () => {
      for await (const _ev of client.streamProcessOutput("sess-1", "proc-1")) {
        // draining triggers the rejection
      }
    },
    (err: unknown) => err instanceof BoxkiteApiError && err.statusCode === 404,
  );
});

test("SandboxSession wraps ls/glob/grep/getLog", async () => {
  const client = clientWith(({ url }) => {
    const path = new URL(url).pathname;
    if (path === "/v1/sandboxes/sess-1/files/ls") {
      return new Response(JSON.stringify({ entries: [] }), { status: 200 });
    }
    if (path === "/v1/sandboxes/sess-1/files/glob") {
      return new Response(JSON.stringify({ matches: [] }), { status: 200 });
    }
    if (path === "/v1/sandboxes/sess-1/files/grep") {
      return new Response(JSON.stringify({ matches: [], error: null, truncated: false }), { status: 200 });
    }
    if (path === "/v1/sandboxes/sess-1/log") {
      return new Response(JSON.stringify({ entries: [] }), { status: 200 });
    }
    throw new Error(`unexpected call: ${path}`);
  });

  const sb = new SandboxSession(client, "sess-1");
  assert.deepEqual(await sb.ls(), { entries: [] });
  assert.deepEqual(await sb.glob("*.ts"), { matches: [] });
  assert.deepEqual(await sb.grep("TODO"), { matches: [], error: null, truncated: false });
  assert.deepEqual(await sb.getLog(), { entries: [] });
});

test("withSandbox destroys even when callback throws", async () => {
  const destroyed: string[] = [];
  const client = clientWith(({ method, url }) => {
    const path = new URL(url).pathname;
    if (method === "POST" && path === "/v1/sandboxes") {
      return new Response(JSON.stringify({ id: "sess-1", status: "active" }), { status: 201 });
    }
    if (method === "DELETE") {
      destroyed.push(path);
      return new Response(null, { status: 204 });
    }
    throw new Error("unexpected call");
  });

  await assert.rejects(
    client.withSandbox(async () => {
      throw new Error("boom");
    }),
    /boom/,
  );

  assert.deepEqual(destroyed, ["/v1/sandboxes/sess-1"]);
});

/** Minimal fake WebSocket -- just enough of the browser/Node WebSocket
 * surface (EventTarget + open/error events + binaryType) for `takeover()`
 * to exercise, without a real socket. Mirrors `fakeFetch`'s role above. */
class FakeWebSocket extends EventTarget {
  static instances: FakeWebSocket[] = [];
  url: string;
  binaryType = "blob";
  sent: unknown[] = [];

  constructor(url: string) {
    super();
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: unknown): void {
    this.sent.push(data);
  }

  close(): void {}

  simulateOpen(): void {
    this.dispatchEvent(new Event("open"));
  }

  simulateError(): void {
    this.dispatchEvent(new Event("error"));
  }
}

/** Fakes `POST /v1/sandboxes/{id}/takeover-token`, returning a fixed token
 * string -- `takeover()` now mints this before ever opening a WebSocket. */
function fakeTakeoverTokenFetch(token = "takeover-tok-abc"): typeof fetch {
  return fakeFetch(() => new Response(JSON.stringify({ token, expires_at: "2026-01-01T00:00:30Z" }), { status: 200 }));
}

/** `takeover()` is now async (it mints a token via a real `await`ed fetch
 * before ever constructing a WebSocket), so a test can't assume the
 * FakeWebSocket instance exists the instant `takeover()` is called --
 * poll (via setImmediate, a macrotask, so it runs after the mint promise's
 * microtask chain drains) until one shows up. */
function waitForWebSocketInstance(): Promise<FakeWebSocket> {
  return new Promise((resolve) => {
    const check = () => {
      const instance = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
      if (instance) resolve(instance);
      else setImmediate(check);
    };
    check();
  });
}

test("createPreviewUrl posts ttlSeconds", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/preview/3000");
    assert.deepEqual(body, { ttl_seconds: 1800 });
    return new Response(
      JSON.stringify({ url: "/v1/sandboxes/sess-1/preview/3000/?token=abc", expires_at: "now", token_id: "tok-1" }),
      { status: 200 },
    );
  });

  const result = await client.createPreviewUrl("sess-1", 3000, { ttlSeconds: 1800 });
  assert.equal(result.token_id, "tok-1");
});

test("createPreviewUrl omits ttlSeconds when not given", async () => {
  const client = clientWith(({ body }) => {
    assert.deepEqual(body, {});
    return new Response(
      JSON.stringify({ url: "/v1/sandboxes/sess-1/preview/3000/?token=abc", expires_at: "now", token_id: "tok-2" }),
      { status: 200 },
    );
  });

  const result = await client.createPreviewUrl("sess-1", 3000);
  assert.equal(result.token_id, "tok-2");
});

test("revokePreviewUrl posts tokenId", async () => {
  const client = clientWith(({ method, url, body }) => {
    assert.equal(method, "POST");
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/preview/3000/revoke");
    assert.deepEqual(body, { token_id: "tok-1" });
    return new Response(JSON.stringify({ revoked: true, token_id: "tok-1" }), { status: 200 });
  });

  const result = await client.revokePreviewUrl("sess-1", 3000, "tok-1");
  assert.deepEqual(result, { revoked: true, token_id: "tok-1" });
});

test("SandboxSession wraps createPreviewUrl and revokePreviewUrl", async () => {
  const client = clientWith(({ url }) => {
    const path = new URL(url).pathname;
    if (path === "/v1/sandboxes/sess-1/preview/3000") {
      return new Response(JSON.stringify({ url: "/preview/3000/?token=abc", expires_at: "now", token_id: "tok-1" }), {
        status: 200,
      });
    }
    if (path === "/v1/sandboxes/sess-1/preview/3000/revoke") {
      return new Response(JSON.stringify({ revoked: true, token_id: "tok-1" }), { status: 200 });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  const sb = new SandboxSession(client, "sess-1");
  const minted = await sb.createPreviewUrl(3000);
  assert.equal(minted.token_id, "tok-1");
  const revoked = await sb.revokePreviewUrl(3000, "tok-1");
  assert.deepEqual(revoked, { revoked: true, token_id: "tok-1" });
});

test("takeover mints a takeover-token via POST, then connects to a wss:// url with ?token=", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeTakeoverTokenFetch("mint-1"),
  });

  const pending = client.takeover("sess-1");
  const socket = await waitForWebSocketInstance();
  assert.equal(socket.url, "wss://cp.example.com/v1/sandboxes/sess-1/takeover?token=mint-1");
  socket.simulateOpen();

  const resolved = await pending;
  assert.equal(resolved, socket as unknown as WebSocket);
});

test("takeover never puts the long-lived apiKey on the WS URL", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_super_secret",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeTakeoverTokenFetch("mint-2"),
  });

  const pending = client.takeover("sess-1");
  const socket = await waitForWebSocketInstance();
  assert.equal(socket.url.includes("bxk_live_super_secret"), false);
  socket.simulateOpen();
  await pending;
});

test("takeover rejects with BoxkiteApiError when the takeover-token mint call itself fails (e.g. a member-role key)", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_member",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeFetch(
      () =>
        new Response(
          JSON.stringify({ error: { code: "takeover_not_permitted", message: "not permitted" } }),
          { status: 403 },
        ),
    ),
  });

  await assert.rejects(client.takeover("sess-1"), (err: unknown) => err instanceof BoxkiteApiError);
});

test("takeover rejects with BoxkiteConnectionError when the socket errors before opening", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeTakeoverTokenFetch(),
  });

  const pending = client.takeover("sess-1");
  const socket = await waitForWebSocketInstance();
  socket.simulateError();

  await assert.rejects(pending, (err: unknown) => err instanceof BoxkiteConnectionError);
});

test("SandboxSession.takeover delegates to the client", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeTakeoverTokenFetch(),
  });
  const sb = new SandboxSession(client, "sess-1");

  const pending = sb.takeover();
  const socket = await waitForWebSocketInstance();
  socket.simulateOpen();
  const resolved = await pending;

  assert.equal((resolved as unknown as FakeWebSocket).url.includes("/v1/sandboxes/sess-1/takeover"), true);
});

/** Fakes `POST /v1/sandboxes/{id}/desktop-token`, returning a fixed token
 * string -- `desktopTakeover()` mints this before ever opening a WebSocket. */
function fakeDesktopTokenFetch(token = "desktop-tok-abc"): typeof fetch {
  return fakeFetch(
    () => new Response(JSON.stringify({ token, expires_at: "2026-01-01T00:00:30Z" }), { status: 200 }),
  );
}

test("desktopTakeover mints a desktop-token via POST, then connects to a wss:// url with ?token=", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeDesktopTokenFetch("mint-1"),
  });

  const pending = client.desktopTakeover("sess-1");
  const socket = await waitForWebSocketInstance();
  assert.equal(socket.url, "wss://cp.example.com/v1/sandboxes/sess-1/desktop?token=mint-1");
  socket.simulateOpen();

  const resolved = await pending;
  assert.equal(resolved, socket as unknown as WebSocket);
});

test("desktopTakeover never puts the long-lived apiKey on the WS URL", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_super_secret",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeDesktopTokenFetch("mint-2"),
  });

  const pending = client.desktopTakeover("sess-1");
  const socket = await waitForWebSocketInstance();
  assert.equal(socket.url.includes("bxk_live_super_secret"), false);
  socket.simulateOpen();
  await pending;
});

test("desktopTakeover rejects with BoxkiteApiError when the desktop-token mint call itself fails (e.g. a member-role key)", async () => {
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_member",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeFetch(
      () =>
        new Response(
          JSON.stringify({ error: { code: "desktop_not_permitted", message: "not permitted" } }),
          { status: 403 },
        ),
    ),
  });

  await assert.rejects(client.desktopTakeover("sess-1"), (err: unknown) => err instanceof BoxkiteApiError);
});

test("desktopTakeover rejects with BoxkiteConnectionError when the socket errors before opening", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeDesktopTokenFetch(),
  });

  const pending = client.desktopTakeover("sess-1");
  const socket = await waitForWebSocketInstance();
  socket.simulateError();

  await assert.rejects(pending, (err: unknown) => err instanceof BoxkiteConnectionError);
});

test("SandboxSession.desktopTakeover delegates to the client", async () => {
  FakeWebSocket.instances = [];
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    wsImpl: FakeWebSocket as unknown as typeof WebSocket,
    fetchImpl: fakeDesktopTokenFetch(),
  });
  const sb = new SandboxSession(client, "sess-1");

  const pending = sb.desktopTakeover();
  const socket = await waitForWebSocketInstance();
  socket.simulateOpen();
  const resolved = await pending;

  assert.equal((resolved as unknown as FakeWebSocket).url.includes("/v1/sandboxes/sess-1/desktop"), true);
});

test("desktopTakeover sends and receives raw bytes against a real local WebSocket server", async () => {
  const wss = new WebSocketServer({ host: "localhost", port: 0 });
  const received: Buffer[] = [];
  let requestUrl = "";

  wss.on("connection", (ws, req) => {
    requestUrl = req.url ?? "";
    ws.on("message", (data: Buffer) => {
      received.push(data);
      ws.send(Buffer.concat([Buffer.from("echo:"), data]));
    });
  });

  await new Promise<void>((resolve) => wss.once("listening", resolve));
  const port = (wss.address() as { port: number }).port;

  try {
    const client = new BoxkiteClient({
      baseUrl: `http://localhost:${port}`,
      apiKey: "bxk_live_test",
      fetchImpl: fakeDesktopTokenFetch("real-ws-token"),
    });
    const socket = await client.desktopTakeover("sess-1");

    const replyPromise = new Promise<ArrayBuffer>((resolve) => {
      socket.addEventListener("message", (event: MessageEvent) => resolve(event.data as ArrayBuffer), {
        once: true,
      });
    });
    socket.send(new TextEncoder().encode("hello desktop"));
    const reply = await replyPromise;
    socket.close();

    assert.equal(Buffer.from(reply).toString(), "echo:hello desktop");
    assert.equal(received.length, 1);
    assert.equal(received[0].toString(), "hello desktop");
    assert.match(requestUrl, /token=real-ws-token/);
    assert.doesNotMatch(requestUrl, /api_key=/);
  } finally {
    wss.close();
  }
});

test("takeover sends and receives raw bytes against a real local WebSocket server", async () => {
  const wss = new WebSocketServer({ host: "localhost", port: 0 });
  const received: Buffer[] = [];
  let requestUrl = "";

  wss.on("connection", (ws, req) => {
    requestUrl = req.url ?? "";
    ws.on("message", (data: Buffer) => {
      received.push(data);
      ws.send(Buffer.concat([Buffer.from("echo:"), data]));
    });
  });

  await new Promise<void>((resolve) => wss.once("listening", resolve));
  const port = (wss.address() as { port: number }).port;

  try {
    const client = new BoxkiteClient({
      baseUrl: `http://localhost:${port}`,
      apiKey: "bxk_live_test",
      fetchImpl: fakeTakeoverTokenFetch("real-ws-token"),
    });
    const socket = await client.takeover("sess-1");

    const replyPromise = new Promise<ArrayBuffer>((resolve) => {
      socket.addEventListener("message", (event: MessageEvent) => resolve(event.data as ArrayBuffer), {
        once: true,
      });
    });
    socket.send(new TextEncoder().encode("hello pty"));
    const reply = await replyPromise;
    socket.close();

    assert.equal(Buffer.from(reply).toString(), "echo:hello pty");
    assert.equal(received.length, 1);
    assert.equal(received[0].toString(), "hello pty");
    assert.match(requestUrl, /token=real-ws-token/);
    assert.doesNotMatch(requestUrl, /api_key=/);
  } finally {
    wss.close();
  }
});

// --- Retry policy (opt-in exponential backoff + jitter, issue #14) ---

/** A fetch stand-in that returns each queued Response (or throws each
 * queued Error) in order, one per call, and records how many times it was
 * called. */
function sequencedFetch(steps: Array<Response | Error>): { fetch: typeof fetch; calls: () => number } {
  let i = 0;
  const fetchImpl = (async () => {
    const step = steps[Math.min(i, steps.length - 1)];
    i++;
    if (step instanceof Error) throw step;
    return step;
  }) as typeof fetch;
  return { fetch: fetchImpl, calls: () => i };
}

function retryClient(fetchImpl: typeof fetch, maxRetries = 2): BoxkiteClient {
  return new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl,
    retry: { maxRetries, initialDelayMs: 1, maxDelayMs: 2 },
  });
}

test("retries an idempotent GET on 503 then succeeds", async () => {
  const seq = sequencedFetch([
    new Response(JSON.stringify({ error: { code: "unavailable", message: "busy" } }), { status: 503 }),
    new Response(JSON.stringify({ id: "acct-1", email: "a@example.com" }), { status: 200 }),
  ]);
  const client = retryClient(seq.fetch);

  const result = await client.account();
  assert.deepEqual(result, { id: "acct-1", email: "a@example.com" });
  assert.equal(seq.calls(), 2);
});

test("retries an idempotent GET on a connection error then succeeds", async () => {
  const seq = sequencedFetch([
    new TypeError("network down"),
    new Response(JSON.stringify([{ id: "sess-1" }]), { status: 200 }),
  ]);
  const client = retryClient(seq.fetch);

  const result = await client.listSandboxes();
  assert.equal(result.length, 1);
  assert.equal(seq.calls(), 2);
});

test("gives up after maxRetries and throws the last error", async () => {
  const seq = sequencedFetch([
    new Response(JSON.stringify({ error: { code: "unavailable", message: "busy" } }), { status: 503 }),
  ]);
  const client = retryClient(seq.fetch, 2);

  await assert.rejects(
    () => client.account(),
    (err: unknown) => err instanceof BoxkiteApiError && err.statusCode === 503,
  );
  // initial attempt + 2 retries
  assert.equal(seq.calls(), 3);
});

test("does not retry a non-idempotent POST", async () => {
  const seq = sequencedFetch([
    new Response(JSON.stringify({ error: { code: "unavailable", message: "busy" } }), { status: 503 }),
  ]);
  const client = retryClient(seq.fetch);

  await assert.rejects(() => client.createSandbox({ label: "x" }), BoxkiteApiError);
  assert.equal(seq.calls(), 1);
});

test("does not retry a 4xx that is not 429", async () => {
  const seq = sequencedFetch([
    new Response(JSON.stringify({ error: { code: "not_found", message: "gone" } }), { status: 404 }),
  ]);
  const client = retryClient(seq.fetch);

  await assert.rejects(() => client.getSandbox("missing"), BoxkiteApiError);
  assert.equal(seq.calls(), 1);
});

test("retries on 429 and honors a Retry-After header", async () => {
  const seq = sequencedFetch([
    new Response(JSON.stringify({ error: { code: "rate_limited", message: "slow down" } }), {
      status: 429,
      headers: { "Retry-After": "0" },
    }),
    new Response(JSON.stringify({ id: "acct-1", email: "a@example.com" }), { status: 200 }),
  ]);
  const client = retryClient(seq.fetch);

  const result = await client.account();
  assert.equal(result.id, "acct-1");
  assert.equal(seq.calls(), 2);
});

test("does not retry when the retry option is omitted (default off)", async () => {
  const seq = sequencedFetch([
    new Response(JSON.stringify({ error: { code: "unavailable", message: "busy" } }), { status: 503 }),
  ]);
  const client = new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: seq.fetch,
  });

  await assert.rejects(() => client.account(), BoxkiteApiError);
  assert.equal(seq.calls(), 1);
});
