/**
 * Opt-in retry policy for the HTTP layer: exponential backoff with jitter,
 * honoring a server `Retry-After` header, applied only to idempotent
 * requests that fail transiently (a connection error, HTTP 429, or 5xx).
 *
 * Retries are off by default -- a BoxkiteClient constructed without a
 * `retry` option behaves exactly as before (one attempt, no backoff).
 * Passing `retry: {}` enables it with the sensible defaults below; every
 * knob is individually overridable.
 */

export interface RetryOptions {
  /** Maximum retries after the initial attempt. Default 0 (disabled).
   * Passing any `retry` object without this field enables retries at the
   * default of 2. */
  maxRetries?: number;
  /** Base backoff delay in milliseconds (the first retry's nominal wait,
   * before jitter). Default 500. */
  initialDelayMs?: number;
  /** Upper bound on any single backoff wait, in milliseconds -- also caps
   * an over-long server `Retry-After`. Default 30_000. */
  maxDelayMs?: number;
  /** Per-attempt exponential multiplier. Default 2. */
  backoffFactor?: number;
  /** Honor a `Retry-After` response header (seconds or HTTP-date) when
   * present, in preference to computed backoff. Default true. */
  respectRetryAfter?: boolean;
}

export interface ResolvedRetryOptions {
  maxRetries: number;
  initialDelayMs: number;
  maxDelayMs: number;
  backoffFactor: number;
  respectRetryAfter: boolean;
}

const DEFAULT_MAX_RETRIES = 2;
const DEFAULT_INITIAL_DELAY_MS = 500;
const DEFAULT_MAX_DELAY_MS = 30_000;
const DEFAULT_BACKOFF_FACTOR = 2;

/** Methods safe to replay: a retried one cannot cause a duplicate
 * side effect the way a retried POST could. */
const IDEMPOTENT_METHODS = new Set(["GET", "HEAD", "OPTIONS", "PUT", "DELETE"]);

/** Undefined `retry` means retries stay disabled; an object (even `{}`)
 * opts in, filling any unset field from the defaults. */
export function resolveRetryOptions(options?: RetryOptions): ResolvedRetryOptions {
  return {
    maxRetries: options?.maxRetries ?? (options ? DEFAULT_MAX_RETRIES : 0),
    initialDelayMs: options?.initialDelayMs ?? DEFAULT_INITIAL_DELAY_MS,
    maxDelayMs: options?.maxDelayMs ?? DEFAULT_MAX_DELAY_MS,
    backoffFactor: options?.backoffFactor ?? DEFAULT_BACKOFF_FACTOR,
    respectRetryAfter: options?.respectRetryAfter ?? true,
  };
}

export function isIdempotentMethod(method: string): boolean {
  return IDEMPOTENT_METHODS.has(method.toUpperCase());
}

/** A response status worth retrying: rate-limited (429) or a server-side
 * failure (5xx). 4xx other than 429 are the caller's fault and won't
 * change on replay. */
export function isRetriableStatus(status: number): boolean {
  return status === 429 || status >= 500;
}

/**
 * Parse an HTTP `Retry-After` header into a delay in milliseconds, or null
 * if absent/unparseable. Accepts both forms the spec allows: a
 * delta-seconds integer, or an HTTP-date. A past date clamps to 0.
 */
export function parseRetryAfter(headerValue: string | null, now: number = Date.now()): number | null {
  if (headerValue === null) return null;
  const trimmed = headerValue.trim();
  if (trimmed === "") return null;
  if (/^\d+$/.test(trimmed)) return Number(trimmed) * 1000;
  const dateMs = Date.parse(trimmed);
  if (Number.isNaN(dateMs)) return null;
  return Math.max(0, dateMs - now);
}

/**
 * Backoff wait before the given retry (attempt 0 = first retry): exponential
 * growth capped at maxDelayMs, then equal jitter -- half the capped delay
 * plus a random 0..half -- so concurrent clients don't retry in lockstep.
 */
export function computeBackoffMs(attempt: number, opts: ResolvedRetryOptions): number {
  const raw = opts.initialDelayMs * Math.pow(opts.backoffFactor, attempt);
  const capped = Math.min(raw, opts.maxDelayMs);
  return capped / 2 + Math.random() * (capped / 2);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
