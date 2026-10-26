import { useQueryClient } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import RouteError from "@/app/error";
import NotFound from "@/app/not-found";
import { makeQueryClient, Providers } from "@/app/providers";
import StatusPage, { generateMetadata } from "@/app/status/page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { en } from "@/i18n/en";

describe("not-found", () => {
  it("links to the status page", () => {
    render(<NotFound />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(en.notFound.title);
    expect(screen.getByRole("link", { name: en.notFound.action })).toHaveAttribute(
      "href",
      "/status",
    );
  });
});

describe("error boundary", () => {
  it("shows the digest and retries", async () => {
    const reset = vi.fn();
    render(
      <RouteError error={Object.assign(new Error("x"), { digest: "abc123" })} reset={reset} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(`${en.error.reference} abc123`);
    await userEvent.click(screen.getByRole("button", { name: en.error.retry }));
    expect(reset).toHaveBeenCalledOnce();
  });

  it("omits the reference without a digest", () => {
    render(<RouteError error={new Error("x")} reset={vi.fn()} />);
    expect(screen.getByRole("alert")).not.toHaveTextContent(new RegExp(`${en.error.reference}\\s`));
  });
});

describe("status page", () => {
  it("sets the page title and renders the status view", () => {
    expect(generateMetadata()).toEqual({ title: en.status.title });
    vi.stubGlobal("fetch", () => new Promise<Response>(() => undefined));
    render(
      <Providers>
        <StatusPage />
      </Providers>,
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(en.status.heading);
    vi.unstubAllGlobals();
  });
});

describe("providers", () => {
  it("uses the spec query defaults", () => {
    const defaults = makeQueryClient().getDefaultOptions().queries;
    expect(defaults).toMatchObject({ staleTime: 30_000, refetchOnWindowFocus: true });
  });

  it("provides one client for the subtree", () => {
    const seen: unknown[] = [];
    function Probe() {
      seen.push(useQueryClient());
      return null;
    }
    const { rerender } = render(
      <Providers>
        <Probe />
      </Providers>,
    );
    rerender(
      <Providers>
        <Probe />
      </Providers>,
    );
    expect(seen[0]).toBe(seen[1]);
  });
});

describe("ui primitives", () => {
  it("renders a button as its child element", () => {
    render(
      <Button asChild variant="link">
        <a href="/status">go</a>
      </Button>,
    );
    expect(screen.getByRole("link", { name: "go" }).dataset.slot).toBe("button");
  });

  it("renders a badge with its tone", () => {
    render(<Badge tone="danger">bad</Badge>);
    expect(screen.getByText("bad").className).toContain("before:bg-danger");
  });
});
