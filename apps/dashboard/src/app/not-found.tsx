import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { en } from "@/i18n/en";

const t = en.notFound;

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-3xl flex-col justify-center px-6 py-16">
      <p className="font-mono text-sm text-primary tabular-nums">{t.code}</p>
      <h1 className="mt-3 text-4xl font-semibold tracking-tight sm:text-5xl">{t.title}</h1>
      <p className="mt-4 max-w-prose text-muted-foreground text-pretty">{t.body}</p>
      <Link href="/status" className={buttonVariants({ className: "mt-8 self-start" })}>
        {t.action}
      </Link>
    </main>
  );
}
