// @vitest-environment node
import { describe, expect, it } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import {
  fetchLiveness,
  fetchPublicHealth,
  fetchReadiness,
  INVALID_BODY_CODE,
  ProbeUnreachableError,
  UNREACHABLE_CODE,
} from "@/lib/api/probes";

import { fakeFetch, json, LIVE, NOT_READY, READY } from "./fakes";

const BASE = "http://api.test";

describe("fetchLiveness", () => {
  it("parses the liveness body", async () => {
    const { fetch, requests } = fakeFetch({ "/healthz": () => json(LIVE) });
    await expect(fetchLiveness(BASE, fetch)).resolves.toEqual(LIVE);
    expect(requests[0]?.headers.get("Accept")).toBe("application/json");
  });

  it("rejects a malformed body with the request id", async () => {
    const { fetch } = fakeFetch({
      "/healthz": () => json({ status: "fine" }, 200, { "X-Request-Id": "req_x" }),
    });
    await expect(fetchLiveness(BASE, fetch)).rejects.toMatchObject({
      code: INVALID_BODY_CODE,
      requestId: "req_x",
    });
  });

  it("rejects a non-JSON body", async () => {
    const { fetch } = fakeFetch({ "/healthz": () => new Response("<html>", { status: 200 }) });
    await expect(fetchLiveness(BASE, fetch)).rejects.toMatchObject({ code: INVALID_BODY_CODE });
  });

  it("maps an error response to ApiError", async () => {
    const { fetch } = fakeFetch({
      "/healthz": () =>
        json(
          { data: null, errors: [{ error: "INTERNAL", message: "boom" }], request_id: "req_b" },
          500,
        ),
    });
    const error = await fetchLiveness(BASE, fetch).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 500, code: "INTERNAL", requestId: "req_b" });
  });

  it("raises ProbeUnreachableError on network failure", async () => {
    const { fetch } = fakeFetch({});
    const error = await fetchLiveness(BASE, fetch).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProbeUnreachableError);
    expect(error).toMatchObject({ code: UNREACHABLE_CODE, status: 0 });
  });
});

describe("fetchReadiness", () => {
  it("parses a ready body", async () => {
    const { fetch } = fakeFetch({ "/readyz": () => json(READY) });
    await expect(fetchReadiness(BASE, fetch)).resolves.toEqual(READY);
  });

  it("treats a 503 body as data", async () => {
    const { fetch } = fakeFetch({ "/readyz": () => json(NOT_READY, 503) });
    await expect(fetchReadiness(BASE, fetch)).resolves.toEqual(NOT_READY);
  });

  it("rejects an unknown check result", async () => {
    const body = { ...READY, checks: { postgres: "slow" } };
    const { fetch } = fakeFetch({ "/readyz": () => json(body, 503) });
    await expect(fetchReadiness(BASE, fetch)).rejects.toMatchObject({ code: INVALID_BODY_CODE });
  });

  it("maps other error statuses", async () => {
    const { fetch } = fakeFetch({ "/readyz": () => json({}, 502) });
    await expect(fetchReadiness(BASE, fetch)).rejects.toMatchObject({
      status: 502,
      code: "UNKNOWN_ERROR",
    });
  });
});

describe("fetchPublicHealth", () => {
  it("returns ok through the typed client", async () => {
    const { fetch, requests } = fakeFetch({ "/v1/health": () => json({ status: "ok" }) });
    await expect(fetchPublicHealth(createApiClient(BASE, fetch))).resolves.toBe("ok");
    expect(requests[0]?.url).toBe(`${BASE}/v1/health`);
  });

  it("maps an error response", async () => {
    const { fetch } = fakeFetch({
      "/v1/health": () =>
        json(
          { status: "error", message: "nope", errors: [{ error: "FORBIDDEN", message: "nope" }] },
          403,
          { "X-Request-Id": "req_f" },
        ),
    });
    await expect(fetchPublicHealth(createApiClient(BASE, fetch))).rejects.toMatchObject({
      status: 403,
      code: "FORBIDDEN",
      requestId: "req_f",
    });
  });

  it("raises ProbeUnreachableError on network failure", async () => {
    const { fetch } = fakeFetch({});
    await expect(fetchPublicHealth(createApiClient(BASE, fetch))).rejects.toBeInstanceOf(
      ProbeUnreachableError,
    );
  });
});
