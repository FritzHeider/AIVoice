## Phase 03 — Vendor Clients and Simulators (Telnyx, ElevenLabs, Telnyx Inference, Stripe)

### Goal
Build the vendor integration layer and its high-fidelity simulators together, so every later phase can be developed
and tested end-to-end without real vendors. Each client is contract-tested against its simulator; each simulator
reproduces the documented wire formats (REST JSON shapes, webhook envelopes and Ed25519 signatures, media/STT/TTS
WebSocket messages, SSE chat streaming). A gated live smoke suite proves the clients against the real vendors.

### Read first
- `specs/30-testing-and-evals.md` — simulator specification and scenario DSL (primary contract for `va_sim`).
- `specs/06-telephony-telnyx.md` — every Telnyx REST call, webhook format, signature, media WS protocol.
- `specs/08-stt-elevenlabs.md` and `specs/09-tts-elevenlabs.md` — ElevenLabs WS/REST contracts.
- `specs/10-llm-gateway.md` — LLM gateway, structured calls, embeddings.
- `specs/24-billing-and-limits.md` — Stripe objects used (for FakeStripe).
- `specs/02-repo-layout-and-conventions.md` §6 — base URL env vars that point at simulators.

### In scope
- `packages/va_sim`: server runner, scenario DSL loader/validator, FakeTelnyx (REST + webhook emitter with Ed25519 +
  media WS client), FakeElevenLabs (TTS multi-context WS, REST TTS, STT realtime WS, batch STT, voices/clone REST),
  FakeLLM (OpenAI-compatible streaming chat + tool calls + embeddings), FakeStripe; `apps/sim` runner and compose
  service; control API for tests (create scenario, inspect recorded agent audio/transcripts, fire events).
- `packages/va_telnyx`: REST client (all operations in specs/06), error mapping to `CARRIER_ERROR`, retries, client-side
  rate limiting, webhook verification, envelope/payload models, media protocol models.
- `packages/va_elevenlabs`: TTS multi-context WS client, STT realtime WS client, REST client.
- `packages/va_llm`: provider registry, streaming chat with TTFT, fallback and circuit breaker, forced-tool structured
  outputs with schema validation and repair retry, embeddings client, token accounting.
- `scripts/telnyx_bootstrap.py` (idempotent creation of Call Control App, outbound voice profile, messaging profile;
  works against the simulator and real Telnyx).
- Live smoke tests (`tests_live/`, marker `live`, skipped unless `LIVE_TESTS=1`).
- `packages/va_events` publisher core (`DomainEvent`, `EventType`, `EventPublisher`, call-event write-behind buffer,
  consumer-group helper) so phases 04–05 emit events through the real bus; webhooks routing arrives in phase 06.
  Read `specs/19-events-and-webhooks.md` §1–§2 for this.

### Phase-specific constraints
- Simulators must be deterministic (seeded) and support an accelerated clock mode for fast tests and a real-time mode
  for latency measurements.
- Client and simulator share no code paths for message construction (otherwise contract tests are meaningless):
  simulators build wire messages from the spec independently.
- Every vendor client method has: timeout, retry policy (where idempotent), structured error, metrics, and tracing.

### Phase exit gate
`make check` passes; `make up` runs the `sim` service healthy; `uv run python scripts/telnyx_bootstrap.py` succeeds
against the simulator and prints the created ids.
