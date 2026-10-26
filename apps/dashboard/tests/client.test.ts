// @vitest-environment node
import { describe, expect, it } from "vitest";

import { createApiClient, CSRF_HEADER, csrfMiddleware, readCookie } from "@/lib/api/client";

import { fakeFetch, json } from "./fakes";

describe("readCookie", () => {
  it("finds a cookie among others and decodes it", () => {
    expect(readCookie("a=1; va_csrf=t%3Dk; b=2", "va_csrf")).toBe("t=k");
    expect(readCookie("a=1", "va_csrf")).toBeUndefined();
    expect(readCookie("", "va_csrf")).toBeUndefined();
  });
});

describe("createApiClient", () => {
  it("sends credentials and no CSRF header on GET", async () => {
    const { fetch, requests } = fakeFetch({ "/v1/health": () => json({ status: "ok" }) });
    const client = createApiClient("http://api.test", fetch, () => "va_csrf=tok");
    await client.GET("/v1/health");
    expect(requests[0]?.credentials).toBe("include");
    expect(requests[0]?.headers.has(CSRF_HEADER)).toBe(false);
  });
});

describe("csrfMiddleware", () => {
  const run = async (method: string, cookies: string) => {
    const middleware = csrfMiddleware(() => cookies);
    const request = new Request("http://api.test/v1/calls", { method });
    const onRequest = middleware.onRequest;
    if (onRequest === undefined) {
      throw new Error("onRequest missing");
    }
    const result = await onRequest({
      request,
      schemaPath: "/v1/calls",
      params: {},
      options: {} as never,
      id: "1",
    });
    return result instanceof Request ? result : request;
  };

  it("injects the token on unsafe methods", async () => {
    expect((await run("POST", "va_csrf=tok")).headers.get(CSRF_HEADER)).toBe("tok");
    expect((await run("DELETE", "va_csrf=tok")).headers.get(CSRF_HEADER)).toBe("tok");
  });

  it("skips safe methods and missing cookies", async () => {
    expect((await run("GET", "va_csrf=tok")).headers.has(CSRF_HEADER)).toBe(false);
    expect((await run("POST", "other=1")).headers.has(CSRF_HEADER)).toBe(false);
  });
});
