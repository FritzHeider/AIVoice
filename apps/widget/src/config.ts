/** Embed configuration read from the `<script data-*>` attributes (specs/22 §4). */

export const WIDGET_KEY_PREFIX = "wgt_";

export interface WidgetOptions {
  /** Public widget key (`wgt_…`) from the widget's embed code. */
  widgetKey: string;
  /** Host-signed visitor JWT (specs/22 §5.3). */
  userToken?: string;
  /** Open the panel immediately after mounting. */
  open?: boolean;
  /** Document to mount into; defaults to the global `document`. */
  target?: Document;
}

export class WidgetConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WidgetConfigError";
  }
}

export function validateOptions(options: WidgetOptions): void {
  const key = options.widgetKey;
  if (!key.startsWith(WIDGET_KEY_PREFIX) || key.length === WIDGET_KEY_PREFIX.length) {
    throw new WidgetConfigError(`widgetKey must start with "${WIDGET_KEY_PREFIX}"`);
  }
}

/** Options from an embed `<script>`, or `null` when it carries no `data-widget-key`. */
export function readScriptConfig(script: HTMLOrSVGScriptElement | null): WidgetOptions | null {
  const widgetKey = script?.dataset.widgetKey;
  if (script === null || widgetKey === undefined || widgetKey === "") {
    return null;
  }
  const options: WidgetOptions = { widgetKey, open: script.dataset.open === "true" };
  const userToken = script.dataset.userToken;
  if (userToken !== undefined && userToken !== "") {
    options.userToken = userToken;
  }
  return options;
}
