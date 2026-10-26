/**
 * Error taxonomy shared with the Python SDK (specs/26 §2.2): every failure is a `VoiceAgentsError`; HTTP failures are
 * `APIError` subclasses chosen by status code. Both error body shapes from specs/31 §1 are parsed: the envelope body
 * `{data: null, errors: [...], request_id}` and the legacy body `{status: "error", message, errors: [...], request_id}`.
 */

export class VoiceAgentsError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = new.target.name;
  }
}

export interface ParsedErrorBody {
  code: string;
  message: string;
  field: string | null;
  requestId: string | null;
}

const UNKNOWN_CODE = "UNKNOWN_ERROR";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function firstErrorItem(body: Record<string, unknown>): Record<string, unknown> | null {
  const errors = body.errors;
  if (Array.isArray(errors) && isRecord(errors[0])) {
    return errors[0];
  }
  return null;
}

/** Extract `{code, message, field, requestId}` from an error response body of either envelope. */
export function parseErrorBody(body: unknown, fallbackMessage: string): ParsedErrorBody {
  if (!isRecord(body)) {
    return { code: UNKNOWN_CODE, message: fallbackMessage, field: null, requestId: null };
  }
  const item = firstErrorItem(body);
  return {
    code: stringOrNull(item?.error) ?? UNKNOWN_CODE,
    message: stringOrNull(item?.message) ?? stringOrNull(body.message) ?? fallbackMessage,
    field: stringOrNull(item?.field),
    requestId: stringOrNull(body.request_id),
  };
}

export class APIError extends VoiceAgentsError {
  readonly status: number;
  readonly code: string;
  readonly field: string | null;
  readonly requestId: string | null;
  readonly body: unknown;

  constructor(status: number, parsed: ParsedErrorBody, body: unknown) {
    super(parsed.message);
    this.status = status;
    this.code = parsed.code;
    this.field = parsed.field;
    this.requestId = parsed.requestId;
    this.body = body;
  }
}

export class BadRequestError extends APIError {}
export class AuthenticationError extends APIError {}
export class InsufficientCreditsError extends APIError {}
export class PermissionDeniedError extends APIError {}
export class NotFoundError extends APIError {}
export class ConflictError extends APIError {}
export class RateLimitError extends APIError {}
export class InternalServerError extends APIError {}

export class APIConnectionError extends VoiceAgentsError {}
export class APITimeoutError extends APIConnectionError {}

type APIErrorClass = new (status: number, parsed: ParsedErrorBody, body: unknown) => APIError;

const STATUS_CLASSES: ReadonlyMap<number, APIErrorClass> = new Map<number, APIErrorClass>([
  [400, BadRequestError],
  [401, AuthenticationError],
  [402, InsufficientCreditsError],
  [403, PermissionDeniedError],
  [404, NotFoundError],
  [409, ConflictError],
  [429, RateLimitError],
]);

function errorClassFor(status: number): APIErrorClass {
  if (status >= 500) {
    return InternalServerError;
  }
  return STATUS_CLASSES.get(status) ?? APIError;
}

/**
 * Build the `APIError` subclass for a non-2xx response. `headerRequestId` (the `X-Request-Id` response header) wins
 * over the body field because every response carries the header, including bodies that are not JSON.
 */
export function makeAPIError(
  status: number,
  body: unknown,
  headerRequestId: string | null,
): APIError {
  const parsed = parseErrorBody(body, `HTTP ${status}`);
  const requestId = headerRequestId ?? parsed.requestId;
  return new (errorClassFor(status))(status, { ...parsed, requestId }, body);
}
