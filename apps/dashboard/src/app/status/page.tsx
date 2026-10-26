import type { Metadata } from "next";

import { StatusView } from "@/components/status/status-view";
import { en } from "@/i18n/en";

export function generateMetadata(): Metadata {
  return { title: en.status.title };
}

export default function StatusPage() {
  return <StatusView />;
}
