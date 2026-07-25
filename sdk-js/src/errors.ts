/** Error types raised by BoxkiteClient. */

export class BoxkiteError extends Error {}

/** The control-plane could not be reached at all (DNS, TLS, timeout). */
export class BoxkiteConnectionError extends BoxkiteError {
  constructor(message: string) {
    super(message);
    this.name = "BoxkiteConnectionError";
  }
}

/** The control-plane responded with an error envelope
 * (`{"error": {code, message}}`), e.g. a 404, 401, or 429. */
export class BoxkiteApiError extends BoxkiteError {
  statusCode: number;
  code: string;
  message: string;

  constructor(statusCode: number, code: string, message: string) {
    super(`${message} [${code}] (HTTP ${statusCode})`);
    this.name = "BoxkiteApiError";
    this.statusCode = statusCode;
    this.code = code;
    this.message = message;
  }
}
