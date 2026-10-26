// @vitest-environment node
import { describe, expect, it } from "vitest";

import pkg from "../package.json";
import { dynamic, GET } from "@/app/api/healthz/route";

describe("GET /api/healthz", () => {
  it("returns ok with the package version, uncached", async () => {
    const response = GET();
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await response.json()).toEqual({ status: "ok", version: pkg.version });
    expect(dynamic).toBe("force-dynamic");
  });
});
