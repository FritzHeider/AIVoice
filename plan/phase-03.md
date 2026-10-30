# Phase 03 — Vendor Clients and Simulators

- [x] P03-T01 Simulator framework and scenario DSL — va_sim settings/clocks/scenario DSL (source:line:col errors)/SimState//sim control API/running_sims + apps/sim wiring; lint+typecheck+placeholders clean, 3519 passed, cov 97.72% (va_sim 94-100%)
  - Spec refs: specs/30-testing-and-evals.md (simulators, scenario DSL)
  - Depends on: P00-T12
  - Deliverables: `va_sim.server` (multi-app runner on ports 9100–9400), `va_sim.scenario` (pydantic models for the YAML DSL, loader, validation errors with line numbers), `va_sim.clock` (real-time and accelerated), control API (`/sim/telnyx/scenarios`, `/sim/recordings/{id}.wav`, `/sim/reset`, `/sim/clock`, `/sim/state`, per-sim `/sim/{sim}/config`; specs/30 §3 Decision 2026-10-29)
  - Acceptance: DSL examples from specs/30 load; invalid documents produce precise errors.

- [x] P03-T02a FakeTelnyx REST resources, auth, and signed webhook emitter — Ed25519 keys + public key file, per-account Bearer auth, Telnyx-shaped errors, balance/apps/OVPs/messaging profiles/number search+orders/phone numbers/messages, signed webhook emitter with 1 s/3 s retries, duplicate mode, delivery log, supervised tasks; lint+typecheck+placeholders clean, 3615 passed, cov 97.87% (va_sim 99.3%)
  - Spec refs: specs/30-testing-and-evals.md §3, §3.2 (FakeTelnyx REST, auth, webhooks); specs/06-telephony-telnyx.md §2.1 (non-call operations), §4.1 (signature); specs/21-messaging-sms.md §2 step 6, §5 (message events)
  - Depends on: P03-T01
  - Deliverables: Ed25519 keypair (`SIM_ED25519_PRIVATE_KEY` or generated) with `GET /sim/telnyx/public-key` and the public key written to `.sim/telnyx_public_key` by `apps/sim`; Bearer auth (`sim-telnyx-key` + `POST /sim/telnyx/accounts`) with per-account resources; Telnyx-shaped errors; `GET /v2/balance`; call control applications, outbound voice profiles, messaging profiles (create/list with filters/get/patch); available numbers search over a seeded inventory; number orders (pending → success/failure after 1 s, `number_order.complete`); phone numbers (list/get/patch/messaging patch/delete); messages (`message.sent` +200 ms, `message.finalized` +600 ms, inbound `message.received` via control API); webhook emitter (envelope, `telnyx-signature-ed25519`/`telnyx-timestamp`, retries after 1 s and 3 s, duplicate mode, delivery log); supervised background tasks
  - Acceptance: responses validate against the Telnyx shapes of specs/30 §3.2 (test-local models); signatures verify with an independent pynacl check; retries and duplicates reproduced.

- [x] P03-T02b FakeTelnyx call control and scenario-driven call progress — dial/actions/AMD/transfer/bridge/recording/conferences/control API, ordered signed webhooks, waiting-leg cancel, off-loop recording mix; 3691 passed, cov 97.93 %, radon A (2.13), jscpd 0
  - Spec refs: specs/30-testing-and-evals.md §3.2 (call endpoints, AMD, inbound, control API), §3.5 (answer/transfer_target); specs/06-telephony-telnyx.md §2.1 (call operations), §5 (event types), §5.1 (hangup causes), §10 (transfer sequences)
  - Depends on: P03-T02a
  - Deliverables: `POST /v2/calls` (dial) and every call action used (answer, reject, hangup, transfer, bridge, streaming_start/stop, record_start/stop, send_dtmf, playback_start/stop), conferences (create/join/leave), `command_id` deduplication, `90018` for ended calls; scenario-driven progress (call.initiated, ringing → answered | busy | no answer | rejected | failed with the hangup causes of specs/06 §5.1), premium AMD events, `call.recording.saved` with sim recording URLs, transfer legs from `transfer_target`; inbound `POST /sim/telnyx/inbound`; control API `GET /sim/telnyx/calls`, `/sim/telnyx/calls/{ccid}/timeline`, `POST /sim/telnyx/calls/{ccid}/hangup`
  - Acceptance: event sequences per scenario outcome asserted in accelerated clock; responses validate against the Telnyx shapes; webhooks signed as in P03-T02a.

- [x] P03-T03a Synthetic caller audio (`va_sim.audio`) — codec/marker/formant synth/DTMF, utterance_seed + 16-bit cap; 3880 passed, coverage 97.96 %, lint/typecheck/placeholders 0, jscpd 0 clones, radon audio A (1.91)
  - Spec refs: specs/30-testing-and-evals.md §3.1 (synthetic caller speech, utterance markers, DTMF, utterance registry); specs/07-media-pipeline.md (G.711 μ-law codec tables, L16 big-endian)
  - Depends on: P03-T01
  - Deliverables: numpy G.711 μ-law codec and L16 helpers built independently in the simulator; `synthesize_utterance` (180 ms sync + 16-bit FSK marker preamble, Rosenberg-pulse formant voiced body with ±3 % jitter, raised-cosine syllables, deterministic vowels, peaks −12/−6 dBFS) at 8 kHz μ-law and natively at 16 kHz PCM16 for hd_audio; `decode_marker` (Goertzel sync search then bits); `synthesize_dtmf`; `SimState.utterance_seed` and a 16-bit cap on utterance ids
  - Acceptance: the marker decoder recovers 100 % of 20 seeded random ids after μ-law round trip, after resampling to 16 kHz, from native 16 kHz audio, and behind random leading silence plus −40 dBFS noise; codec known vectors and full-range round trip; synthesis deterministic per seed. The Silero detection requirement of §3.1 (`test_synth_detected_by_silero`) is verified in P04-T03 once `SileroVad` exists.

- [x] P03-T03b FakeTelnyx media WebSocket client (transport, playout clock, agent audio) — lint/typecheck/placeholders 0, make test 3964 passed (coverage 97.94 %), 22 loopback media session tests + format/frames/playout units
  - Spec refs: specs/30-testing-and-evals.md §3.2 (media WS client, clock, control API agent audio/timeline); specs/06-telephony-telnyx.md §4.2 (stream signature), §8 (media WS protocol)
  - Depends on: P03-T02b, P03-T03a
  - Deliverables: when a stream starts (dial/answer `stream_url` on answer, or `streaming_start`) a supervised media session connects to `stream_url` with Ed25519 signature headers (and `Authorization: Bearer <stream_auth_token>`); sends `connected`, `start` (media_format from the stream codec/rate), `media` frames every 20 ms on the sim clock (160-byte PCMU or 640-byte L16 16 kHz) from a caller audio queue, silence when idle, `dtmf`, `stop` on streaming_stop/hang-up; accepts outbound `media`, `mark`, `clear` with a virtual playout clock (mark echo when played, `clear` drops queued audio and echoes pending marks), Telnyx `error` frames for malformed input; caller and agent tracks feed recordings; `streaming.failed` on connect failure or peer close; jitter mode (adjacent frames swapped) and raw frame injection; control API `POST /sim/telnyx/calls/{ccid}/say`, `/dtmf`, `/media/inject`, `GET .../agent_audio.wav`; timeline utterances with first agent audio
  - Acceptance: loopback test with a minimal test WS server proves signature headers, framing, mark echo after playback, clear behaviour, jitter order, L16 16 kHz, recording tracks, and timeline latency fields.

- [ ] P03-T03c FakeTelnyx scenario caller runner
  - Spec refs: specs/30-testing-and-evals.md §3.2 (media WS client), §3.5 (steps, greeting_audio, transfer_target.steps, barge_in_after_agent_ms)
  - Depends on: P03-T03b
  - Deliverables: per-call runner started when the media session is up: `greeting_audio` (text, or a `.wav` path relative to the scenario file), then the scenario `steps` (`wait_agent_speech` with an energy detector over played-out agent audio, `say` incl. `barge_in_after_agent_ms`, `dtmf` incl. `w` pauses and in-band tones, `silence`, `expect_hangup`, `hangup`); transfer legs run `transfer_target.steps`; step outcomes (ok / timeout) in the call summary and timeline
  - Acceptance: accelerated-clock tests with a scripted test WS agent cover every step type, timeouts, barge-in timing, and caller hang-up.

- [ ] P03-T04 FakeElevenLabs TTS (WS multi-context + REST)
  - Spec refs: specs/30-testing-and-evals.md (FakeElevenLabs); specs/09-tts-elevenlabs.md (WS protocol, REST convert)
  - Depends on: P03-T01
  - Deliverables: `/v1/text-to-speech/{voice_id}/multi-stream-input` WS honoring contexts, flush, close_context, close_socket, keepalive, inactivity timeout, chunk_length_schedule buffering, deterministic μ-law/PCM audio (60 ms per character) with per-chunk alignment; REST `/v1/text-to-speech/{voice_id}` and `/stream`
  - Acceptance: protocol tests incl. interleaved contexts and close_context discarding pending audio.

- [ ] P03-T05 FakeElevenLabs STT (realtime WS + batch) and voices REST
  - Spec refs: specs/30-testing-and-evals.md (FakeElevenLabs STT marker scheme); specs/08-stt-elevenlabs.md; specs/15-voices.md (vendor endpoints)
  - Depends on: P03-T01, P03-T03a (`decode_marker`)
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
  - Depends on: P03-T02b
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
  - Deliverables: `scripts/telnyx_bootstrap.py` (idempotent find-or-create by name, prints env lines), compose `sim` service with healthcheck, `.env` integration for sim public key, Redis-backed SimState sharing when `SIM_STATE_REDIS_URL` is set
  - Acceptance: running twice against the simulator creates resources once; two sim processes see the same registered scenario.

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
