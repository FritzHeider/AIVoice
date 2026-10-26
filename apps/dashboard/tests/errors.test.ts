// @vitest-environment node
import { describe, expect, it } from "vitest";

import { ApiError, toApiError, UNKNOWN_ERROR_CODE } from "@/lib/api/errors";

describe("toApiError", () => {
  it("maps the envelope body", () => {
    const error = toApiError(
      422,
      {
        data: null,
        errors: [{ error: "INVALID_INPUT", message: "must be E.164", field: "phone_number" }],
        request_id: "req_1",
      },
      null,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      name: "ApiError",
      status: 422,
      code: "INVALID_INPUT",
      message: "must be E.164",
      field: "phone_number",
      requestId: "req_1",
    });
  });

  it("maps the legacy body and prefers the header request id", () => {
    const error = toApiError(
      404,
      {
        status: "error",
        message: "Call not found.",
        errors: [{ error: "NOT_FOUND", message: "Call not found.", field: null }],
        request_id: "req_body",
      },
      "req_header",
    );
    expect(error).toMatchObject({ code: "NOT_FOUND", field: undefined, requestId: "req_header" });
  });

  it("falls back to the top-level message", () => {
    expect(toApiError(400, { status: "error", message: "Bad.", errors: [] }, null)).toMatchObject({
      code: UNKNOWN_ERROR_CODE,
      message: "Bad.",
    });
  });

  it.each([[null], ["text"], [{ errors: "nope" }]])("tolerates body %j", (body) => {
    expect(toApiError(500, body, null)).toMatchObject({
      code: UNKNOWN_ERROR_CODE,
      message: "HTTP 500",
      requestId: undefined,
    });
  });
});
