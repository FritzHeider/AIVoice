/**
 * Mounting: a `<div id="va-widget-root">` appended to `body` hosts an open Shadow DOM root that holds the widget's
 * styles and launcher, isolated from host-page CSS (specs/22 §4). All text is set via `textContent`.
 */
import { validateOptions, type WidgetOptions } from "./config.js";
import { WIDGET_CSS } from "./styles.js";

export const ROOT_ID = "va-widget-root";
export const LAUNCHER_LABEL = "Chat with us";

const SVG_NS = "http://www.w3.org/2000/svg";
const WAVE_PATH = "M4 12h2M8 8v8M12 5v14M16 8v8M20 12h-2";

export interface WidgetHandle {
  readonly widgetKey: string;
  readonly host: HTMLDivElement;
  readonly root: ShadowRoot;
  readonly isOpen: boolean;
  open(): void;
  close(): void;
  toggle(): void;
  destroy(): void;
}

function waveIcon(doc: Document): SVGSVGElement {
  const svg = doc.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = doc.createElementNS(SVG_NS, "path");
  path.setAttribute("d", WAVE_PATH);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  svg.append(path);
  return svg;
}

function buildLauncher(doc: Document): HTMLButtonElement {
  const button = doc.createElement("button");
  button.type = "button";
  button.className = "launcher";
  button.setAttribute("part", "launcher");
  button.setAttribute("aria-label", LAUNCHER_LABEL);
  button.setAttribute("aria-expanded", "false");
  const label = doc.createElement("span");
  label.textContent = LAUNCHER_LABEL;
  button.append(waveIcon(doc), label);
  return button;
}

class MountedWidget implements WidgetHandle {
  readonly widgetKey: string;
  readonly host: HTMLDivElement;
  readonly root: ShadowRoot;
  readonly userToken: string | undefined;
  #open = false;
  readonly #launcher: HTMLButtonElement;

  constructor(options: WidgetOptions, doc: Document) {
    this.widgetKey = options.widgetKey;
    this.userToken = options.userToken;
    this.host = doc.createElement("div");
    this.host.id = ROOT_ID;
    this.root = this.host.attachShadow({ mode: "open" });
    const style = doc.createElement("style");
    style.textContent = WIDGET_CSS;
    this.#launcher = buildLauncher(doc);
    this.#launcher.addEventListener("click", () => {
      this.toggle();
    });
    this.root.append(style, this.#launcher);
    doc.body.append(this.host);
    this.#render(options.open === true);
  }

  get isOpen(): boolean {
    return this.#open;
  }

  open(): void {
    this.#render(true);
  }

  close(): void {
    this.#render(false);
  }

  toggle(): void {
    this.#render(!this.#open);
  }

  destroy(): void {
    this.host.remove();
    if (current === this) {
      current = null;
    }
  }

  #render(open: boolean): void {
    this.#open = open;
    this.host.dataset.open = String(open);
    this.#launcher.setAttribute("aria-expanded", String(open));
  }
}

let current: MountedWidget | null = null;

/**
 * Mount the widget (idempotent): a second call with the same key returns the existing handle; a different key
 * replaces the mounted widget.
 */
export function init(options: WidgetOptions): WidgetHandle {
  validateOptions(options);
  const doc = options.target ?? document;
  if (current !== null && current.host.isConnected && current.host.ownerDocument === doc) {
    if (current.widgetKey === options.widgetKey) {
      return current;
    }
    current.destroy();
  }
  current = new MountedWidget(options, doc);
  return current;
}
