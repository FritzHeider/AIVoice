"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { en } from "@/i18n/en";
import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { fetchLiveness, fetchPublicHealth, fetchReadiness, type Readiness } from "@/lib/api/probes";
import { apiBaseUrl } from "@/lib/env";
import { cn } from "@/lib/utils";

import {
  CHECK_TONE,
  overallState,
  readinessUnavailable,
  ROW_TONE,
  rowState,
  type Overall,
  type RowState,
} from "./status-model";

export const REFRESH_INTERVAL_MS = 10_000;

const t = en.status;

export interface StatusViewProps {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

function useProbes(baseUrl: string, fetchImpl: typeof fetch) {
  const client = useMemo(() => createApiClient(baseUrl, fetchImpl), [baseUrl, fetchImpl]);
  const common = { refetchInterval: REFRESH_INTERVAL_MS, retry: false, staleTime: 0 } as const;
  const liveness = useQuery({
    queryKey: ["status", baseUrl, "liveness"],
    queryFn: () => fetchLiveness(baseUrl, fetchImpl),
    ...common,
  });
  const readiness = useQuery({
    queryKey: ["status", baseUrl, "readiness"],
    queryFn: () => fetchReadiness(baseUrl, fetchImpl),
    ...common,
  });
  const publicHealth = useQuery({
    queryKey: ["status", baseUrl, "public-health"],
    queryFn: () => fetchPublicHealth(client),
    ...common,
  });
  return { liveness, readiness, publicHealth };
}

const OVERALL_TEXT: Record<Overall, string> = {
  operational: t.allOperational,
  degraded: t.degraded,
  checking: t.checking,
  unreachable: t.unreachableTitle,
};

const OVERALL_DOT: Record<Overall, string> = {
  operational: "bg-success",
  degraded: "bg-warning",
  checking: "bg-muted-foreground animate-pulse",
  unreachable: "bg-danger",
};

function RowBadge({ state }: { state: RowState }) {
  return <Badge tone={ROW_TONE[state]}>{t.rowStatus[state]}</Badge>;
}

function ErrorNote({ error }: { error: Error | null }) {
  if (!(error instanceof ApiError) || error.requestId === undefined) {
    return null;
  }
  return (
    <p className="mt-1 font-mono text-xs text-muted-foreground">
      {t.requestId} {error.requestId}
    </p>
  );
}

function ProbeRow(props: {
  label: string;
  detail: string;
  state: RowState;
  error: Error | null;
  children?: ReactNode;
}) {
  return (
    <li className="grid gap-3 py-5 first:pt-0 last:pb-0 sm:grid-cols-[1fr_auto] sm:items-start">
      <div>
        <p className="font-medium">{props.label}</p>
        <p className="font-mono text-xs text-muted-foreground">{props.detail}</p>
        <ErrorNote error={props.error} />
        {props.children}
      </div>
      <RowBadge state={props.state} />
    </li>
  );
}

function ChecksTable({ readiness }: { readiness: Readiness }) {
  const entries = Object.entries(readiness.checks).sort(([a], [b]) => a.localeCompare(b));
  return (
    <table className="mt-4 w-full text-sm">
      <thead>
        <tr className="text-left text-xs tracking-wide text-muted-foreground uppercase">
          <th scope="col" className="pb-2 font-medium">
            {t.checkColumn}
          </th>
          <th scope="col" className="pb-2 text-right font-medium">
            {t.resultColumn}
          </th>
        </tr>
      </thead>
      <tbody className="divide-y">
        {entries.map(([name, result]) => (
          <tr key={name}>
            <td className="py-2 font-mono">{name}</td>
            <td className="py-2 text-right">
              <Badge tone={CHECK_TONE[result]}>{t.checkStatus[result]}</Badge>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SkeletonRows() {
  return (
    <ul aria-hidden="true" className="divide-y">
      {[0, 1, 2].map((i) => (
        <li key={i} className="flex items-center justify-between py-5 first:pt-0 last:pb-0">
          <div className="space-y-2">
            <div className="h-4 w-32 animate-pulse rounded bg-muted" />
            <div className="h-3 w-24 animate-pulse rounded bg-muted" />
          </div>
          <div className="h-5 w-20 animate-pulse rounded-full bg-muted" />
        </li>
      ))}
    </ul>
  );
}

function formatTime(ms: number): string {
  return new Intl.DateTimeFormat("en", { timeStyle: "medium" }).format(ms);
}

export function StatusView({ baseUrl = apiBaseUrl(), fetchImpl = fetch }: StatusViewProps) {
  const { liveness, readiness, publicHealth } = useProbes(baseUrl, fetchImpl);
  const probes = [liveness, readiness, publicHealth];
  const rows: RowState[] = [
    rowState(liveness),
    rowState(readiness, readinessUnavailable),
    rowState(publicHealth),
  ];
  const settled = probes.filter((probe) => !probe.isPending);
  const overall = overallState(
    rows,
    settled.length === probes.length ? probes.map((probe) => probe.error) : [],
  );
  const fetching = probes.some((probe) => probe.isFetching);
  const updatedAt = Math.max(...probes.map((probe) => probe.dataUpdatedAt || probe.errorUpdatedAt));
  const retry = () => {
    for (const probe of probes) {
      void probe.refetch();
    }
  };

  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-16 sm:py-24">
      <p className="text-xs font-semibold tracking-[0.18em] text-primary uppercase">{t.eyebrow}</p>
      <h1 className="mt-3 text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
        {t.heading}
      </h1>
      <p className="mt-4 max-w-prose text-muted-foreground text-pretty">{t.lede}</p>

      <div
        role="status"
        aria-live="polite"
        className="mt-10 flex items-center gap-3 rounded-lg border bg-card px-5 py-4"
      >
        <span aria-hidden="true" className={cn("size-2.5 rounded-full", OVERALL_DOT[overall])} />
        <span className="font-medium">{OVERALL_TEXT[overall]}</span>
        {updatedAt > 0 && (
          <span className="ml-auto text-xs text-muted-foreground tabular-nums">
            {t.lastChecked} {formatTime(updatedAt)}
          </span>
        )}
      </div>

      {overall === "unreachable" ? (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>{t.unreachableTitle}</CardTitle>
            <CardDescription>
              {t.unreachableBody} <span className="font-mono">{baseUrl}</span>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={retry} disabled={fetching}>
              {fetching ? t.retrying : t.retry}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card className="mt-6">
          <CardContent className="pt-6">
            {settled.length === 0 ? (
              <SkeletonRows />
            ) : (
              <ul className="divide-y">
                <ProbeRow
                  label={t.rows.liveness.label}
                  detail={t.rows.liveness.detail}
                  state={rows[0] ?? "loading"}
                  error={liveness.error}
                >
                  {liveness.data && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t.version} <span className="font-mono">{liveness.data.version}</span>
                    </p>
                  )}
                </ProbeRow>
                <ProbeRow
                  label={t.rows.readiness.label}
                  detail={t.rows.readiness.detail}
                  state={rows[1] ?? "loading"}
                  error={readiness.error}
                >
                  {readiness.data?.failing && readiness.data.failing.length > 0 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t.failingLabel}:{" "}
                      <span className="font-mono">{readiness.data.failing.join(", ")}</span>
                    </p>
                  )}
                  {readiness.data && <ChecksTable readiness={readiness.data} />}
                </ProbeRow>
                <ProbeRow
                  label={t.rows.publicHealth.label}
                  detail={t.rows.publicHealth.detail}
                  state={rows[2] ?? "loading"}
                  error={publicHealth.error}
                />
              </ul>
            )}
          </CardContent>
        </Card>
      )}
    </main>
  );
}
