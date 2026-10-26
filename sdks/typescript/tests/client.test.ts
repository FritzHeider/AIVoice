import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  APIConnectionError,
  APITimeoutError,
  CLIENT_HEADER,
  DEFAULT_BASE_URL,
  DEFAULT_MAX_RETRIES,
  DEFAULT_TIMEOUT_MS,
  InternalServerError,
  NotFoundError,
  RateLimitError,
  VERSION,
  VoiceAgentsClient,
} from "../src/index.js";
import { backoffDelay, parseRetryAfter } from "../src/client.js";
import { BASE_URL, server, testClient } from "./helpers.js";

const HEALTH = `${BASE_URL}/v1/health`;

function envelope404(): Response {
  return HttpResponse.json(
    {
      data: null,
      errors: [{ error: "NOT_FOUND", message: "Not found.", field: null, details: null }],
      request_id: "req_body",
    },
    { status: 404, headers: { "X-Request-Id": "req_header" } },
  );
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("VoiceAgentsClient options", () => {
  it("uses defaults", () => {
    vi.stubEnv("VOICEAGENTS_BASE_URL", "");
    const client = new VoiceAgentsClient();
    expect(client.baseURL).toBe(DEFAULT_BASE_URL);
    expect(client.timeout).toBe(DEFAULT_TIMEOUT_MS);
    expect(client.maxRetries).toBe(DEFAULT_MAX_RETRIES);
  });

  it("strips trailing slashes from baseURL", () => {
    expect(new VoiceAgentsClient({ baseURL: "http://api.test///" }).baseURL).toBe(BASE_URL);
  });

  it("reads baseURL and apiKey from the environment", async () => {
    vi.stubEnv("VOICEAGENTS_BASE_URL", `${BASE_URL}/`);
    vi.stubEnv("VOICEAGENTS_API_KEY", "va_env_key");
    let auth: string | null = null;
    server.use(
      http.get(HEALTH, ({ request }) => {
        auth = request.headers.get("Authorization");
        return HttpResponse.json({ status: "ok" });
      }),
    );
    const client = new VoiceAgentsClient();
    expect(client.baseURL).toBe(BASE_URL);
    await client.health();
    expect(auth).toBe("Bearer va_env_key");
  });

  it("sends no Authorization header without an api key", async () => {
    vi.stubEnv("VOICEAGENTS_API_KEY", "");
    let headers = new Headers();
    server.use(
      http.get(HEALTH, ({ request }) => {
        headers = request.headers;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    await new VoiceAgentsClient({ baseURL: BASE_URL }).health();
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.has("X-VA-Org-Id")).toBe(false);
  });
});

describe("health", () => {
  it("returns the public health status", async () => {
    server.use(http.get(HEALTH, () => HttpResponse.json({ status: "ok" })));
    const { client } = testClient();
    await expect(client.health()).resolves.toEqual({ status: "ok" });
  });

  it("sends bearer, org, client, and default headers", async () => {
    let headers = new Headers();
    server.use(
      http.get(HEALTH, ({ request }) => {
        headers = request.headers;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    const { client } = testClient({ orgId: "org_sub", defaultHeaders: { "X-Trace": "abc" } });
    await client.health();
    expect(headers.get("Authorization")).toBe("Bearer va_test_key");
    expect(headers.get("X-VA-Org-Id")).toBe("org_sub");
    expect(headers.get(CLIENT_HEADER)).toBe(`voiceagents-typescript/${VERSION}`);
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.get("X-Trace")).toBe("abc");
  });

  it("uses a custom fetch", async () => {
    const calls: string[] = [];
    const customFetch: typeof fetch = (input) => {
      calls.push(input instanceof Request ? input.url : String(input));
      return Promise.resolve(Response.json({ status: "ok" }));
    };
    const { client } = testClient({ fetch: customFetch });
    await client.health();
    expect(calls).toEqual([HEALTH]);
  });
});

describe("retries", () => {
  it("retries a 503 with jittered backoff, then succeeds", async () => {
    let count = 0;
    server.use(
      http.get(HEALTH, () => {
        count += 1;
        return count < 3
          ? HttpResponse.json({}, { status: 503 })
          : HttpResponse.json({ status: "ok" });
      }),
    );
    const { client, sleeps } = testClient({}, 0.5);
    await expect(client.health()).resolves.toEqual({ status: "ok" });
    expect(count).toBe(3);
    expect(sleeps).toEqual([250, 500]);
  });

  it("honours Retry-After seconds on 429", async () => {
    let count = 0;
    server.use(
      http.get(HEALTH, () => {
        count += 1;
        return count === 1
          ? HttpResponse.json({}, { status: 429, headers: { "Retry-After": "3" } })
          : HttpResponse.json({ status: "ok" });
      }),
    );
    const { client, sleeps } = testClient();
    await client.health();
    expect(sleeps).toEqual([3000]);
  });

  it("gives up after maxRetries with the request id from the header", async () => {
    let count = 0;
    server.use(
      http.get(HEALTH, () => {
        count += 1;
        return HttpResponse.json(
          { status: "error", message: "Down.", errors: [{ error: "INTERNAL", message: "Down." }] },
          { status: 500, headers: { "X-Request-Id": "req_500" } },
        );
      }),
    );
    const { client, sleeps } = testClient({ maxRetries: 1 });
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(InternalServerError);
    expect(error).toMatchObject({ status: 500, code: "INTERNAL", requestId: "req_500" });
    expect(count).toBe(2);
    expect(sleeps).toHaveLength(1);
  });

  it("does not retry a 404 and maps the envelope body", async () => {
    let count = 0;
    server.use(
      http.get(HEALTH, () => {
        count += 1;
        return envelope404();
      }),
    );
    const { client } = testClient();
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(NotFoundError);
    expect(error).toMatchObject({
      code: "NOT_FOUND",
      message: "Not found.",
      requestId: "req_header",
    });
    expect(count).toBe(1);
  });

  it("retries 409 IDEMPOTENCY_IN_PROGRESS but not other conflicts", async () => {
    const bodies = [
      {
        status: "error",
        message: "busy",
        errors: [{ error: "IDEMPOTENCY_IN_PROGRESS", message: "busy" }],
      },
      { status: "error", message: "dup", errors: [{ error: "CONFLICT", message: "dup" }] },
    ];
    let count = 0;
    server.use(
      http.get(HEALTH, () => {
        const body = bodies[Math.min(count, 1)];
        count += 1;
        return HttpResponse.json(body, { status: 409 });
      }),
    );
    const { client } = testClient();
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 409, code: "CONFLICT" });
    expect(count).toBe(2);
  });

  it("maps a rate limit that never clears to RateLimitError", async () => {
    server.use(http.get(HEALTH, () => HttpResponse.text("slow down", { status: 429 })));
    const { client } = testClient({ maxRetries: 0 });
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(RateLimitError);
    expect(error).toMatchObject({ code: "UNKNOWN_ERROR", message: "HTTP 429", body: "slow down" });
  });

  it("wraps network failures in APIConnectionError after retrying", async () => {
    server.use(http.get(HEALTH, () => HttpResponse.error()));
    const { client, sleeps } = testClient();
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(APIConnectionError);
    expect(error).not.toBeInstanceOf(APITimeoutError);
    expect(sleeps).toHaveLength(DEFAULT_MAX_RETRIES);
  });

  it("raises APITimeoutError when the attempt exceeds the timeout", async () => {
    const hangingFetch: typeof fetch = (_input, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    const { client } = testClient({ fetch: hangingFetch, timeout: 1, maxRetries: 0 });
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(APITimeoutError);
    expect(error).toMatchObject({ message: "Request timed out after 1 ms" });
  });
});

describe("backoff helpers", () => {
  it("caps the exponential backoff at 8 s", () => {
    expect(backoffDelay(0, () => 1)).toBe(500);
    expect(backoffDelay(3, () => 1)).toBe(4000);
    expect(backoffDelay(10, () => 1)).toBe(8000);
    expect(backoffDelay(2, () => 0)).toBe(0);
  });

  it.each([
    [null, null],
    ["", null],
    ["2", 2000],
    ["0.5", 500],
    ["-1", null],
    ["120", null],
    ["soon", null],
  ])("parses Retry-After %j", (value, expected) => {
    expect(parseRetryAfter(value, 0)).toBe(expected);
  });

  it("parses an HTTP-date Retry-After", () => {
    const now = Date.parse("2026-10-25T12:00:00Z");
    expect(parseRetryAfter("Sun, 25 Oct 2026 12:00:05 GMT", now)).toBe(5000);
  });
});
