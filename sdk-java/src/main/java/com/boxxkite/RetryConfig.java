package com.boxxkite;

import java.time.Duration;
import java.util.Objects;

/** Opt-in exponential retry policy. Only retryable methods/statuses are repeated. */
public final class RetryConfig {
    public static final RetryConfig DISABLED = new RetryConfig(0, Duration.ofMillis(500), Duration.ofSeconds(30));

    private final int maxRetries;
    private final Duration baseDelay;
    private final Duration maxDelay;

    public RetryConfig(int maxRetries, Duration baseDelay, Duration maxDelay) {
        if (maxRetries < 0) throw new IllegalArgumentException("maxRetries must not be negative");
        this.baseDelay = positive(baseDelay, "baseDelay");
        this.maxDelay = positive(maxDelay, "maxDelay");
        if (this.maxDelay.compareTo(this.baseDelay) < 0) throw new IllegalArgumentException("maxDelay must be at least baseDelay");
        this.maxRetries = maxRetries;
    }

    public static RetryConfig ofMaxRetries(int maxRetries) {
        return new RetryConfig(maxRetries, Duration.ofMillis(500), Duration.ofSeconds(30));
    }

    public int maxRetries() { return maxRetries; }
    Duration baseDelay() { return baseDelay; }
    Duration maxDelay() { return maxDelay; }

    private static Duration positive(Duration value, String name) {
        Objects.requireNonNull(value, name);
        if (value.isNegative() || value.isZero()) throw new IllegalArgumentException(name + " must be positive");
        return value;
    }
}
