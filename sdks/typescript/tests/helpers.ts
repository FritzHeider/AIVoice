import { setupServer } from "msw/node";

import { VoiceAgentsClient, type ClientOptions } from "../src/client.js";
import { RUNTIME, type InternalClientOptions } from "../src/internal.js";

export const BASE_URL = "http://api.test";

export const server = setupServer();

export interface TestClient {
  client: VoiceAgentsClient;
  sleeps: number[];
}

/** A client whose retry waits are recorded instead of slept and whose jitter draw is fixed. */
export function testClient(options: ClientOptions = {}, random = 0.5): TestClient {
  const sleeps: number[] = [];
  const internal: InternalClientOptions = {
    baseURL: BASE_URL,
    apiKey: "va_test_key",
    ...options,
    [RUNTIME]: {
      sleep: (ms: number) => {
        sleeps.push(ms);
        return Promise.resolve();
      },
      random: () => random,
    },
  };
  return { client: new VoiceAgentsClient(internal), sleeps };
}
