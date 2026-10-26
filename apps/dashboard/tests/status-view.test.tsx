import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { StatusView } from "@/components/status/status-view";
import { en } from "@/i18n/en";

import { fakeFetch, json, LIVE, NOT_READY, READY, type Responder } from "./fakes";
import { renderWithQuery } from "./render";

const BASE = "http://api.test";
const t = en.status;

const healthy: Record<string, Responder> = {
  "/healthz": () => json(LIVE),
  "/readyz": () => json(READY),
  "/v1/health": () => json({ status: "ok" }),
};

function liveRegion(): HTMLElement {
  return screen.getByRole("status");
}

describe("StatusView", () => {
  it("shows a skeleton and checking state before the first answer", () => {
    const { fetch } = fakeFetch({
      "/healthz": () => new Promise<Response>(() => undefined),
      "/readyz": () => new Promise<Response>(() => undefined),
      "/v1/health": () => new Promise<Response>(() => undefined),
    });
    renderWithQuery(<StatusView baseUrl={BASE} fetchImpl={fetch} />);
    expect(liveRegion()).toHaveTextContent(t.checking);
    expect(liveRegion()).toHaveAttribute("aria-live", "polite");
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("renders every probe as operational", async () => {
    const { fetch } = fakeFetch(healthy);
    renderWithQuery(<StatusView baseUrl={BASE} fetchImpl={fetch} />);
    await waitFor(() => {
      expect(liveRegion()).toHaveTextContent(t.allOperational);
    });
    expect(liveRegion()).toHaveTextContent(t.lastChecked);
    expect(screen.getByText("1.2.3")).toBeTruthy();
    const table = screen.getByRole("table");
    expect(within(table).getAllByText(t.checkStatus.ok)).toHaveLength(3);
    expect(within(table).getByText("postgres")).toBeTruthy();
  });

  it("lists failing dependencies from a 503 readiness body", async () => {
    const { fetch } = fakeFetch({ ...healthy, "/readyz": () => json(NOT_READY, 503) });
    renderWithQuery(<StatusView baseUrl={BASE} fetchImpl={fetch} />);
    await waitFor(() => {
      expect(liveRegion()).toHaveTextContent(t.degraded);
    });
    expect(screen.getByText("redis, temporal")).toBeTruthy();
    const table = screen.getByRole("table");
    expect(within(table).getByText(t.checkStatus.timeout)).toBeTruthy();
    expect(within(table).getByText(t.checkStatus.error)).toBeTruthy();
    expect(screen.getByText(t.rowStatus.unavailable)).toBeTruthy();
  });

  it("shows the request id of a failed probe", async () => {
    const { fetch } = fakeFetch({
      ...healthy,
      "/v1/health": () => json({}, 500, { "X-Request-Id": "req_oops" }),
    });
    renderWithQuery(<StatusView baseUrl={BASE} fetchImpl={fetch} />);
    await waitFor(() => {
      expect(screen.getByText(`${t.requestId} req_oops`)).toBeTruthy();
    });
    expect(screen.getByText(t.rowStatus.down)).toBeTruthy();
  });

  it("shows the unreachable state and retries on demand", async () => {
    let up = false;
    const routes: Record<string, Responder> = {};
    for (const [path, responder] of Object.entries(healthy)) {
      routes[path] = (request) => {
        if (!up) {
          throw new TypeError("connection refused");
        }
        return responder(request);
      };
    }
    const { fetch } = fakeFetch(routes);
    renderWithQuery(<StatusView baseUrl={BASE} fetchImpl={fetch} />);
    const retry = await screen.findByRole("button", { name: t.retry });
    expect(screen.getByText(BASE)).toBeTruthy();
    expect(liveRegion()).toHaveTextContent(t.unreachableTitle);
    up = true;
    await userEvent.click(retry);
    await waitFor(() => {
      expect(liveRegion()).toHaveTextContent(t.allOperational);
    });
  });
});
