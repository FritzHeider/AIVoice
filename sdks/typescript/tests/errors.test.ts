import { describe, expect, it } from "vitest";

import {
  APIError,
  AuthenticationError,
  BadRequestError,
  ConflictError,
  InsufficientCreditsError,
  InternalServerError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  VoiceAgentsError,
} from "../src/index.js";
import { makeAPIError, parseErrorBody } from "../src/errors.js";

describe("makeAPIError", () => {
  it.each([
    [400, BadRequestError],
    [401, AuthenticationError],
    [402, InsufficientCreditsError],
    [403, PermissionDeniedError],
    [404, NotFoundError],
    [409, ConflictError],
    [429, RateLimitError],
    [500, InternalServerError],
    [503, InternalServerError],
    [418, APIError],
  ])("maps status %i", (status, cls) => {
    const error = makeAPIError(status, null, null);
    expect(error).toBeInstanceOf(cls);
    expect(error).toBeInstanceOf(APIError);
    expect(error).toBeInstanceOf(VoiceAgentsError);
    expect(error.name).toBe(cls.name);
    expect(error.status).toBe(status);
  });

  it("falls back to the body request id", () => {
    const error = makeAPIError(400, { request_id: "req_b", errors: [] }, null);
    expect(error.requestId).toBe("req_b");
  });
});

describe("parseErrorBody", () => {
  it("parses the envelope body", () => {
    expect(
      parseErrorBody(
        {
          data: null,
          errors: [{ error: "INVALID_INPUT", message: "must be E.164", field: "phone_number" }],
          request_id: "req_1",
        },
        "fallback",
      ),
    ).toEqual({
      code: "INVALID_INPUT",
      message: "must be E.164",
      field: "phone_number",
      requestId: "req_1",
    });
  });

  it("parses the legacy body", () => {
    expect(
      parseErrorBody(
        {
          status: "error",
          message: "Top.",
          errors: [{ error: "NOT_FOUND", message: "Item." }],
          request_id: "r",
        },
        "fallback",
      ),
    ).toEqual({ code: "NOT_FOUND", message: "Item.", field: null, requestId: "r" });
  });

  it("uses the top-level message when errors is empty", () => {
    expect(
      parseErrorBody({ status: "error", message: "Top.", errors: [] }, "fallback"),
    ).toMatchObject({
      code: "UNKNOWN_ERROR",
      message: "Top.",
    });
  });

  it.each([[null], ["text"], [[1, 2]], [{ errors: ["bad"] }]])("tolerates %j", (body) => {
    expect(parseErrorBody(body, "fallback")).toMatchObject({
      code: "UNKNOWN_ERROR",
      message: "fallback",
    });
  });
});
