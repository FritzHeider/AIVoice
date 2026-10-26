/**
 * `ApiError {code, message, field?, requestId}` (specs/25 §23) from either error body of specs/31 §1: the envelope
 * body `{data: null, errors: [...], request_id}` or the legacy body `{status: "error", message, errors, request_id}`.
 */
import { z } from "zod";

export const UNKNOWN_ERROR_CODE = "UNKNOWN_ERROR";

const errorItemSchema = z.object({
  error: z.string(),
  message: z.string(),
  field: z.string().nullish(),
});

const errorBodySchema = z.object({
  message: z.string().optional(),
  errors: z.array(errorItemSchema).optional(),
  request_id: z.string().optional(),
});

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly field: string | undefined;
  readonly requestId: string | undefined;

  constructor(
    status: number,
    init: {
      code: string;
      message: string;
      field?: string | undefined;
      requestId?: string | undefined;
    },
  ) {
    super(init.message);
    this.name = "ApiError";
    this.status = status;
    this.code = init.code;
    this.field = init.field;
    this.requestId = init.requestId;
  }
}

/** Map a non-2xx response body to `ApiError`; the `X-Request-Id` header wins over the body's `request_id`. */
export function toApiError(
  status: number,
  body: unknown,
  headerRequestId: string | null,
): ApiError {
  const parsed = errorBodySchema.safeParse(body);
  const data = parsed.success ? parsed.data : {};
  const item = data.errors?.[0];
  return new ApiError(status, {
    code: item?.error ?? UNKNOWN_ERROR_CODE,
    message: item?.message ?? data.message ?? `HTTP ${status}`,
    field: item?.field ?? undefined,
    requestId: headerRequestId ?? data.request_id,
  });
}
