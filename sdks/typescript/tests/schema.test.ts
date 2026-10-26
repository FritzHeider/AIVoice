import { readFileSync } from "node:fs";

import openapiTS, { astToString } from "openapi-typescript";
import { describe, expect, it } from "vitest";

const SPEC = new URL("../../../openapi/openapi.json", import.meta.url);
const COMMITTED = new URL("../src/schema.d.ts", import.meta.url);

describe("generated schema", () => {
  it("matches openapi/openapi.json (run `pnpm gen:api` after `make openapi`)", async () => {
    const generated = astToString(await openapiTS(SPEC));
    const committed = readFileSync(COMMITTED, "utf8");
    expect(committed.endsWith(generated)).toBe(true);
  });
});
