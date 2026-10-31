package com.boxxkite;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;
import static com.boxxkite.Models.*;

class ClientTest {
    private HttpServer server;

    @AfterEach void stopServer() { if (server != null) server.stop(0); }

    @Test void sendsBearerAndDecodesTypedSandbox() throws Exception {
        server = server(exchange -> {
            assertEquals("Bearer test-key", exchange.getRequestHeaders().getFirst("Authorization"));
            assertEquals("GET", exchange.getRequestMethod());
            assertEquals("/v1/sandboxes/session%2Fone", exchange.getRequestURI().getRawPath());
            respond(exchange, 200, "{\"id\":\"session/one\",\"status\":\"running\",\"created_at\":\"2026-01-01T00:00:00Z\"}");
        });
        Sandbox result = client().getSandbox("session/one");
        assertEquals("session/one", result.id);
        assertEquals("running", result.status);
        assertEquals("2026-01-01T00:00:00Z", result.createdAt);
    }

    @Test void encodesRequestAndAsyncUsesSameTypedRoute() throws Exception {
        server = server(exchange -> {
            assertEquals("POST", exchange.getRequestMethod());
            assertTrue(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8).contains("\"command\":\"echo hi\""));
            respond(exchange, 200, "{\"exit_code\":0,\"stdout\":\"hi\\n\",\"stderr\":\"\"}");
        });
        ExecResult result = client().session("s1").execAsync("echo hi").join();
        assertEquals(0, result.exitCode);
        assertEquals("hi\n", result.stdout);
    }

    @Test void returnsStructuredApiErrorWithoutLeakingRequestCredential() throws Exception {
        server = server(exchange -> respond(exchange, 403,
                "{\"error\":{\"code\":\"command_not_allowed\",\"message\":\"blocked\",\"retryable\":false,\"remediation\":\"change policy\"}}"));
        BoxxkiteException error = assertThrows(BoxxkiteException.class, () -> client().exec("s1", "whoami"));
        assertEquals(403, error.statusCode());
        assertEquals("command_not_allowed", error.code());
        assertEquals("change policy", error.remediation());
        assertFalse(error.getMessage().contains("test-key"));
    }

    @Test void rejectsInsecureNonLoopbackBaseUrl() {
        assertThrows(IllegalArgumentException.class, () -> new Client("http://example.com", "test-key"));
        assertThrows(IllegalArgumentException.class, () -> new Client("https://example.com?token=x", "test-key"));
    }

    @Test void retriesGetOnTransientStatusButDoesNotRetryPost() throws Exception {
        AtomicInteger getCalls = new AtomicInteger();
        AtomicInteger postCalls = new AtomicInteger();
        server = server(exchange -> {
            if (exchange.getRequestMethod().equals("GET")) {
                if (getCalls.incrementAndGet() == 1) respond(exchange, 503, "{}");
                else respond(exchange, 200, "{\"id\":\"s1\",\"status\":\"running\"}");
            } else {
                postCalls.incrementAndGet(); respond(exchange, 503, "{}");
            }
        });
        Client retrying = new Client(url(), "test-key", Client.DEFAULT_TIMEOUT,
                new RetryConfig(2, Duration.ofMillis(1), Duration.ofMillis(2)));
        assertEquals("s1", retrying.getSandbox("s1").id);
        assertEquals(2, getCalls.get());
        assertThrows(BoxxkiteException.class, () -> retrying.exec("s1", "true"));
        assertEquals(1, postCalls.get());
    }

    @Test void retries429ForAsyncPostWhenServerRejectsIt() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        server = server(exchange -> {
            if (calls.incrementAndGet() == 1) respond(exchange, 429, "{}");
            else respond(exchange, 200, "{\"exit_code\":0,\"stdout\":\"\",\"stderr\":\"\"}");
        });
        Client retrying = new Client(url(), "test-key", Client.DEFAULT_TIMEOUT,
                new RetryConfig(1, Duration.ofMillis(1), Duration.ofMillis(2)));
        CompletableFuture<Models.ExecResult> result = retrying.execAsync("s1", "true");
        assertEquals(0, result.join().exitCode);
        assertEquals(2, calls.get());
    }

    @Test void usesExistingFileAndProcessRoutes() throws Exception {
        server = server(exchange -> {
            String path = exchange.getRequestURI().getRawPath();
            switch (path) {
                case "/v1/sandboxes":
                    assertEquals("POST", exchange.getRequestMethod());
                    assertTrue(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8).contains("java"));
                    respond(exchange, 200, "{\"id\":\"s1\",\"status\":\"running\"}");
                    break;
                case "/v1/sandboxes/s1/files":
                    respond(exchange, 200, "{\"path\":\"note.txt\",\"size\":2,\"created\":true}");
                    break;
                case "/v1/sandboxes/s1/files/view":
                    respond(exchange, 200, "{\"content\":\"ok\",\"lines\":1}");
                    break;
                case "/v1/sandboxes/s1/processes":
                    if (exchange.getRequestMethod().equals("POST")) respond(exchange, 200, "{\"process_id\":\"p1\",\"status\":\"running\"}");
                    else respond(exchange, 200, "{\"processes\":[]}");
                    break;
                case "/v1/sandboxes/s1/processes/p1/output":
                    assertEquals("since_offset=4", exchange.getRequestURI().getRawQuery());
                    respond(exchange, 200, "{\"status\":\"running\",\"stdout_chunk\":\"x\",\"next_offset\":5}");
                    break;
                case "/v1/sandboxes/s1/processes/p1/input":
                    respond(exchange, 200, "{\"bytes_written\":1}");
                    break;
                case "/v1/sandboxes/s1/processes/p1/stop":
                    assertEquals("POST", exchange.getRequestMethod());
                    respond(exchange, 200, "{\"status\":\"stopped\"}");
                    break;
                default: fail("unexpected route " + path);
            }
        });
        Client client = client();
        assertEquals("s1", client.createSandbox(new CreateSandboxRequest("java")).id);
        assertTrue(client.fileCreate("s1", "note.txt", "ok").created);
        assertEquals("ok", client.view("s1", "note.txt").content);
        assertEquals("p1", client.startProcess("s1", "sleep 1").processId);
        assertEquals(0, client.listProcesses("s1").processes.size());
        assertEquals(5, client.getProcessOutput("s1", "p1", 4).nextOffset);
        assertEquals(1, client.sendProcessInput("s1", "p1", "x").bytesWritten);
        assertEquals("stopped", client.stopProcess("s1", "p1").status);
    }

    @Test void malformedApiErrorBodyUsesSafeFallback() throws Exception {
        server = server(exchange -> respond(exchange, 502, ""));
        BoxxkiteException error = assertThrows(BoxxkiteException.class, () -> client().getSandbox("s1"));
        assertEquals(502, error.statusCode());
        assertEquals("error", error.code());
        assertEquals("HTTP 502", error.getMessage());
    }

    @Test void batchCreationDecodesArrayForSyncAndAsyncCalls() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        server = server(exchange -> {
            assertEquals("POST", exchange.getRequestMethod());
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            assertTrue(body.contains("\"count\":2"));
            calls.incrementAndGet();
            respond(exchange, 200, "[{\"id\":\"s1\",\"status\":\"active\"},{\"id\":\"s2\",\"status\":\"active\"}]");
        });
        Client client = client();
        CreateSandboxRequest request = new CreateSandboxRequest("batch");
        request.count = 2;
        assertArrayEquals(new String[]{"s1", "s2"}, ids(client.createSandboxBatch(request)));
        assertArrayEquals(new String[]{"s1", "s2"}, ids(client.createSandboxBatchAsync(request).join()));
        assertEquals(2, calls.get());

        request.count = 1;
        assertThrows(IllegalArgumentException.class, () -> client.createSandboxBatch(request));
        request.count = 2;
        assertThrows(IllegalArgumentException.class, () -> client.createSandbox(request));
        assertThrows(IllegalArgumentException.class, () -> client.createSandboxAsync(request));
        assertEquals(2, calls.get());
    }

    private static String[] ids(Sandbox[] sandboxes) {
        return java.util.Arrays.stream(sandboxes).map(sandbox -> sandbox.id).toArray(String[]::new);
    }

    private Client client() { return new Client(url(), "test-key"); }
    private String url() { return "http://localhost:" + server.getAddress().getPort(); }

    private static HttpServer server(Handler handler) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> handler.handle(exchange));
        server.start();
        return server;
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    @FunctionalInterface private interface Handler { void handle(HttpExchange exchange) throws IOException; }
}
