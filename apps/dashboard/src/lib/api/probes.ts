/**
 * Status probes for the `/status` page: the api's unlisted `/healthz` and `/readyz` (bodies per specs/27 §9
 * Decision, validated with zod) and the public `GET /v1/health` through the typed client. A 503 readiness body is
 * data (it lists the failing dependencies), not an error.
 */
import { z } from "zod";

import type { ApiClient } from "./client";
import { ApiError, toApiError } from "./errors";

export const livenessSchema = z.object({ status: z.literal("ok"), version: z.string() });

export const checkStatusSchema = z.enum(["ok", "error", "timeout"]);

export const readinessSchema = z.object({
  status: z.enum(["ok", "unavailable"]),
  checks: z.record(z.string(), checkStatusSchema),
  failing: z.array(z.string()).optional(),
  version: z.string(),
});

export type Liveness = z.infer<typeof livenessSchema>;
export type Readiness = z.infer<typeof readinessSchema>;
export type CheckStatus = z.infer<typeof checkStatusSchema>;

export const UNREACHABLE_CODE = "API_UNREACHABLE";
export const INVALID_BODY_CODE = "INVALID_RESPONSE";

/** The api did not answer (network failure, CORS rejection, DNS). */
export class ProbeUnreachableError extends ApiError {
  constructor(url: string, cause: unknown) {
    super(0, { code: UNREACHABLE_CODE, message: `Could not reach ${url}` });
    this.name = "ProbeUnreachableError";
    this.cause = cause;
  }
}

async function getJson(
  fetchImpl: typeof fetch,
  url: string,
): Promise<{ response: Response; body: unknown }> {
  let response: Response;
  try {
    response = await fetchImpl(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  } catch (error) {
    throw new ProbeUnreachableError(url, error);
  }
  const body: unknown = await response.json().catch(() => null);
  return { response, body };
}

function parseOrThrow<T>(schema: z.ZodType<T>, response: Response, body: unknown): T {
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(response.status, {
      code: INVALID_BODY_CODE,
      message: "The api returned an unexpected response body.",
      requestId: response.headers.get("X-Request-Id") ?? undefined,
    });
  }
  return parsed.data;
}

export async function fetchLiveness(
  baseUrl: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Liveness> {
  const { response, body } = await getJson(fetchImpl, `${baseUrl}/healthz`);
  if (!response.ok) {
    throw toApiError(response.status, body, response.headers.get("X-Request-Id"));
  }
  return parseOrThrow(livenessSchema, response, body);
}

export async function fetchReadiness(
  baseUrl: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Readiness> {
  const { response, body } = await getJson(fetchImpl, `${baseUrl}/readyz`);
  if (!response.ok && response.status !== 503) {
    throw toApiError(response.status, body, response.headers.get("X-Request-Id"));
  }
  return parseOrThrow(readinessSchema, response, body);
}

export async function fetchPublicHealth(client: ApiClient): Promise<"ok"> {
  const { data, error, response } = await client.GET("/v1/health").catch((cause: unknown) => {
    throw new ProbeUnreachableError("/v1/health", cause);
  });
  if (data === undefined) {
    throw toApiError(response.status, error, response.headers.get("X-Request-Id"));
  }
  return data.status;
}
