"use client";

import { Button } from "@/components/ui/button";
import { en } from "@/i18n/en";

const t = en.error;

export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main
      role="alert"
      className="mx-auto flex min-h-dvh w-full max-w-3xl flex-col justify-center px-6 py-16"
    >
      <h1 className="text-3xl font-semibold tracking-tight">{t.title}</h1>
      <p className="mt-4 max-w-prose text-muted-foreground text-pretty">{t.body}</p>
      {error.digest !== undefined && (
        <p className="mt-2 font-mono text-xs text-muted-foreground">
          {t.reference} {error.digest}
        </p>
      )}
      <Button className="mt-8 self-start" onClick={reset}>
        {t.retry}
      </Button>
    </main>
  );
}
