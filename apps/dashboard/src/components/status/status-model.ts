/** Pure status derivation for the `/status` page, kept apart from rendering so it is unit-testable. */
import type { Readiness } from "@/lib/api/probes";
import { ProbeUnreachableError } from "@/lib/api/probes";

export type RowState = "ok" | "down" | "unavailable" | "loading";
export type Overall = "operational" | "degraded" | "checking" | "unreachable";

export interface ProbeResult<T> {
  data: T | undefined;
  error: Error | null;
}

export function rowState<T>(probe: ProbeResult<T>, isUnavailable?: (data: T) => boolean): RowState {
  if (probe.data !== undefined) {
    return isUnavailable?.(probe.data) === true ? "unavailable" : "ok";
  }
  return probe.error === null ? "loading" : "down";
}

export function readinessUnavailable(readiness: Readiness): boolean {
  return readiness.status === "unavailable";
}

export function overallState(
  rows: readonly RowState[],
  errors: readonly (Error | null)[],
): Overall {
  if (errors.length > 0 && errors.every((error) => error instanceof ProbeUnreachableError)) {
    return "unreachable";
  }
  if (rows.some((row) => row === "down" || row === "unavailable")) {
    return "degraded";
  }
  return rows.includes("loading") ? "checking" : "operational";
}

export const ROW_TONE = {
  ok: "success",
  down: "danger",
  unavailable: "warning",
  loading: "neutral",
} as const satisfies Record<RowState, string>;

export const CHECK_TONE = { ok: "success", error: "danger", timeout: "warning" } as const;
