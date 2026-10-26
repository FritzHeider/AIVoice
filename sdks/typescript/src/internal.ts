/**
 * Runtime seams used by the client for waiting between retries and drawing jitter. Not re-exported from the package
 * entry point: tests pass `InternalClientOptions` to the client constructor to make backoff deterministic.
 */
import type { ClientOptions } from "./client.js";

export const RUNTIME: unique symbol = Symbol("voiceagents.runtime");

export interface Runtime {
  sleep: (ms: number) => Promise<void>;
  random: () => number;
}

export interface InternalClientOptions extends ClientOptions {
  [RUNTIME]?: Partial<Runtime>;
}

export const defaultRuntime: Runtime = {
  sleep: (ms) =>
    new Promise((resolve) => {
      setTimeout(resolve, ms);
    }),
  random: Math.random,
};

export function resolveRuntime(options: ClientOptions): Runtime {
  const overrides = (options as InternalClientOptions)[RUNTIME];
  return { ...defaultRuntime, ...overrides };
}
