package com.boxxkite;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;

import static com.boxxkite.Models.*;

/** Synchronous and asynchronous client for the Boxxkite control-plane v1 API. */
public final class Client {
    public static final Duration DEFAULT_TIMEOUT = Duration.ofSeconds(30);
    private static final ObjectMapper JSON = new ObjectMapper()
            .setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);

    private final URI baseUri;
    private final String apiKey;
    private final HttpClient http;
    private final Duration timeout;
    private final RetryConfig retry;

    public Client(String baseUrl, String apiKey) {
        this(baseUrl, apiKey, DEFAULT_TIMEOUT, RetryConfig.DISABLED, HttpClient.newHttpClient());
    }

    public Client(String baseUrl, String apiKey, Duration timeout, RetryConfig retry) {
        this(baseUrl, apiKey, timeout, retry, HttpClient.newHttpClient());
    }

    public Client(String baseUrl, String apiKey, Duration timeout, RetryConfig retry, HttpClient httpClient) {
        if (apiKey == null || apiKey.isBlank()) throw new IllegalArgumentException("apiKey must not be blank");
        if (timeout == null || timeout.isZero() || timeout.isNegative()) throw new IllegalArgumentException("timeout must be positive");
        this.baseUri = validateBaseUrl(baseUrl);
        this.apiKey = apiKey;
        this.timeout = timeout;
        this.retry = retry == null ? RetryConfig.DISABLED : retry;
        this.http = httpClient == null ? HttpClient.newHttpClient() : httpClient;
    }

    /** Bind an explicitly supplied session ID; no session is created implicitly. */
    public Session session(String sessionId) {
        requireId(sessionId, "sessionId");
        return new Session(this, sessionId);
    }

    public Sandbox createSandbox(CreateSandboxRequest request) {
        return send("POST", "/v1/sandboxes", requireSingle(request), Sandbox.class);
    }
    public CompletableFuture<Sandbox> createSandboxAsync(CreateSandboxRequest request) {
        return sendAsync("POST", "/v1/sandboxes", requireSingle(request), Sandbox.class);
    }

    /** Create multiple sandboxes. The request count must be greater than one. */
    public Sandbox[] createSandboxBatch(CreateSandboxRequest request) {
        return send("POST", "/v1/sandboxes", requireBatch(request), Sandbox[].class);
    }

    /** Async batch form; the request count must be greater than one. */
    public CompletableFuture<Sandbox[]> createSandboxBatchAsync(CreateSandboxRequest request) {
        return sendAsync("POST", "/v1/sandboxes", requireBatch(request), Sandbox[].class);
    }

    public Sandbox getSandbox(String sessionId) { return send("GET", sessionPath(sessionId), null, Sandbox.class); }
    public CompletableFuture<Sandbox> getSandboxAsync(String sessionId) { return sendAsync("GET", sessionPath(sessionId), null, Sandbox.class); }

    public Sandbox[] listSandboxes(boolean activeOnly) {
        return send("GET", "/v1/sandboxes?active_only=" + activeOnly, null, Sandbox[].class);
    }
    public CompletableFuture<Sandbox[]> listSandboxesAsync(boolean activeOnly) {
        return sendAsync("GET", "/v1/sandboxes?active_only=" + activeOnly, null, Sandbox[].class);
    }

    public void destroySandbox(String sessionId) { send("DELETE", sessionPath(sessionId), null, Void.class); }
    public CompletableFuture<Void> destroySandboxAsync(String sessionId) { return sendAsync("DELETE", sessionPath(sessionId), null, Void.class); }

    public ExecResult exec(String sessionId, String command) { return exec(sessionId, command, null); }
    public ExecResult exec(String sessionId, String command, Integer timeoutSeconds) {
        requireText(command, "command");
        Map<String, Object> body = timeoutSeconds == null ? Map.of("command", command) : Map.of("command", command, "timeout", timeoutSeconds);
        return send("POST", sessionPath(sessionId) + "/exec", body, ExecResult.class, execRequestTimeout(timeoutSeconds));
    }
    public CompletableFuture<ExecResult> execAsync(String sessionId, String command) { return execAsync(sessionId, command, null); }
    public CompletableFuture<ExecResult> execAsync(String sessionId, String command, Integer timeoutSeconds) {
        requireText(command, "command");
        Map<String, Object> body = timeoutSeconds == null ? Map.of("command", command) : Map.of("command", command, "timeout", timeoutSeconds);
        return sendAsync("POST", sessionPath(sessionId) + "/exec", body, ExecResult.class, execRequestTimeout(timeoutSeconds));
    }

    public FileCreateResult fileCreate(String sessionId, String path, String content) {
        return send("POST", sessionPath(sessionId) + "/files", Map.of("path", path, "content", content), FileCreateResult.class);
    }
    public CompletableFuture<FileCreateResult> fileCreateAsync(String sessionId, String path, String content) {
        return sendAsync("POST", sessionPath(sessionId) + "/files", Map.of("path", path, "content", content), FileCreateResult.class);
    }

    public FileViewResult view(String sessionId, String path) {
        return send("POST", sessionPath(sessionId) + "/files/view", Map.of("path", path), FileViewResult.class);
    }
    public CompletableFuture<FileViewResult> viewAsync(String sessionId, String path) {
        return sendAsync("POST", sessionPath(sessionId) + "/files/view", Map.of("path", path), FileViewResult.class);
    }

    public ReplaceResult strReplace(String sessionId, String path, String oldText, String newText, boolean replaceAll) {
        return send("POST", sessionPath(sessionId) + "/files/str-replace",
                Map.of("path", path, "old_str", oldText, "new_str", newText, "replace_all", replaceAll), ReplaceResult.class);
    }
    public CompletableFuture<ReplaceResult> strReplaceAsync(String sessionId, String path, String oldText, String newText, boolean replaceAll) {
        return sendAsync("POST", sessionPath(sessionId) + "/files/str-replace",
                Map.of("path", path, "old_str", oldText, "new_str", newText, "replace_all", replaceAll), ReplaceResult.class);
    }

    public DirectoryListResult ls(String sessionId, String path) {
        return send("POST", sessionPath(sessionId) + "/files/ls", Map.of("path", path), DirectoryListResult.class);
    }
    public CompletableFuture<DirectoryListResult> lsAsync(String sessionId, String path) {
        return sendAsync("POST", sessionPath(sessionId) + "/files/ls", Map.of("path", path), DirectoryListResult.class);
    }

    public GlobResult glob(String sessionId, String pattern, String path) {
        return send("POST", sessionPath(sessionId) + "/files/glob", Map.of("pattern", pattern, "path", path), GlobResult.class);
    }
    public CompletableFuture<GlobResult> globAsync(String sessionId, String pattern, String path) {
        return sendAsync("POST", sessionPath(sessionId) + "/files/glob", Map.of("pattern", pattern, "path", path), GlobResult.class);
    }

    public GrepResult grep(String sessionId, String pattern, String path) {
        return send("POST", sessionPath(sessionId) + "/files/grep", Map.of("pattern", pattern, "path", path), GrepResult.class);
    }
    public CompletableFuture<GrepResult> grepAsync(String sessionId, String pattern, String path) {
        return sendAsync("POST", sessionPath(sessionId) + "/files/grep", Map.of("pattern", pattern, "path", path), GrepResult.class);
    }

    public ProcessStartResult startProcess(String sessionId, String command) {
        return send("POST", sessionPath(sessionId) + "/processes", Map.of("command", command, "max_runtime_seconds", 3600), ProcessStartResult.class);
    }
    public CompletableFuture<ProcessStartResult> startProcessAsync(String sessionId, String command) {
        return sendAsync("POST", sessionPath(sessionId) + "/processes", Map.of("command", command, "max_runtime_seconds", 3600), ProcessStartResult.class);
    }

    public ProcessListResult listProcesses(String sessionId) { return send("GET", sessionPath(sessionId) + "/processes", null, ProcessListResult.class); }
    public CompletableFuture<ProcessListResult> listProcessesAsync(String sessionId) { return sendAsync("GET", sessionPath(sessionId) + "/processes", null, ProcessListResult.class); }

    public ProcessOutputResult getProcessOutput(String sessionId, String processId, long sinceOffset) {
        return send("GET", processPath(sessionId, processId) + "/output?since_offset=" + sinceOffset, null, ProcessOutputResult.class);
    }
    public CompletableFuture<ProcessOutputResult> getProcessOutputAsync(String sessionId, String processId, long sinceOffset) {
        return sendAsync("GET", processPath(sessionId, processId) + "/output?since_offset=" + sinceOffset, null, ProcessOutputResult.class);
    }

    public ProcessInputResult sendProcessInput(String sessionId, String processId, String data) {
        return send("POST", processPath(sessionId, processId) + "/input", Map.of("data", data), ProcessInputResult.class);
    }
    public CompletableFuture<ProcessInputResult> sendProcessInputAsync(String sessionId, String processId, String data) {
        return sendAsync("POST", processPath(sessionId, processId) + "/input", Map.of("data", data), ProcessInputResult.class);
    }

    public ProcessStopResult stopProcess(String sessionId, String processId) {
        return send("POST", processPath(sessionId, processId) + "/stop", null, ProcessStopResult.class);
    }
    public CompletableFuture<ProcessStopResult> stopProcessAsync(String sessionId, String processId) {
        return sendAsync("POST", processPath(sessionId, processId) + "/stop", null, ProcessStopResult.class);
    }

    private <T> T send(String method, String path, Object body, Class<T> type) {
        return send(method, path, body, type, timeout);
    }

    private <T> T send(String method, String path, Object body, Class<T> type, Duration requestTimeout) {
        String payload = jsonBody(body);
        for (int attempt = 0; ; attempt++) {
            try {
                HttpResponse<String> response = http.send(request(method, path, payload, requestTimeout), HttpResponse.BodyHandlers.ofString());
                if (retryable(method, response.statusCode(), attempt)) {
                    pause(retryDelay(attempt, response.headers().firstValue("Retry-After").orElse(null)));
                    continue;
                }
                return decode(response.statusCode(), response.body(), type);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw BoxxkiteException.transport(e);
            } catch (IOException e) {
                if (retryable(method, 0, attempt)) { pause(retryDelay(attempt, null)); continue; }
                throw BoxxkiteException.transport(e);
            }
        }
    }

    private <T> CompletableFuture<T> sendAsync(String method, String path, Object body, Class<T> type) {
        return sendAsync(method, path, body, type, timeout);
    }

    private <T> CompletableFuture<T> sendAsync(String method, String path, Object body, Class<T> type, Duration requestTimeout) {
        return sendAsyncAttempt(method, path, jsonBody(body), type, requestTimeout, 0);
    }

    private <T> CompletableFuture<T> sendAsyncAttempt(String method, String path, String payload, Class<T> type, Duration requestTimeout, int attempt) {
        return http.sendAsync(request(method, path, payload, requestTimeout), HttpResponse.BodyHandlers.ofString())
                .handle((response, failure) -> {
                    Throwable cause = unwrap(failure);
                    if (cause != null) {
                        if (retryable(method, 0, attempt)) return delayed(method, path, payload, type, requestTimeout, attempt, retryDelay(attempt, null));
                        return Client.<T>failed(BoxxkiteException.transport(cause));
                    }
                    if (retryable(method, response.statusCode(), attempt)) {
                        return delayed(method, path, payload, type, requestTimeout, attempt,
                                retryDelay(attempt, response.headers().firstValue("Retry-After").orElse(null)));
                    }
                    try { return CompletableFuture.<T>completedFuture(decode(response.statusCode(), response.body(), type)); }
                    catch (RuntimeException e) { return Client.<T>failed(e); }
                }).thenCompose(future -> future);
    }

    private <T> CompletableFuture<T> delayed(String method, String path, String payload, Class<T> type,
            Duration requestTimeout, int attempt, Duration delay) {
        return CompletableFuture.supplyAsync(() -> null,
                CompletableFuture.delayedExecutor(delay.toMillis(), TimeUnit.MILLISECONDS))
                .thenCompose(ignored -> sendAsyncAttempt(method, path, payload, type, requestTimeout, attempt + 1));
    }

    private HttpRequest request(String method, String path, String payload, Duration requestTimeout) {
        HttpRequest.Builder builder = HttpRequest.newBuilder(baseUri.resolve(path)).timeout(requestTimeout)
                .header("Authorization", "Bearer " + apiKey).header("Accept", "application/json");
        if (payload == null) builder.method(method, HttpRequest.BodyPublishers.noBody());
        else builder.header("Content-Type", "application/json").method(method, HttpRequest.BodyPublishers.ofString(payload));
        return builder.build();
    }

    private static String jsonBody(Object body) {
        if (body == null) return null;
        try { return JSON.writeValueAsString(body); }
        catch (IOException e) { throw BoxxkiteException.decode(e); }
    }

    private Duration execRequestTimeout(Integer timeoutSeconds) {
        if (timeoutSeconds == null) return timeout;
        if (timeoutSeconds < 1) throw new IllegalArgumentException("timeoutSeconds must be at least 1");
        return Duration.ofSeconds(timeoutSeconds.longValue() + 15);
    }

    private static CreateSandboxRequest requireBatch(CreateSandboxRequest request) {
        Objects.requireNonNull(request, "request");
        if (request.count == null || request.count <= 1) {
            throw new IllegalArgumentException("batch creation requires request.count greater than 1");
        }
        return request;
    }

    private static CreateSandboxRequest requireSingle(CreateSandboxRequest request) {
        Objects.requireNonNull(request, "request");
        if (request.count != null && request.count != 1) {
            throw new IllegalArgumentException("single sandbox creation requires request.count to be omitted or 1; use createSandboxBatch for count greater than 1");
        }
        return request;
    }

    private static <T> T decode(int status, String body, Class<T> type) {
        if (status < 200 || status >= 300) throw apiError(status, body);
        if (type == Void.class || body == null || body.isEmpty()) return null;
        try { return JSON.readValue(body, type); }
        catch (IOException e) { throw BoxxkiteException.decode(e); }
    }

    private static BoxxkiteException apiError(int status, String body) {
        try {
            JsonNode root = body == null || body.isBlank() ? null : JSON.readTree(body);
            JsonNode error = root == null ? JSON.getNodeFactory().nullNode() : root.path("error");
            String code = error.path("code").asText("error");
            String message = error.path("message").asText("HTTP " + status);
            return BoxxkiteException.api(status, code, message, error.path("retryable").asBoolean(status >= 500),
                    error.path("remediation").asText(null));
        } catch (IOException e) {
            return BoxxkiteException.api(status, "error", "HTTP " + status, status >= 500, null);
        }
    }

    private boolean retryable(String method, int status, int attempt) {
        if (attempt >= retry.maxRetries()) return false;
        if (status == 429) return true;
        boolean safeMethod = "GET".equals(method) || "DELETE".equals(method) || "HEAD".equals(method) || "OPTIONS".equals(method);
        return safeMethod && (status == 0 || status == 500 || status == 502 || status == 503 || status == 504);
    }

    private Duration retryDelay(int attempt, String retryAfter) {
        if (retryAfter != null) {
            try { return Duration.ofSeconds(Math.min(Math.max(0, Long.parseLong(retryAfter.trim())), retry.maxDelay().toSeconds())); }
            catch (NumberFormatException ignored) {
                try {
                    Duration parsed = Duration.between(Instant.now(), ZonedDateTime.parse(retryAfter, DateTimeFormatter.RFC_1123_DATE_TIME).toInstant());
                    return parsed.isNegative() ? Duration.ZERO : parsed.compareTo(retry.maxDelay()) > 0 ? retry.maxDelay() : parsed;
                } catch (RuntimeException ignoredDate) { }
            }
        }
        long cap = Math.min(retry.maxDelay().toMillis(), retry.baseDelay().toMillis() * (1L << Math.min(attempt, 20)));
        return Duration.ofMillis(ThreadLocalRandom.current().nextLong(Math.max(1, cap + 1)));
    }

    private static void pause(Duration delay) {
        try { TimeUnit.NANOSECONDS.sleep(delay.toNanos()); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw BoxxkiteException.transport(e); }
    }

    private String sessionPath(String id) { requireId(id, "sessionId"); return "/v1/sandboxes/" + segment(id); }
    private String processPath(String sessionId, String processId) {
        requireId(processId, "processId"); return sessionPath(sessionId) + "/processes/" + segment(processId);
    }
    private static String segment(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20"); }
    private static void requireId(String value, String name) { requireText(value, name); }
    private static void requireText(String value, String name) { if (value == null || value.isBlank()) throw new IllegalArgumentException(name + " must not be blank"); }

    private static URI validateBaseUrl(String value) {
        requireText(value, "baseUrl");
        final URI uri;
        try { uri = URI.create(value); }
        catch (IllegalArgumentException e) { throw new IllegalArgumentException("baseUrl must be a valid HTTPS URL", e); }
        String host = uri.getHost();
        boolean loopback = host != null && (host.equalsIgnoreCase("localhost") || host.equals("127.0.0.1") || host.equals("::1") || host.equals("[::1]"));
        if (host == null || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null ||
                !("https".equalsIgnoreCase(uri.getScheme()) || ("http".equalsIgnoreCase(uri.getScheme()) && loopback))) {
            throw new IllegalArgumentException("baseUrl must use HTTPS (HTTP is allowed only for localhost development)");
        }
        String normalized = value.replaceAll("/+$", "");
        return URI.create(normalized + "/");
    }

    private static Throwable unwrap(Throwable failure) {
        while (failure instanceof CompletionException && failure.getCause() != null) failure = failure.getCause();
        return failure;
    }

    private static <T> CompletableFuture<T> failed(Throwable error) {
        CompletableFuture<T> future = new CompletableFuture<>(); future.completeExceptionally(error); return future;
    }
}
