/**
 * `VoiceAgentsClient` (specs/26 §3): option resolution, authentication headers, timeouts, and the shared retry policy
 * of the Python SDK (specs/26 §2.2). Resource namespaces are added as their API families land; the public health
 * probe `GET /v1/health` is the first operation.
 */
import { APIConnectionError, APITimeoutError, makeAPIError } from "./errors.js";
import { resolveRuntime, type Runtime } from "./internal.js";
import type { components } from "./schema.js";
import { VERSION } from "./version.js";

export type HealthStatus = components["schemas"]["HealthStatus"];

export interface ClientOptions {
  /** API key; defaults to `process.env.VOICEAGENTS_API_KEY` where `process` exists. */
  apiKey?: string;
  /** API origin; defaults to `process.env.VOICEAGENTS_BASE_URL`, then `DEFAULT_BASE_URL`. */
  baseURL?: string;
  /** Per-attempt timeout in milliseconds. */
  timeout?: number;
  /** Retries after the first attempt for retryable failures. */
  maxRetries?: number;
  /** Acts on behalf of a sub-account (`X-VA-Org-Id`). */
  orgId?: string;
  /** Custom `fetch` implementation (defaults to the global one). */
  fetch?: typeof fetch;
  /** Extra headers sent with every request; they override the SDK defaults. */
  defaultHeaders?: Record<string, string>;
}

export const DEFAULT_BASE_URL = "https://api.voiceagents.ai";
export const DEFAULT_TIMEOUT_MS = 30_000;
export const DEFAULT_MAX_RETRIES = 2;
export const CLIENT_HEADER = "X-VA-Client";

const BACKOFF_BASE_MS = 500;
const BACKOFF_CAP_MS = 8_000;
const MAX_RETRY_AFTER_MS = 60_000;
const RETRYABLE_STATUSES: ReadonlySet<number> = new Set([408, 429, 500, 502, 503, 504]);
const IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS";

type HttpMethod = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

interface ProcessLike {
  process?: { env?: Record<string, string | undefined> };
}

function readEnv(name: string): string | undefined {
  const value = (globalThis as ProcessLike).process?.env?.[name];
  return value === "" ? undefined : value;
}

function stripTrailingSlashes(url: string): string {
  return url.replace(/\/+$/, "");
}

/** Full-jitter exponential backoff: a uniform draw in `[0, min(cap, base * 2^attempt))`. */
export function backoffDelay(attempt: number, random: () => number): number {
  return Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt) * random();
}

/** `Retry-After` as milliseconds (delta-seconds or HTTP date), or `null` when absent, invalid, or too long. */
export function parseRetryAfter(value: string | null, now: number): number | null {
  if (value === null || value.trim() === "") {
    return null;
  }
  const seconds = Number(value);
  const ms = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(value) - now;
  if (!Number.isFinite(ms) || ms < 0 || ms > MAX_RETRY_AFTER_MS) {
    return null;
  }
  return ms;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function isRetryableStatus(status: number, body: unknown): boolean {
  if (RETRYABLE_STATUSES.has(status)) {
    return true;
  }
  return status === 409 && makeAPIError(status, body, null).code === IDEMPOTENCY_IN_PROGRESS;
}

type Attempt =
  | { kind: "ok"; body: unknown }
  | { kind: "retry"; error: Error; retryAfterMs: number | null }
  | { kind: "fail"; error: Error };

export class VoiceAgentsClient {
  readonly baseURL: string;
  readonly timeout: number;
  readonly maxRetries: number;
  readonly #apiKey: string | undefined;
  readonly #orgId: string | undefined;
  readonly #fetch: typeof fetch;
  readonly #defaultHeaders: Record<string, string>;
  readonly #runtime: Runtime;

  constructor(options: ClientOptions = {}) {
    this.baseURL = stripTrailingSlashes(
      options.baseURL ?? readEnv("VOICEAGENTS_BASE_URL") ?? DEFAULT_BASE_URL,
    );
    this.timeout = options.timeout ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.#apiKey = options.apiKey ?? readEnv("VOICEAGENTS_API_KEY");
    this.#orgId = options.orgId;
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.#defaultHeaders = options.defaultHeaders ?? {};
    this.#runtime = resolveRuntime(options);
  }

  /** `GET /v1/health`: public liveness of the API (never reveals dependency state). */
  async health(): Promise<HealthStatus> {
    return (await this.request("GET", "/v1/health")) as HealthStatus;
  }

  #headers(): Headers {
    const headers = new Headers({ Accept: "application/json" });
    headers.set(CLIENT_HEADER, `voiceagents-typescript/${VERSION}`);
    if (this.#apiKey !== undefined) {
      headers.set("Authorization", `Bearer ${this.#apiKey}`);
    }
    if (this.#orgId !== undefined) {
      headers.set("X-VA-Org-Id", this.#orgId);
    }
    for (const [name, value] of Object.entries(this.#defaultHeaders)) {
      headers.set(name, value);
    }
    return headers;
  }

  protected async request(method: HttpMethod, path: string): Promise<unknown> {
    for (let attempt = 0; ; attempt += 1) {
      const outcome = await this.#attempt(method, path);
      if (outcome.kind === "ok") {
        return outcome.body;
      }
      if (outcome.kind === "fail" || attempt >= this.maxRetries) {
        throw outcome.error;
      }
      await this.#runtime.sleep(
        outcome.retryAfterMs ?? backoffDelay(attempt, this.#runtime.random),
      );
    }
  }

  async #attempt(method: HttpMethod, path: string): Promise<Attempt> {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, this.timeout);
    try {
      const response = await this.#fetch(`${this.baseURL}${path}`, {
        method,
        headers: this.#headers(),
        signal: controller.signal,
      });
      return await this.#classify(response);
    } catch (error) {
      return this.#connectionFailure(error, controller.signal.aborted);
    } finally {
      clearTimeout(timer);
    }
  }

  async #classify(response: Response): Promise<Attempt> {
    const body = await readBody(response);
    if (response.ok) {
      return { kind: "ok", body };
    }
    const error = makeAPIError(response.status, body, response.headers.get("X-Request-Id"));
    if (!isRetryableStatus(response.status, body)) {
      return { kind: "fail", error };
    }
    const retryAfterMs = parseRetryAfter(response.headers.get("Retry-After"), Date.now());
    return { kind: "retry", error, retryAfterMs };
  }

  #connectionFailure(error: unknown, timedOut: boolean): Attempt {
    const wrapped = timedOut
      ? new APITimeoutError(`Request timed out after ${this.timeout} ms`, { cause: error })
      : new APIConnectionError("Connection error", { cause: error });
    return { kind: "retry", error: wrapped, retryAfterMs: null };
  }
}
