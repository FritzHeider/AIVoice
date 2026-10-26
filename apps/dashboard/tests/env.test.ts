// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiBaseUrl, DEFAULT_API_BASE_URL } from "@/lib/env";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("apiBaseUrl", () => {
  it("defaults to the local api", () => {
    expect(apiBaseUrl(undefined)).toBe(DEFAULT_API_BASE_URL);
    expect(apiBaseUrl("")).toBe(DEFAULT_API_BASE_URL);
  });

  it("reads NEXT_PUBLIC_API_BASE_URL and strips trailing slashes", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "https://api.voiceagents.ai//");
    expect(apiBaseUrl()).toBe("https://api.voiceagents.ai");
  });

  it.each(["not a url", "ftp://files.voiceagents.ai"])("rejects %j", (value) => {
    expect(() => apiBaseUrl(value)).toThrow();
  });
});
