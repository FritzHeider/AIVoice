import pkg from "../../../../package.json";

/** Dashboard liveness and readiness (specs/27 §9): the Next server is up and serving route handlers. */
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json(
    { status: "ok", version: pkg.version },
    { headers: { "Cache-Control": "no-store" } },
  );
}
