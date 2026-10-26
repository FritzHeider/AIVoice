import { z } from "zod";

export const DEFAULT_API_BASE_URL = "http://localhost:8000";

const apiBaseUrlSchema = z.url({ protocol: /^https?$/ });

/**
 * Browser base URL of the api (`NEXT_PUBLIC_API_BASE_URL`, specs/02 §6) without a trailing slash. Next inlines the
 * value at build time, so the variable is read through its literal name.
 */
export function apiBaseUrl(
  value: string | undefined = process.env.NEXT_PUBLIC_API_BASE_URL,
): string {
  const raw = value === undefined || value === "" ? DEFAULT_API_BASE_URL : value;
  return apiBaseUrlSchema.parse(raw).replace(/\/+$/, "");
}
