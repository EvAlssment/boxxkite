using Boxxkite;

var baseUrl = Environment.GetEnvironmentVariable("BOXXKITE_BASE_URL")
    ?? throw new InvalidOperationException("Set BOXXKITE_BASE_URL.");
var apiKey = Environment.GetEnvironmentVariable("BOXXKITE_API_KEY")
    ?? throw new InvalidOperationException("Set BOXXKITE_API_KEY.");

using var client = new BoxxkiteClient(new Uri(baseUrl), apiKey);
using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(2));
var sandbox = await client.CreateSandboxAsync(new CreateSandboxRequest { Label = "dotnet-quickstart" }, cancellation.Token);

try
{
    var result = await client.ExecAsync(sandbox.Id, "python3 -c 'print(1 + 1)'", cancellationToken: cancellation.Token);
    Console.WriteLine($"exit={result.ExitCode}: {result.Stdout}");
}
finally
{
    await client.DeleteSandboxAsync(sandbox.Id, CancellationToken.None);
}
