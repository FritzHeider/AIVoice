import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      // The root layout renders <html>/<body> and imports global CSS; `next build` and the smoke run cover it.
      exclude: ["src/**/*.d.ts", "src/app/layout.tsx"],
      thresholds: { lines: 80, branches: 80, functions: 80, statements: 80 },
    },
  },
});
