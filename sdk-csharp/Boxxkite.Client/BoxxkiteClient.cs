using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Boxxkite;

public sealed class BoxxkiteClient : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };
    private readonly HttpClient _httpClient;
    private readonly bool _ownsHttpClient;
    private readonly Uri _baseUri;
    private readonly string _apiKey;
    private readonly TimeSpan _timeout;
    private bool _disposed;

    public BoxxkiteClient(
        Uri baseUri,
        string apiKey,
        HttpClient? httpClient = null,
        TimeSpan? timeout = null)
    {
        ArgumentNullException.ThrowIfNull(baseUri);
        ArgumentException.ThrowIfNullOrWhiteSpace(apiKey);
        ValidateBaseUri(baseUri);

        _baseUri = new Uri(baseUri.AbsoluteUri.TrimEnd('/') + "/", UriKind.Absolute);
        _apiKey = apiKey;
        _timeout = timeout ?? TimeSpan.FromSeconds(30);
        if (_timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout), "Timeout must be positive.");
        }

        _ownsHttpClient = httpClient is null;
        _httpClient = httpClient ?? new HttpClient();
        if (_ownsHttpClient)
        {
            _httpClient.Timeout = Timeout.InfiniteTimeSpan;
        }
    }

    public async Task<Sandbox> CreateSandboxAsync(
        CreateSandboxRequest? request = null,
        CancellationToken cancellationToken = default)
    {
        request ??= new CreateSandboxRequest();
        if (request.Count is not null and not 1)
        {
            throw new ArgumentException("CreateSandboxAsync supports exactly one sandbox; Count must be 1 or null.", nameof(request));
        }

        return await SendAsync<Sandbox>(HttpMethod.Post, "v1/sandboxes", request, cancellationToken)
            .ConfigureAwait(false);
    }

    public Task<Sandbox> GetSandboxAsync(string sessionId, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionId);
        return SendAsync<Sandbox>(HttpMethod.Get, $"v1/sandboxes/{EscapePathSegment(sessionId)}", null, cancellationToken);
    }

    public async Task DeleteSandboxAsync(string sessionId, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionId);
        await SendAsync<object>(HttpMethod.Delete, $"v1/sandboxes/{EscapePathSegment(sessionId)}", null, cancellationToken)
            .ConfigureAwait(false);
    }

    public Task<ExecResult> ExecAsync(
        string sessionId,
        string command,
        ExecOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionId);
        ArgumentException.ThrowIfNullOrWhiteSpace(command);
        options ??= new ExecOptions();
        if (options.Timeout is < 1 or > 100)
        {
            throw new ArgumentOutOfRangeException(nameof(options), "Exec timeout must be between 1 and 100 seconds.");
        }

        var body = new ExecRequest(command, options.Timeout, options.Description);
        var requestTimeout = options.Timeout is int seconds
            ? TimeSpan.FromSeconds(seconds + 15)
            : _timeout;
        return SendAsync<ExecResult>(
            HttpMethod.Post,
            $"v1/sandboxes/{EscapePathSegment(sessionId)}/exec",
            body,
            cancellationToken,
            requestTimeout);
    }

    private async Task<T> SendAsync<T>(
        HttpMethod method,
        string path,
        object? body,
        CancellationToken cancellationToken,
        TimeSpan? requestTimeout = null)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeoutSource.CancelAfter(requestTimeout ?? _timeout);
        using var request = new HttpRequestMessage(method, new Uri(_baseUri, path));
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _apiKey);
        if (body is not null)
        {
            request.Content = new StringContent(JsonSerializer.Serialize(body, JsonOptions), Encoding.UTF8, "application/json");
        }

        HttpResponseMessage response;
        try
        {
            response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseContentRead, timeoutSource.Token)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new BoxxkiteConnectionException("The Boxxkite request timed out.", new TimeoutException());
        }
        catch (HttpRequestException exception)
        {
            throw new BoxxkiteConnectionException("Could not connect to the Boxxkite control-plane.", exception);
        }

        using (response)
        {
            string payload;
            try
            {
                payload = await response.Content.ReadAsStringAsync(timeoutSource.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                throw new BoxxkiteConnectionException("The Boxxkite request timed out.", new TimeoutException());
            }
            if (!response.IsSuccessStatusCode)
            {
                throw CreateApiException(response.StatusCode, payload);
            }

            if (typeof(T) == typeof(object) && response.StatusCode == HttpStatusCode.NoContent)
            {
                return (T)(object)new object();
            }

            try
            {
                return JsonSerializer.Deserialize<T>(payload, JsonOptions)
                    ?? throw new JsonException("The response body was empty.");
            }
            catch (JsonException exception)
            {
                throw new BoxxkiteConnectionException("The Boxxkite control-plane returned an invalid response.", exception);
            }
        }
    }

    private static BoxxkiteApiException CreateApiException(HttpStatusCode statusCode, string payload)
    {
        try
        {
            var envelope = JsonSerializer.Deserialize<ApiErrorEnvelope>(payload, JsonOptions)?.Error;
            if (envelope is not null)
            {
                return new BoxxkiteApiException(
                    (int)statusCode,
                    envelope.Code ?? "http_error",
                    envelope.Message ?? $"Request failed with HTTP {(int)statusCode}.",
                    envelope.Retryable,
                    envelope.Remediation,
                    envelope.Details);
            }
        }
        catch (JsonException)
        {
        }

        return new BoxxkiteApiException((int)statusCode, "http_error", $"Request failed with HTTP {(int)statusCode}.");
    }

    private static string EscapePathSegment(string value) => Uri.EscapeDataString(value);

    private static void ValidateBaseUri(Uri uri)
    {
        if (!uri.IsAbsoluteUri || !string.IsNullOrEmpty(uri.UserInfo) || uri.Query.Length > 0 || uri.Fragment.Length > 0)
        {
            throw new ArgumentException("Base URI must be absolute and cannot include credentials, query, or fragment.", nameof(uri));
        }

        if (uri.Scheme == Uri.UriSchemeHttps)
        {
            return;
        }

        if (uri.Scheme == Uri.UriSchemeHttp && uri.IsLoopback)
        {
            return;
        }

        throw new ArgumentException("Refusing non-HTTPS base URI; HTTP is permitted only for loopback development.", nameof(uri));
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (_ownsHttpClient)
        {
            _httpClient.Dispose();
        }
    }

    private sealed record ExecRequest(
        string Command,
        [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Timeout,
        [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Description);

    private sealed class ApiErrorEnvelope
    {
        public ApiError? Error { get; init; }
    }

    private sealed class ApiError
    {
        public string? Code { get; init; }
        public string? Message { get; init; }
        public bool Retryable { get; init; }
        public string? Remediation { get; init; }
        public JsonElement? Details { get; init; }
    }
}
