using System.Net;
using System.Net.Sockets;
using System.Text;
using Boxxkite;
using Xunit;

namespace Boxxkite.Client.Tests;

public sealed class BoxxkiteClientTests
{
    [Fact]
    public async Task LifecycleAndExecUseExpectedRoutesAndDecodeResponses()
    {
        await using var server = new LocalServer(async context =>
        {
            Assert.Equal("Bearer test-key", context.Request.Headers["Authorization"]);
            switch (context.Request.HttpMethod, context.Request.RawUrl)
            {
                case ("POST", "/v1/sandboxes"):
                    var createBody = await ReadBody(context);
                    Assert.Contains("\"label\":\"sdk-test\"", createBody);
                    Assert.DoesNotContain("\"count\":null", createBody);
                    Assert.DoesNotContain("\"size\":null", createBody);
                    await WriteJson(context, """
                        {"id":"sb-1","status":"active","label":"sdk-test","created_at":"2026-10-01T00:00:00Z","expires_at":"2026-10-02T00:00:00Z"}
                        """);
                    break;
                case ("GET", "/v1/sandboxes/sb-1"):
                    await WriteJson(context, """
                        {"id":"sb-1","status":"active","label":"sdk-test","created_at":"2026-10-01T00:00:00Z","expires_at":"2026-10-02T00:00:00Z"}
                        """);
                    break;
                case ("POST", "/v1/sandboxes/sb-1/exec"):
                    Assert.Contains("\"command\":\"echo hi\"", await ReadBody(context));
                    await WriteJson(context, """
                        {"exit_code":0,"stdout":"hi\n","stderr":""}
                        """);
                    break;
                case ("DELETE", "/v1/sandboxes/sb-1"):
                    context.Response.StatusCode = (int)HttpStatusCode.NoContent;
                    context.Response.Close();
                    break;
                default:
                    context.Response.StatusCode = (int)HttpStatusCode.NotFound;
                    context.Response.Close();
                    break;
            }
        });
        using var client = new BoxxkiteClient(server.BaseUri, "test-key");

        var created = await client.CreateSandboxAsync(new CreateSandboxRequest { Label = "sdk-test" });
        var found = await client.GetSandboxAsync(created.Id);
        var execution = await client.ExecAsync(found.Id, "echo hi");
        await client.DeleteSandboxAsync(found.Id);

        Assert.Equal("sb-1", created.Id);
        Assert.Equal(0, execution.ExitCode);
        Assert.Equal("hi\n", execution.Stdout);
        Assert.Equal(4, server.RequestCount);
    }

    [Fact]
    public async Task ApiErrorsPreserveStructuredFields()
    {
        await using var server = new LocalServer(async context =>
        {
            context.Response.StatusCode = (int)HttpStatusCode.TooManyRequests;
            await WriteJson(context, """
                {"error":{"code":"concurrent_sandbox_limit_reached","message":"Limit reached","retryable":true,"remediation":"delete one","details":{"limit":2}}}
                """);
        });
        using var client = new BoxxkiteClient(server.BaseUri, "test-key");

        var error = await Assert.ThrowsAsync<BoxxkiteApiException>(() => client.CreateSandboxAsync());

        Assert.Equal(429, error.StatusCode);
        Assert.Equal("concurrent_sandbox_limit_reached", error.Code);
        Assert.True(error.Retryable);
        Assert.Equal("delete one", error.Remediation);
        Assert.Equal(2, error.Details?.GetProperty("limit").GetInt32());
    }

    [Fact]
    public void RejectsInsecureNonLoopbackAndAllowsLocalHttp()
    {
        Assert.Throws<ArgumentException>(() => new BoxxkiteClient(new Uri("http://example.com"), "secret"));
        using var client = new BoxxkiteClient(new Uri("http://127.0.0.1:8000"), "test-key");
    }

    [Fact]
    public async Task CancellationTokenCancelsInFlightRequest()
    {
        var requestAccepted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var server = new LocalServer(async context =>
        {
            requestAccepted.SetResult();
            await Task.Delay(TimeSpan.FromMilliseconds(250));
            try
            {
                context.Response.Close();
            }
            catch (HttpListenerException)
            {
            }
        });
        using var client = new BoxxkiteClient(server.BaseUri, "test-key");
        using var cancellation = new CancellationTokenSource();
        var call = client.GetSandboxAsync("sb-1", cancellation.Token);
        await requestAccepted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => call);
    }

    [Fact]
    public async Task SessionIdIsEscapedAsOnePathSegment()
    {
        await using var server = new LocalServer(async context =>
        {
            Assert.Equal("/v1/sandboxes/id%2Fpart", context.Request.RawUrl);
            await WriteJson(context, """
                {"id":"id/part","status":"active","created_at":"2026-10-01T00:00:00Z","expires_at":"2026-10-02T00:00:00Z"}
                """);
        });
        using var client = new BoxxkiteClient(server.BaseUri, "test-key");

        var result = await client.GetSandboxAsync("id/part");

        Assert.Equal("id/part", result.Id);
    }

    private static async Task<string> ReadBody(HttpListenerContext context)
    {
        using var reader = new StreamReader(context.Request.InputStream, context.Request.ContentEncoding);
        return await reader.ReadToEndAsync();
    }

    private static async Task WriteJson(HttpListenerContext context, string json)
    {
        context.Response.ContentType = "application/json";
        var bytes = Encoding.UTF8.GetBytes(json);
        context.Response.ContentLength64 = bytes.Length;
        await context.Response.OutputStream.WriteAsync(bytes);
        context.Response.Close();
    }

    private sealed class LocalServer : IAsyncDisposable
    {
        private readonly HttpListener _listener = new();
        private readonly Task _loop;
        private readonly Func<HttpListenerContext, Task> _handler;
        private int _requestCount;

        public LocalServer(Func<HttpListenerContext, Task> handler)
        {
            _handler = handler;
            var port = GetFreePort();
            BaseUri = new Uri($"http://127.0.0.1:{port}/");
            _listener.Prefixes.Add(BaseUri.AbsoluteUri);
            _listener.Start();
            _loop = Task.Run(ListenAsync);
        }

        public Uri BaseUri { get; }
        public int RequestCount => _requestCount;

        private async Task ListenAsync()
        {
            while (_listener.IsListening)
            {
                HttpListenerContext context;
                try
                {
                    context = await _listener.GetContextAsync();
                }
                catch (HttpListenerException)
                {
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }

                Interlocked.Increment(ref _requestCount);
                await _handler(context);
            }
        }

        public async ValueTask DisposeAsync()
        {
            _listener.Close();
            try
            {
                await _loop.WaitAsync(TimeSpan.FromSeconds(2));
            }
            catch (TimeoutException)
            {
            }
        }

        private static int GetFreePort()
        {
            using var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
    }
}
