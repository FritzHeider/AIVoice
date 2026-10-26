import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    setupFiles: ["tests/setup.ts"],
    typecheck: { include: ["tests/**/*.test-d.ts"], tsconfig: "./tsconfig.json" },
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      exclude: ["src/schema.d.ts"],
      thresholds: { lines: 80, branches: 80, functions: 80, statements: 80 },
    },
  },
});
