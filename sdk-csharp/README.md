# Boxxkite .NET client

This is a foundational .NET 8 client for the Boxxkite control-plane HTTP API.
It currently supports creating one sandbox, looking up a sandbox by ID,
deleting a sandbox, and synchronous command execution.

## Build and test

```bash
dotnet build Boxxkite.Client.sln --configuration Release
dotnet test Boxxkite.Client.sln --configuration Release
```

## Example

```csharp
using Boxxkite;

var baseUrl = Environment.GetEnvironmentVariable("BOXXKITE_BASE_URL")
    ?? throw new InvalidOperationException("Set BOXXKITE_BASE_URL.");
var apiKey = Environment.GetEnvironmentVariable("BOXXKITE_API_KEY")
    ?? throw new InvalidOperationException("Set BOXXKITE_API_KEY.");

using var client = new BoxxkiteClient(new Uri(baseUrl), apiKey);
using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(2));

var sandbox = await client.CreateSandboxAsync(
    new CreateSandboxRequest { Label = "dotnet-example", Size = "small" },
    cancellation.Token);

try
{
    var result = await client.ExecAsync(
        sandbox.Id,
        "python3 -c 'print(1 + 1)'",
        cancellationToken: cancellation.Token);
    Console.WriteLine($"exit={result.ExitCode}: {result.Stdout}");
}
finally
{
    await client.DeleteSandboxAsync(sandbox.Id, CancellationToken.None);
}
```

The API key is a full-privilege credential. Keep it in server-side secret
storage and never embed it in browser applications, source control, or logs.
The client rejects plain HTTP except for loopback development URLs. It owns
and disposes its `HttpClient` only when one was not supplied by the caller;
dispose the client with `using` in either case.

`BoxxkiteApiException` preserves HTTP status, API error code, message,
retryability, remediation, and details. Request cancellation propagates as
`OperationCanceledException`; a client-side timeout is reported as a
`BoxxkiteConnectionException`.

This slice does not yet provide batch creation, files, processes, streaming,
WebSockets/takeover, previews, account resources, retries, or Semantic Kernel
adapters. It is not published to NuGet and makes no package-availability
claim. The scope is intentionally smaller than the existing SDKs.
