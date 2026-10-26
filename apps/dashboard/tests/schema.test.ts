// @vitest-environment node
import { readFileSync } from "node:fs";

import openapiTS, { astToString } from "openapi-typescript";
import { describe, expect, it } from "vitest";

const SPEC = new URL("../../../openapi/openapi.json", import.meta.url);
const COMMITTED = new URL("../src/lib/api/schema.d.ts", import.meta.url);

describe("generated schema", () => {
  it("matches openapi/openapi.json (run `pnpm --filter dashboard gen:api` after `make openapi`)", async () => {
    const generated = astToString(await openapiTS(SPEC));
    expect(readFileSync(COMMITTED, "utf8").endsWith(generated)).toBe(true);
  });
});
