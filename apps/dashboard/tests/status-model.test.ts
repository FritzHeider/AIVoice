// @vitest-environment node
import { describe, expect, it } from "vitest";

import { overallState, readinessUnavailable, rowState } from "@/components/status/status-model";
import { ApiError } from "@/lib/api/errors";
import { ProbeUnreachableError } from "@/lib/api/probes";

const unreachable = new ProbeUnreachableError("http://api.test/healthz", new TypeError("x"));

describe("rowState", () => {
  it("derives loading, ok, down, and unavailable", () => {
    expect(rowState({ data: undefined, error: null })).toBe("loading");
    expect(rowState({ data: "ok", error: null })).toBe("ok");
    expect(rowState({ data: undefined, error: new Error("x") })).toBe("down");
    const notReady = { status: "unavailable", checks: {}, version: "1" } as const;
    expect(rowState({ data: notReady, error: null }, readinessUnavailable)).toBe("unavailable");
  });
});

describe("overallState", () => {
  it("is unreachable only when every probe failed to connect", () => {
    expect(overallState(["down", "down", "down"], [unreachable, unreachable, unreachable])).toBe(
      "unreachable",
    );
    const apiError = new ApiError(500, { code: "INTERNAL", message: "x" });
    expect(overallState(["down", "down", "down"], [unreachable, apiError, unreachable])).toBe(
      "degraded",
    );
  });

  it("prefers degraded over checking, and checking over operational", () => {
    expect(overallState(["ok", "unavailable", "loading"], [])).toBe("degraded");
    expect(overallState(["ok", "loading", "ok"], [])).toBe("checking");
    expect(overallState(["ok", "ok", "ok"], [null, null, null])).toBe("operational");
  });
});
