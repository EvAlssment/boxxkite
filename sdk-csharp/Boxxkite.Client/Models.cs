using System.Text.Json;
using System.Text.Json.Serialization;

namespace Boxxkite;

public sealed record CreateSandboxRequest
{
    public string? Label { get; init; }
    public string? Size { get; init; }
    [JsonPropertyName("storage_gb")]
    public double? StorageGb { get; init; }
    [JsonPropertyName("lifetime_minutes")]
    public int? LifetimeMinutes { get; init; }
    public int? Count { get; init; }
}

public sealed record Sandbox
{
    public required string Id { get; init; }
    public required string Status { get; init; }
    public string? Label { get; init; }
    [JsonPropertyName("created_at")]
    public required DateTimeOffset CreatedAt { get; init; }
    [JsonPropertyName("destroyed_at")]
    public DateTimeOffset? DestroyedAt { get; init; }
    [JsonPropertyName("expires_at")]
    public required DateTimeOffset ExpiresAt { get; init; }
}

public sealed record ExecOptions
{
    public int? Timeout { get; init; }
    public string? Description { get; init; }
}

public sealed record ExecResult
{
    [JsonPropertyName("exit_code")]
    public required int ExitCode { get; init; }
    public required string Stdout { get; init; }
    public required string Stderr { get; init; }
}

public sealed class BoxxkiteApiException(
    int statusCode,
    string code,
    string message,
    bool retryable = false,
    string? remediation = null,
    JsonElement? details = null) : Exception($"{message} [{code}] (HTTP {statusCode})")
{
    public int StatusCode { get; } = statusCode;
    public string Code { get; } = code;
    public bool Retryable { get; } = retryable;
    public string? Remediation { get; } = remediation;
    public JsonElement? Details { get; } = details;
}

public sealed class BoxxkiteConnectionException(string message, Exception innerException)
    : Exception(message, innerException);
