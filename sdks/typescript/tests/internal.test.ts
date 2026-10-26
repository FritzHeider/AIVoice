import { afterEach, describe, expect, it, vi } from "vitest";

import {
  defaultRuntime,
  resolveRuntime,
  RUNTIME,
  type InternalClientOptions,
} from "../src/internal.js";

afterEach(() => {
  vi.useRealTimers();
});

describe("runtime", () => {
  it("sleeps on the timer queue", async () => {
    vi.useFakeTimers();
    let done = false;
    const pending = defaultRuntime.sleep(250).then(() => {
      done = true;
    });
    await vi.advanceTimersByTimeAsync(249);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await pending;
    expect(done).toBe(true);
  });

  it("merges overrides onto the defaults", () => {
    const random = () => 0.25;
    const options: InternalClientOptions = { [RUNTIME]: { random } };
    const runtime = resolveRuntime(options);
    expect(runtime.random).toBe(random);
    expect(runtime.sleep).toBe(defaultRuntime.sleep);
    expect(resolveRuntime({})).toEqual(defaultRuntime);
  });
});
