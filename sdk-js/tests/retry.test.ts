import assert from "node:assert/strict";
import { test } from "node:test";

import {
  computeBackoffMs,
  isIdempotentMethod,
  isRetriableStatus,
  parseRetryAfter,
  resolveRetryOptions,
} from "../src/retry.js";

test("resolveRetryOptions leaves retries disabled when no options given", () => {
  const resolved = resolveRetryOptions(undefined);
  assert.equal(resolved.maxRetries, 0);
});

test("resolveRetryOptions enables retries with defaults for an empty object", () => {
  const resolved = resolveRetryOptions({});
  assert.equal(resolved.maxRetries, 2);
  assert.equal(resolved.initialDelayMs, 500);
  assert.equal(resolved.maxDelayMs, 30_000);
  assert.equal(resolved.backoffFactor, 2);
  assert.equal(resolved.respectRetryAfter, true);
});

test("resolveRetryOptions honors an explicit maxRetries of 0", () => {
  assert.equal(resolveRetryOptions({ maxRetries: 0 }).maxRetries, 0);
});

test("resolveRetryOptions overrides individual knobs", () => {
  const resolved = resolveRetryOptions({
    maxRetries: 5,
    initialDelayMs: 10,
    maxDelayMs: 100,
    backoffFactor: 3,
    respectRetryAfter: false,
  });
  assert.deepEqual(resolved, {
    maxRetries: 5,
    initialDelayMs: 10,
    maxDelayMs: 100,
    backoffFactor: 3,
    respectRetryAfter: false,
  });
});

test("isIdempotentMethod classifies methods case-insensitively", () => {
  for (const m of ["GET", "get", "HEAD", "OPTIONS", "PUT", "DELETE", "delete"]) {
    assert.equal(isIdempotentMethod(m), true, m);
  }
  for (const m of ["POST", "post", "PATCH"]) {
    assert.equal(isIdempotentMethod(m), false, m);
  }
});

test("isRetriableStatus only retries 429 and 5xx", () => {
  assert.equal(isRetriableStatus(429), true);
  assert.equal(isRetriableStatus(500), true);
  assert.equal(isRetriableStatus(503), true);
  assert.equal(isRetriableStatus(400), false);
  assert.equal(isRetriableStatus(404), false);
  assert.equal(isRetriableStatus(200), false);
});

test("parseRetryAfter parses delta-seconds", () => {
  assert.equal(parseRetryAfter("5"), 5000);
  assert.equal(parseRetryAfter("0"), 0);
});

test("parseRetryAfter parses an HTTP-date relative to now", () => {
  const now = Date.parse("2026-01-01T00:00:00Z");
  const tenSecondsLater = new Date(now + 10_000).toUTCString();
  assert.equal(parseRetryAfter(tenSecondsLater, now), 10_000);
});

test("parseRetryAfter clamps a past date to 0", () => {
  const now = Date.parse("2026-01-01T00:00:10Z");
  const earlier = new Date(now - 10_000).toUTCString();
  assert.equal(parseRetryAfter(earlier, now), 0);
});

test("parseRetryAfter returns null for absent or unparseable values", () => {
  assert.equal(parseRetryAfter(null), null);
  assert.equal(parseRetryAfter(""), null);
  assert.equal(parseRetryAfter("not-a-date"), null);
});

test("computeBackoffMs grows exponentially and stays within [half, full] of the cap", () => {
  const opts = resolveRetryOptions({ initialDelayMs: 100, maxDelayMs: 10_000, backoffFactor: 2 });
  for (const attempt of [0, 1, 2]) {
    const nominal = Math.min(100 * 2 ** attempt, 10_000);
    const delay = computeBackoffMs(attempt, opts);
    assert.ok(delay >= nominal / 2, `attempt ${attempt}: ${delay} >= ${nominal / 2}`);
    assert.ok(delay <= nominal, `attempt ${attempt}: ${delay} <= ${nominal}`);
  }
});

test("computeBackoffMs never exceeds the max delay cap", () => {
  const opts = resolveRetryOptions({ initialDelayMs: 1000, maxDelayMs: 2000, backoffFactor: 10 });
  assert.ok(computeBackoffMs(5, opts) <= 2000);
});
