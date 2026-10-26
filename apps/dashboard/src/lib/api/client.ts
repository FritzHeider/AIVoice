/**
 * Typed api client (specs/25 §23): `openapi-fetch` over the generated `schema.d.ts`, sending the session cookie
 * (`credentials: "include"`) and the double-submit CSRF header on unsafe methods (specs/04 §4.4).
 */
import createClient, { type Client, type Middleware } from "openapi-fetch";

import type { paths } from "./schema";

export type ApiClient = Client<paths>;

export const CSRF_COOKIE = "va_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

const SAFE_METHODS: ReadonlySet<string> = new Set(["GET", "HEAD", "OPTIONS"]);

/** Value of cookie `name` in a `document.cookie` string, or `undefined`. */
export function readCookie(cookies: string, name: string): string | undefined {
  for (const part of cookies.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) {
      return decodeURIComponent(rest.join("="));
    }
  }
  return undefined;
}

export function csrfMiddleware(cookies: () => string): Middleware {
  return {
    onRequest({ request }) {
      if (SAFE_METHODS.has(request.method)) {
        return request;
      }
      const token = readCookie(cookies(), CSRF_COOKIE);
      if (token !== undefined) {
        request.headers.set(CSRF_HEADER, token);
      }
      return request;
    },
  };
}

function documentCookies(): string {
  return typeof document === "undefined" ? "" : document.cookie;
}

export function createApiClient(
  baseUrl: string,
  fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
  cookies: () => string = documentCookies,
): ApiClient {
  const client = createClient<paths>({ baseUrl, credentials: "include", fetch: fetchImpl });
  client.use(csrfMiddleware(cookies));
  return client;
}
