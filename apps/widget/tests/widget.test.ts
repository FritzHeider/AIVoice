import { afterEach, describe, expect, it, vi } from "vitest";

import {
  autoInit,
  ERROR_EVENT,
  init,
  LAUNCHER_LABEL,
  readScriptConfig,
  ROOT_ID,
  WidgetConfigError,
} from "../src/index.js";

function script(attrs: Record<string, string>): HTMLScriptElement {
  const el = document.createElement("script");
  for (const [name, value] of Object.entries(attrs)) {
    el.setAttribute(name, value);
  }
  return el;
}

function launcher(root: ShadowRoot): HTMLButtonElement {
  const button = root.querySelector("button");
  if (button === null) {
    throw new Error("launcher missing");
  }
  return button;
}

afterEach(() => {
  document.getElementById(ROOT_ID)?.remove();
  vi.restoreAllMocks();
});

describe("init", () => {
  it("mounts a shadow root on a host appended to body", () => {
    const handle = init({ widgetKey: "wgt_abc" });
    const host = document.getElementById(ROOT_ID);
    expect(host).toBe(handle.host);
    expect(host?.parentElement).toBe(document.body);
    expect(handle.root.mode).toBe("open");
    expect(host?.shadowRoot).toBe(handle.root);
  });

  it("renders an accessible launcher", () => {
    const button = launcher(init({ widgetKey: "wgt_abc" }).root);
    expect(button.type).toBe("button");
    expect(button.getAttribute("aria-label")).toBe(LAUNCHER_LABEL);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.textContent).toBe(LAUNCHER_LABEL);
    expect(button.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("keeps styles inside the shadow root", () => {
    const handle = init({ widgetKey: "wgt_abc" });
    expect(handle.root.querySelector("style")?.textContent).toContain("prefers-reduced-motion");
    expect(handle.root.querySelector("style")?.textContent).toContain("prefers-color-scheme: dark");
    expect(document.head.querySelector("style")).toBeNull();
  });

  it("toggles open state from the launcher", () => {
    const handle = init({ widgetKey: "wgt_abc" });
    const button = launcher(handle.root);
    button.click();
    expect(handle.isOpen).toBe(true);
    expect(handle.host.dataset.open).toBe("true");
    expect(button.getAttribute("aria-expanded")).toBe("true");
    handle.close();
    expect(handle.isOpen).toBe(false);
    handle.open();
    handle.toggle();
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens immediately when open is set", () => {
    const handle = init({ widgetKey: "wgt_abc", open: true });
    expect(handle.isOpen).toBe(true);
    expect(handle.host.dataset.open).toBe("true");
  });

  it("is idempotent for the same key", () => {
    const first = init({ widgetKey: "wgt_abc" });
    expect(init({ widgetKey: "wgt_abc" })).toBe(first);
    expect(document.querySelectorAll(`#${ROOT_ID}`)).toHaveLength(1);
  });

  it("remounts for a different key", () => {
    const first = init({ widgetKey: "wgt_abc" });
    const second = init({ widgetKey: "wgt_xyz" });
    expect(second).not.toBe(first);
    expect(first.host.isConnected).toBe(false);
    expect(second.widgetKey).toBe("wgt_xyz");
    expect(document.querySelectorAll(`#${ROOT_ID}`)).toHaveLength(1);
  });

  it("mounts again after destroy", () => {
    const first = init({ widgetKey: "wgt_abc" });
    first.destroy();
    expect(document.getElementById(ROOT_ID)).toBeNull();
    const second = init({ widgetKey: "wgt_abc" });
    expect(second).not.toBe(first);
    first.destroy();
    expect(second.host.isConnected).toBe(true);
  });

  it.each(["", "wgt_", "abc", "WGT_abc"])("rejects widget key %j", (widgetKey) => {
    expect(() => init({ widgetKey })).toThrow(WidgetConfigError);
    expect(document.getElementById(ROOT_ID)).toBeNull();
  });

  it("mounts into an explicit target document", () => {
    const other = document.implementation.createHTMLDocument("host");
    const handle = init({ widgetKey: "wgt_abc", target: other });
    expect(other.getElementById(ROOT_ID)).toBe(handle.host);
    expect(document.getElementById(ROOT_ID)).toBeNull();
  });
});

describe("readScriptConfig", () => {
  it("reads every data attribute", () => {
    const el = script({
      "data-widget-key": "wgt_k",
      "data-user-token": "jwt",
      "data-open": "true",
    });
    expect(readScriptConfig(el)).toEqual({ widgetKey: "wgt_k", userToken: "jwt", open: true });
  });

  it("defaults open to false and omits an empty token", () => {
    const el = script({ "data-widget-key": "wgt_k", "data-user-token": "", "data-open": "yes" });
    expect(readScriptConfig(el)).toEqual({ widgetKey: "wgt_k", open: false });
  });

  it.each([[null], [script({})], [script({ "data-widget-key": "" })]])(
    "ignores scripts without a key",
    (el) => {
      expect(readScriptConfig(el)).toBeNull();
    },
  );
});

describe("autoInit", () => {
  it("mounts from the embed script", () => {
    expect(autoInit(script({ "data-widget-key": "wgt_auto", "data-open": "true" }))).toBe(true);
    const host = document.getElementById(ROOT_ID);
    expect(host?.dataset.open).toBe("true");
  });

  it("does nothing without a widget key", () => {
    expect(autoInit(null)).toBe(false);
    expect(document.getElementById(ROOT_ID)).toBeNull();
  });

  it("dispatches an error event for an invalid key", () => {
    const seen: unknown[] = [];
    const listener = (event: Event) => {
      seen.push((event as CustomEvent<{ error: unknown }>).detail.error);
    };
    window.addEventListener(ERROR_EVENT, listener);
    autoInit(script({ "data-widget-key": "bad" }));
    window.removeEventListener(ERROR_EVENT, listener);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toBeInstanceOf(WidgetConfigError);
  });

  it("waits for DOMContentLoaded while the document is loading", () => {
    vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
    autoInit(script({ "data-widget-key": "wgt_late" }));
    expect(document.getElementById(ROOT_ID)).toBeNull();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(document.getElementById(ROOT_ID)).not.toBeNull();
  });
});
