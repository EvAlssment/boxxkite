/** Error types raised by BoxxkiteClient. */

export class BoxxkiteError extends Error {}

/** The control-plane could not be reached at all (DNS, TLS, timeout). */
export class BoxxkiteConnectionError extends BoxxkiteError {
  constructor(message: string) {
    super(message);
    this.name = "BoxxkiteConnectionError";
  }
}

/** The control-plane responded with an error envelope
 * (`{"error": {code, message}}`), e.g. a 404, 401, or 429. */
export class BoxxkiteApiError extends BoxxkiteError {
  statusCode: number;
  code: string;
  message: string;
  retryable: boolean;
  remediation?: string;
  details?: unknown;

  constructor(statusCode: number, code: string, message: string, retryable = false, remediation?: string, details?: unknown) {
    super(`${message} [${code}] (HTTP ${statusCode})`);
    this.name = "BoxxkiteApiError";
    this.statusCode = statusCode;
    this.code = code;
    this.message = message;
    this.retryable = retryable;
    this.remediation = remediation;
    this.details = details;
  }
}

export class BoxxkiteQuotaExceededError extends BoxxkiteApiError {}
export class BoxxkiteEgressDeniedError extends BoxxkiteApiError {}
export class BoxxkiteSandboxNotReadyError extends BoxxkiteApiError {}
export class BoxxkiteCapabilityDeniedError extends BoxxkiteApiError {}
export class BoxxkiteReadonlyFilesystemError extends BoxxkiteApiError {}
export class BoxxkiteSandboxCrashedError extends BoxxkiteApiError {}
export class BoxxkiteServiceUnavailableError extends BoxxkiteApiError {}

export function apiErrorType(code: string, statusCode: number): typeof BoxxkiteApiError {
  if (code.endsWith("_limit_reached") || code.endsWith("_capacity_reached")) return BoxxkiteQuotaExceededError;
  const types: Record<string, typeof BoxxkiteApiError> = {
    egress_denied: BoxxkiteEgressDeniedError,
    capability_denied: BoxxkiteCapabilityDeniedError,
    command_not_allowed: BoxxkiteCapabilityDeniedError,
    readonly_filesystem: BoxxkiteReadonlyFilesystemError,
    sandbox_not_ready: BoxxkiteSandboxNotReadyError,
    sandbox_crashed: BoxxkiteSandboxCrashedError,
    service_unavailable: BoxxkiteServiceUnavailableError,
  };
  return types[code] ?? (statusCode >= 500 ? BoxxkiteServiceUnavailableError : BoxxkiteApiError);
}
