/** Route-table fetch double for probe and client tests. */

export type Responder = (request: Request) => Response | Promise<Response>;

export interface FakeFetch {
  fetch: typeof fetch;
  requests: Request[];
}

export function fakeFetch(routes: Record<string, Responder>): FakeFetch {
  const requests: Request[] = [];
  const impl: typeof fetch = async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    const route = routes[new URL(request.url).pathname];
    if (route === undefined) {
      throw new TypeError(`fetch failed: no route for ${request.url}`);
    }
    return route(request);
  };
  return { fetch: impl, requests };
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export const LIVE = { status: "ok", version: "1.2.3" };
export const READY = {
  status: "ok",
  checks: { postgres: "ok", redis: "ok", temporal: "ok" },
  version: "1.2.3",
};
export const NOT_READY = {
  status: "unavailable",
  checks: { postgres: "ok", redis: "timeout", temporal: "error" },
  failing: ["redis", "temporal"],
  version: "1.2.3",
};
