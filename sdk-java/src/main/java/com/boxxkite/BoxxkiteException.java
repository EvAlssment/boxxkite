package com.boxxkite;

/** Typed API, transport, or response-decoding failure. */
public final class BoxxkiteException extends RuntimeException {
    private final int statusCode;
    private final String code;
    private final boolean retryable;
    private final String remediation;

    BoxxkiteException(String message, Throwable cause, int statusCode, String code, boolean retryable, String remediation) {
        super(message, cause);
        this.statusCode = statusCode;
        this.code = code;
        this.retryable = retryable;
        this.remediation = remediation;
    }

    static BoxxkiteException api(int status, String code, String message, boolean retryable, String remediation) {
        return new BoxxkiteException(message, null, status, code, retryable, remediation);
    }

    static BoxxkiteException transport(Throwable cause) {
        return new BoxxkiteException("Boxxkite request failed", cause, 0, "connection_error", true, null);
    }

    static BoxxkiteException decode(Throwable cause) {
        return new BoxxkiteException("Could not decode Boxxkite response", cause, 0, "decode_error", false, null);
    }

    public int statusCode() { return statusCode; }
    public String code() { return code; }
    public boolean retryable() { return retryable; }
    public String remediation() { return remediation; }
}
