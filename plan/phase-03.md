# Phase 03 — Vendor Clients and Simulators

- [ ] P03-T01 Simulator framework and scenario DSL
  - Spec refs: specs/30-testing-and-evals.md (simulators, scenario DSL)
  - Depends on: P00-T12
  - Deliverables: `va_sim.server` (multi-app runner on ports 9100–9400), `va_sim.scenario` (pydantic models for the YAML DSL, loader, validation errors with line numbers), `va_sim.clock` (real-time and accelerated), control API (`/__sim/scenarios`, `/__sim/calls/{id}/recording`, `/__sim/reset`)
  - Acceptance: DSL examples from specs/30 load; invalid documents produce precise errors.

- [ ] P03-T02 FakeTelnyx REST and webhook emitter
  - Spec refs: specs/30-testing-and-evals.md (FakeTelnyx); specs/06-telephony-telnyx.md (REST operations, webhook envelope, signature)
  - Depends on: P03-T01
  - Deliverables: endpoints for calls (dial) and every call action used, conferences, call control applications, outbound voice profiles, messaging profiles, available numbers, number orders, phone numbers, messages; Ed25519 keypair generation with public key written to `.sim/telnyx_public_key`; signed webhook delivery with retries; scenario-driven call progress (ringing, answered, busy, no answer, voicemail AMD results, hangup causes)
  - Acceptance: responses validate against the response models in specs/06; signatures verify with an independent pynacl check.

- [ ] P03-T03 FakeTelnyx media WebSocket client
  - Spec refs: specs/30-testing-and-evals.md (FakeTelnyx media); specs/06-telephony-telnyx.md (media WS protocol)
  - Depends on: P03-T02
  - Deliverables: on answer, connects to `stream_url` with signature headers; sends `connected`, `start`, `media` frames (μ-law 20 ms) from scenario audio (WAV files or synthesized marker audio), `dtmf`, `stop`; accepts outbound `media`, `mark`, `clear`; echoes marks per Telnyx semantics; records agent audio per call; supports out-of-order frame injection for tests
  - Acceptance: loopback test with a minimal test WS server proves framing, mark echo after playback, clear behaviour.

- [ ] P03-T04 FakeElevenLabs TTS (WS multi-context + REST)
  - Spec refs: specs/30-testing-and-evals.md (FakeElevenLabs); specs/09-tts-elevenlabs.md (WS protocol, REST convert)
  - Depends on: P03-T01
  - Deliverables: `/v1/text-to-speech/{voice_id}/multi-stream-input` WS honoring contexts, flush, close_context, close_socket, keepalive, inactivity timeout, chunk_length_schedule buffering, deterministic μ-law/PCM audio (60 ms per character) with per-chunk alignment; REST `/v1/text-to-speech/{voice_id}` and `/stream`
  - Acceptance: protocol tests incl. interleaved contexts and close_context discarding pending audio.

- [ ] P03-T05 FakeElevenLabs STT (realtime WS + batch) and voices REST
  - Spec refs: specs/30-testing-and-evals.md (FakeElevenLabs STT marker scheme); specs/08-stt-elevenlabs.md; specs/15-voices.md (vendor endpoints)
  - Depends on: P03-T01
  - Deliverables: `/v1/speech-to-text/realtime` (session_started, partial_transcript, committed_transcript(_with_timestamps), error types), marker-driven transcript mapping, `previous_text` first-chunk rule, `/v1/speech-to-text` batch with diarization, `/v2/voices`, `/v1/voices/add`, `/v1/voices/{id}`, `/v1/voices/{id}/edit`, `/v1/voices/{id}/settings`, delete
  - Acceptance: error message types reproducible via scenario flags; batch output shape matches spec.

- [ ] P03-T06 FakeLLM (OpenAI-compatible) and embeddings
  - Spec refs: specs/30-testing-and-evals.md (FakeLLM rules file); specs/10-llm-gateway.md
  - Depends on: P03-T01
  - Deliverables: `/v1/chat/completions` (stream and non-stream, tool calls incl. forced `tool_choice`, usage), configurable TTFT/tokens-per-second, error injection (429, 500, timeout), `/v1/embeddings` deterministic normalized 1024-dim vectors, `/v1/models`
  - Acceptance: the official `openai` Python SDK works against it unmodified (test).

- [ ] P03-T07 FakeStripe
  - Spec refs: specs/30-testing-and-evals.md (FakeStripe); specs/24-billing-and-limits.md (Stripe objects and webhooks)
  - Depends on: P03-T01
  - Deliverables: customers, checkout sessions, payment intents (off-session), subscriptions, billing portal sessions, invoices, webhook emission with `Stripe-Signature`
  - Acceptance: the official `stripe` Python SDK works against it with `api_base` override (test).

- [ ] P03-T08 `va_telnyx` REST client
  - Spec refs: specs/06-telephony-telnyx.md (REST client operations, error mapping, retries, rate limiting, client_state, command_id)
  - Depends on: P03-T02
  - Deliverables: `TelnyxClient` with typed methods for every operation, per-account credentials (platform or BYO), `command_id` support, error mapping, retry on 429/5xx for idempotent operations, token-bucket rate limiter, metrics/tracing
  - Acceptance: contract tests against FakeTelnyx for every method; respx tests for error mapping.

- [ ] P03-T09 `va_telnyx` webhooks and media protocol models
  - Spec refs: specs/06-telephony-telnyx.md (webhook verification, envelope, media WS protocol)
  - Depends on: P03-T08
  - Deliverables: `verify_webhook(raw_body, headers, public_key, now)`, `verify_media_ws(stream_url, headers, public_key, now)`, event payload models for every voice and messaging event type, media message models (inbound and outbound) with parsing that tolerates unknown fields
  - Acceptance: fixed test vectors; 5-minute tolerance; tampered body rejected.

- [ ] P03-T10 `va_elevenlabs` TTS WebSocket client
  - Spec refs: specs/09-tts-elevenlabs.md (WS client, contexts, keepalive, reconnection, alignment)
  - Depends on: P03-T04
  - Deliverables: `TtsSocket` (connect once per call, `open_context`, `send_text`, `flush`, `close_context`, keepalive task, async iterator of `AudioChunk(context_id, audio, alignment)`), reconnection policy
  - Acceptance: contract tests against FakeElevenLabs incl. reconnection mid-utterance.

- [ ] P03-T11 `va_elevenlabs` STT WebSocket client
  - Spec refs: specs/08-stt-elevenlabs.md (client, messages, errors, reconnection, keyterms, language mapping)
  - Depends on: P03-T05
  - Deliverables: `SttSession` (connect with query params, `send_audio(frame, commit=False)`, `commit()`, async iterator of `Partial/Committed` events), error classification, buffered reconnect
  - Acceptance: contract tests for every server message type and error type.

- [ ] P03-T12 `va_elevenlabs` REST client
  - Spec refs: specs/09-tts-elevenlabs.md (REST convert for phrase cache); specs/08-stt-elevenlabs.md (batch re-transcription); specs/15-voices.md (voices, cloning)
  - Depends on: P03-T05
  - Deliverables: `ElevenLabsRest` (tts_convert, list_voices v2 with pagination, add_voice multipart, edit_voice, get/update voice settings, delete_voice, batch_transcribe)
  - Acceptance: contract tests against FakeElevenLabs; multipart encoding verified.

- [ ] P03-T13 `va_llm` gateway: providers, streaming, fallback
  - Spec refs: specs/10-llm-gateway.md (provider registry, model roles, streaming, TTFT, fallback, circuit breaker, retries, token metering)
  - Depends on: P03-T06
  - Deliverables: `LlmGateway` with `stream_chat(role, messages, tools, temperature)` yielding deltas with timing, `complete(...)`, per-org provider overrides (encrypted API keys), fallback model policy, circuit breaker, usage accounting hooks
  - Acceptance: tests for TTFT measurement, fallback on 5xx/timeout, breaker open/half-open/close.

- [ ] P03-T14 `va_llm` structured outputs and embeddings
  - Spec refs: specs/10-llm-gateway.md (structured outputs via forced tool call, embeddings client)
  - Depends on: P03-T13
  - Deliverables: `structured(role, prompt_messages, schema: type[BaseModel])` with forced tool call, jsonschema validation, one repair retry; `Embeddings.embed(texts)` batching 64 with `dimensions=1024`
  - Acceptance: tests for valid, invalid-then-repaired, and unrecoverable outputs.

- [ ] P03-T15 Telnyx bootstrap script and sim service wiring
  - Spec refs: specs/06-telephony-telnyx.md (bootstrap script); specs/29-infrastructure.md (docker-compose sim service)
  - Depends on: P03-T08
  - Deliverables: `scripts/telnyx_bootstrap.py` (idempotent find-or-create by name, prints env lines), compose `sim` service with healthcheck, `.env` integration for sim public key
  - Acceptance: running twice against the simulator creates resources once.

- [ ] P03-T16 Live smoke tests for vendor clients
  - Spec refs: specs/30-testing-and-evals.md (live smoke tests)
  - Depends on: P03-T08, P03-T10, P03-T11, P03-T12, P03-T13, P03-T14
  - Deliverables: `tests_live/test_vendor_clients.py` (Telnyx: list numbers/profiles; ElevenLabs: list voices, short TTS over WS with alignment, Scribe realtime on a committed WAV fixture, batch STT; Telnyx Inference: streaming chat, forced tool call, embeddings with 1024 dims); skipped unless `LIVE_TESTS=1`
  - Acceptance: suite is collected and skipped in CI; documented run command in AGENTS.md.

- [ ] P03-T17 Event publisher
  - Spec refs: specs/19-events-and-webhooks.md (internal event model, publisher steps)
  - Depends on: P01-T10, P00-T08
  - Deliverables: `DomainEvent`, `EventType` enum (every type in the catalog), `EventPublisher` (seq via INCR, XADD with MAXLEN, PUBLISH live, write-behind `CallEventBuffer` with flush policy), consumer-group helper with XAUTOCLAIM
  - Acceptance: tests assert stream routing per type prefix, seq monotonicity under concurrency, buffer flush triggers.

## Phase exit gate
`make check` passes; sim service healthy; bootstrap script works against the simulator.
