/**
 * Styles injected into the widget's Shadow DOM root. Every colour is a CSS variable so the theme from the widget
 * config (specs/22 §3) can override it on the host. The defaults keep text contrast at or above 4.5:1 in both schemes.
 */
export const WIDGET_CSS = `
:host {
  all: initial;
  position: fixed;
  inset: auto 20px 20px auto;
  z-index: 2147483000;
  --va-accent: #4338ca;
  --va-accent-hover: #3730a3;
  --va-on-accent: #ffffff;
  --va-ring: #1e1b4b;
  --va-surface: #ffffff;
  --va-shadow: 0 1px 2px rgb(15 23 42 / 0.12), 0 8px 24px -6px rgb(15 23 42 / 0.28);
  --va-radius: 999px;
  --va-ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --va-duration: 180ms;
  font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :host {
    --va-accent: #a5b4fc;
    --va-accent-hover: #c7d2fe;
    --va-on-accent: #1e1b4b;
    --va-ring: #e0e7ff;
    --va-surface: #0f172a;
  }
}
.launcher {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 52px;
  padding: 0 20px 0 16px;
  border: 0;
  border-radius: var(--va-radius);
  background: var(--va-accent);
  color: var(--va-on-accent);
  box-shadow: var(--va-shadow);
  font: 600 15px/1 inherit;
  letter-spacing: 0.01em;
  cursor: pointer;
  transition: transform var(--va-duration) var(--va-ease-out),
    background-color var(--va-duration) var(--va-ease-out);
}
.launcher:hover {
  background: var(--va-accent-hover);
  transform: translateY(-1px);
}
.launcher:active {
  transform: translateY(0) scale(0.98);
}
.launcher:focus-visible {
  outline: 3px solid var(--va-ring);
  outline-offset: 3px;
}
.launcher svg {
  width: 22px;
  height: 22px;
  flex: none;
}
:host([data-open="true"]) .launcher {
  background: var(--va-accent-hover);
}
@media (prefers-reduced-motion: reduce) {
  .launcher {
    transition: none;
  }
  .launcher:hover,
  .launcher:active {
    transform: none;
  }
}
`;
