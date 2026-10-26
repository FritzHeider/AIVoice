import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/**
 * Status badges keep the text in `--foreground` and carry the status hue in a dot, so contrast never depends on the
 * status colour itself.
 */
export const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap before:size-1.5 before:rounded-full before:content-['']",
  {
    variants: {
      tone: {
        neutral: "before:bg-muted-foreground",
        success: "before:bg-success",
        warning: "before:bg-warning",
        danger: "before:bg-danger",
        info: "before:bg-info",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeProps = ComponentProps<"span"> & VariantProps<typeof badgeVariants>;

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span data-slot="badge" className={cn(badgeVariants({ tone }), className)} {...props} />;
}
