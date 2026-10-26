// @vitest-environment node
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { contrastRatio, parseOklch } from "./contrast";

const CSS = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");

const COLOR_TOKENS = [
  "background",
  "foreground",
  "muted",
  "muted-foreground",
  "card",
  "border",
  "primary",
  "primary-foreground",
  "success",
  "warning",
  "danger",
  "info",
  "node-default",
  "node-wait",
  "node-kb",
  "node-webhook",
  "node-transfer",
  "node-end",
  "node-sms",
  "node-code",
  "node-button",
  "node-transfer-pathway",
  "node-scheduling",
  "node-global",
] as const;

const THEMED = [
  "background",
  "foreground",
  "muted",
  "muted-foreground",
  "card",
  "border",
  "primary",
  "primary-foreground",
];

const TEXT_PAIRS: readonly [string, string][] = [
  ["foreground", "background"],
  ["foreground", "card"],
  ["foreground", "muted"],
  ["muted-foreground", "background"],
  ["muted-foreground", "card"],
  ["muted-foreground", "muted"],
  ["primary-foreground", "primary"],
  ["primary", "background"],
];

type Theme = "light" | "dark";

function rootTokens(): Map<string, string> {
  const start = CSS.indexOf(":root {");
  const body = CSS.slice(start, CSS.indexOf("\n}", start));
  return new Map(
    [...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1] ?? "", (m[2] ?? "").trim()]),
  );
}

const TOKENS = rootTokens();
const LIGHT_DARK_RE = /^light-dark\((oklch\([^)]*\)),\s*(oklch\([^)]*\))\)$/;

function raw(name: string, theme: Theme): string {
  const value = TOKENS.get(name);
  if (value === undefined) {
    throw new Error(`missing token --${name}`);
  }
  const pair = LIGHT_DARK_RE.exec(value);
  if (pair === null) {
    return value;
  }
  return (theme === "light" ? pair[1] : pair[2]) ?? "";
}

function colour(name: string, theme: Theme) {
  return parseOklch(raw(name, theme));
}

describe("design tokens", () => {
  it("defines every spec colour token as oklch in both themes", () => {
    for (const name of COLOR_TOKENS) {
      expect(() => colour(name, "light"), name).not.toThrow();
      expect(() => colour(name, "dark"), name).not.toThrow();
    }
  });

  it("uses the spec palette anchors", () => {
    expect(raw("primary", "light")).toBe("oklch(51.1% 0.262 276.966)");
    expect(raw("primary", "dark")).toBe("oklch(67.3% 0.182 276.935)");
    expect(TOKENS.get("radius")).toBe("8px");
    expect(TOKENS.get("space")).toBe("4px");
    expect(TOKENS.get("font-sans")).toContain("Inter");
    expect(TOKENS.get("font-mono")).toContain("JetBrains Mono");
  });

  it("gives every themed token a distinct dark value", () => {
    for (const name of THEMED) {
      expect(TOKENS.get(name), name).toMatch(LIGHT_DARK_RE);
      expect(raw(name, "dark"), name).not.toBe(raw(name, "light"));
    }
  });

  it("follows the system scheme unless a class forces one", () => {
    expect(CSS).toMatch(/:root \{\n\s+color-scheme: light dark;/);
    expect(CSS).toMatch(/:root\.light \{\n\s+color-scheme: light;/);
    expect(CSS).toMatch(/:root\.dark \{\n\s+color-scheme: dark;/);
  });

  it.each(["light", "dark"] as const)("meets WCAG AA text contrast in %s", (theme) => {
    for (const [fg, bg] of TEXT_PAIRS) {
      const ratio = contrastRatio(colour(fg, theme), colour(bg, theme));
      expect(ratio, `${fg} on ${bg}: ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("contrast helper", () => {
  it("matches the WCAG extremes", () => {
    const white = parseOklch("oklch(100% 0 0)");
    const black = parseOklch("oklch(0% 0 0)");
    expect(contrastRatio(white, black)).toBeCloseTo(21, 1);
    expect(contrastRatio(white, white)).toBeCloseTo(1, 5);
  });

  it("rejects non-oklch values", () => {
    expect(() => parseOklch("#fff")).toThrow("not an oklch() colour");
  });
});
