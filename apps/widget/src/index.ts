/**
 * `widget.js` entry point (specs/22 §4), built as an IIFE that exposes these exports on `window.VoiceAgentsWidget`.
 * Phase 00 scope: configuration, the Shadow DOM mount, and the launcher's open state; chat, voice, and the rest of the
 * JS API arrive with the widget phase. Loaded from an embed `<script data-widget-key="wgt_…">`, it mounts itself.
 */
import { readScriptConfig, type WidgetOptions } from "./config.js";
import { init } from "./mount.js";

export { readScriptConfig, WidgetConfigError, type WidgetOptions } from "./config.js";
export { init, LAUNCHER_LABEL, ROOT_ID, type WidgetHandle } from "./mount.js";

export const ERROR_EVENT = "va-widget:error";

function mountReporting(options: WidgetOptions, doc: Document): void {
  try {
    init({ ...options, target: doc });
  } catch (error) {
    doc.defaultView?.dispatchEvent(new CustomEvent(ERROR_EVENT, { detail: { error } }));
  }
}

/**
 * Mount from the embed script's `data-*` attributes once `body` exists. Failures are dispatched as a
 * `va-widget:error` event on `window` so the host page can observe them.
 */
export function autoInit(script: HTMLOrSVGScriptElement | null, doc: Document = document): boolean {
  const options = readScriptConfig(script);
  if (options === null) {
    return false;
  }
  if (doc.readyState === "loading") {
    doc.addEventListener(
      "DOMContentLoaded",
      () => {
        mountReporting(options, doc);
      },
      { once: true },
    );
  } else {
    mountReporting(options, doc);
  }
  return true;
}

if (typeof document !== "undefined") {
  autoInit(document.currentScript);
}
