import { defineConfig } from "vitest/config";

export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2019",
    minify: "esbuild",
    lib: {
      entry: "src/index.ts",
      name: "VoiceAgentsWidget",
      formats: ["iife"],
      fileName: () => "widget.js",
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      thresholds: { lines: 80, branches: 80, functions: 80, statements: 80 },
    },
  },
});
