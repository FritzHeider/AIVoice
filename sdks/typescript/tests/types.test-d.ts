import { describe, expectTypeOf, it } from "vitest";

import { VoiceAgentsClient, type ClientOptions, type HealthStatus } from "../src/index.js";

describe("public types", () => {
  it("types health() from the OpenAPI schema", () => {
    const client = new VoiceAgentsClient({ apiKey: "k" });
    expectTypeOf(client.health()).resolves.toEqualTypeOf<HealthStatus>();
    expectTypeOf<HealthStatus>().toEqualTypeOf<{ status: "ok" }>();
  });

  it("accepts the documented options only", () => {
    expectTypeOf<ClientOptions>().toHaveProperty("orgId");
    // @ts-expect-error unknown options are rejected
    new VoiceAgentsClient({ apiKey: "k", region: "eu" });
  });
});
