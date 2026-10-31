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

- [x] P03-T03c FakeTelnyx scenario caller runner — CallerScript/ScriptSteps (greeting text/WAV, wait_agent_speech, say+barge-in, dtmf/inband, silence, expect_hangup, hangup, transfer_target), hold_clock prep, script in summary/timeline; lint/typecheck/placeholders 0, make test 4012 passed, radon no new C
  - Spec refs: specs/30-testing-and-evals.md §3.2 (media WS client), §3.5 (steps, greeting_audio, transfer_target.steps, barge_in_after_agent_ms)
  - Depends on: P03-T03b
  - Deliverables: per-call runner started when the media session is up: `greeting_audio` (text, or a `.wav` path relative to the scenario file), then the scenario `steps` (`wait_agent_speech` with an energy detector over played-out agent audio, `say` incl. `barge_in_after_agent_ms`, `dtmf` incl. `w` pauses and in-band tones, `silence`, `expect_hangup`, `hangup`); transfer legs run `transfer_target.steps`; step outcomes (ok / timeout) in the call summary and timeline
  - Acceptance: accelerated-clock tests with a scripted test WS agent cover every step type, timeouts, barge-in timing, and caller hang-up.

- [x] P03-T04 FakeElevenLabs TTS (WS multi-context + REST) — va_sim.elevenlabs WS/REST/control; make lint/typecheck/placeholders 0, make test 4109 passed, coverage 97.95 %, radon new code ≤ B
  - Spec refs: specs/30-testing-and-evals.md (FakeElevenLabs); specs/09-tts-elevenlabs.md (WS protocol, REST convert)
  - Depends on: P03-T01
  - Deliverables: `/v1/text-to-speech/{voice_id}/multi-stream-input` WS honoring contexts, flush, close_context, close_socket, keepalive, inactivity timeout, chunk_length_schedule buffering, deterministic μ-law/PCM audio (60 ms per character) with per-chunk alignment; REST `/v1/text-to-speech/{voice_id}` and `/stream`
  - Acceptance: protocol tests incl. interleaved contexts and close_context discarding pending audio.

- [x] P03-T05a FakeElevenLabs STT realtime WebSocket — va_sim+apps/sim 771 passed (98% va_sim cov); make lint/typecheck/placeholders 0; make test 4174 passed, 24 apps/api pytest-timeout setup errors (known fix:api-test-timeout-load; module 27 passed alone); spec refs 0; jscpd 0 clones
  - Spec refs: specs/30-testing-and-evals.md §3.3 (STT realtime, marker scheme); specs/08-stt-elevenlabs.md §2-§4, §10
  - Depends on: P03-T01, P03-T03a (`decode_marker`), P03-T04 (`va_sim.elevenlabs` package, auth, store)
  - Deliverables: `/v1/speech-to-text/realtime` (query validation, `session_started` config echo, `input_audio_chunk`
    decoding for every audio format, 20 ms energy segmentation, marker-driven transcript mapping with a scripted
    fallback queue and `[unintelligible]`, `partial_transcript` every 300 ms of voiced audio, manual and vad commits
    after `stt_final_ms` with `committed_transcript`, `committed_transcript_with_timestamps`,
    `committed_transcript_entities`, `previous_text` first-chunk rule, `input_error`/`chunk_size_exceeded`), STT
    fault injection for every §4 error `message_type` plus disconnect, session records, control routes
    `/sim/elevenlabs/stt/sessions`, `/stt/fault`, `/stt/transcripts`, reset closes sockets 1012
  - Acceptance: protocol tests over real sockets with synthesized caller audio (μ-law 8 kHz and PCM 16 kHz) recover
    the registered utterance text; every error message type reproducible via the fault control route.
  - Split from P03-T05 (2026-10-29, Planner): realtime STT alone is a TTS-sized task.

- [x] P03-T05b FakeElevenLabs batch STT — `POST /v1/speech-to-text` (file / cloud_storage_url / source_url, multichannel separate|combined, diarize, granularity, entities, keyterms, enable_logging), marker + TTS-tone decoding, batch records/fault/reset; dual-channel acceptance test; make test 4262 passed, coverage 98.01 %, lint/typecheck/placeholders clean, jscpd 0
  - Spec refs: specs/30-testing-and-evals.md §3.3 (REST `POST /v1/speech-to-text`); specs/08-stt-elevenlabs.md §8
  - Depends on: P03-T05a, P03-T04 (TTS request records for agent-channel text), P03-T02b (recordings)
  - Deliverables: `POST /v1/speech-to-text` (multipart `file` or `cloud_storage_url`/`source_url`, `model_id`,
    `use_multi_channel`, `multichannel_output_style` separate|combined, `diarize`, `timestamps_granularity`
    none|word|character, `language_code`, `keyterms`, `entity_detection`, query `enable_logging`), caller-channel
    marker decoding and agent-channel tone-character decoding resolved against TTS records, word timestamps,
    `speaker_id`/`channel_index`, entities, batch request records, batch fault route, reset
  - Acceptance: batch output shape matches the ElevenLabs contract and reconstructs both sides of a FakeTelnyx
    dual-channel recording (caller utterance text from markers, agent text from TTS tones).
  - Split from P03-T05b (2026-10-29, Planner): batch STT and the voice registry are separate TTS-sized tasks.

- [x] P03-T05c FakeElevenLabs voices REST and subscription — 11 premade voices (Karen + 10 labelled), `/v2/voices` paging/search/category/sort, add (WAV/MP3/FLAC measured, < 1 s `invalid_sample`), get/edit/settings/settings-edit/delete, TTS WS+REST `voice_not_found`, `/v1/user/subscription`, voices fault/records/reset; make test 4377 passed, coverage 98.06 %, lint/typecheck/placeholders clean, jscpd 0
  - Spec refs: specs/30-testing-and-evals.md §3.3 (REST voices, subscription); specs/15-voices.md §2, §3.2-§3.4, §7
  - Depends on: P03-T05b
  - Deliverables: voice registry seeded with premade voices (incl. `Karen` and 10 others with labels), `/v2/voices`
    (pagination, search, category), `/v1/voices/add`, `/v1/voices/{id}`, `/v1/voices/{id}/edit`,
    `/v1/voices/{id}/settings`, `/v1/voices/{id}/settings/edit`, `DELETE /v1/voices/{id}`, TTS `voice_not_found`
    for unknown voices (WS and REST), `GET /v1/user/subscription`
  - Acceptance: voice clone rejects samples < 1 s with `invalid_sample`; TTS with an unknown voice fails with
    `voice_not_found`; `/v2/voices` pages with `next_page_token` until `has_more=false`.

- [x] P03-T06a FakeLLM chat completions, rules engine, faults, and models — va_sim.llm (chat/rules/faults/models/control), openai SDK acceptance tests; review re-run: lint/typecheck/placeholders pass, make test 4478 passed, coverage 98.13 %
  - Spec refs: specs/30-testing-and-evals.md §3 (env table, profiles), §3.4 (chat, rules engine, default dialogue,
    `/v1/models`, control API), §10; specs/10-llm-gateway.md §2, §2.1, §12
  - Depends on: P03-T01
  - Deliverables: `va_sim.llm` package; `POST /v1/chat/completions` (Bearer auth, OpenAI error bodies, request
    validation, stream and non-stream, `stream_options.include_usage` usage chunk, `[DONE]`, tool calls from rules
    incl. forced `tool_choice`, `finish_reason` stop/tool_calls/length via `max_tokens`), TTFT + tokens-per-second
    pacing on the sim clock with per-rule `ttft_ms`/`delay_ms`, default dialogue reply, YAML rules engine
    (`SIM_LLM_RULES` files loaded at startup, `POST/GET/DELETE /sim/llm/rules`, priority + registration order,
    `system_contains`, `last_user_regex`, `tool_name`, `model`, `call_index`, `call_id` via `X-VA-Call-Id`),
    error injection (rule `error`, `/sim/llm/fault` for 429 with `Retry-After`, 500/502/503, timeout before headers or
    stall after n tokens, malformed tool arguments), `GET /v1/models` from the env-configured model ids,
    `/sim/llm/requests` log, reset
  - Acceptance: the official `openai` Python SDK works against it unmodified for streaming text, non-streaming, and
    streamed tool-call delta assembly (test); every fault kind reproducible via the control route.
  - Split from P03-T06 (2026-10-31, Planner): chat + rules + faults alone is a TTS-sized task.

- [ ] P03-T06b FakeLLM structured-call heuristics and embeddings
  - Spec refs: specs/30-testing-and-evals.md §3.4 (built-in structured-call heuristics, embeddings); specs/10-llm-gateway.md §3, §9, §10, §12
  - Depends on: P03-T06a
  - Deliverables: forced-tool heuristics for every specs/10 §3 registered function name (router `select_path`, loop
    `score_condition`, `extract_variables`, `classify_answer`, `evaluate_guard_rails`, disposition, summary, analysis
    schema / citations, `grade` evals, the remaining names with schema-valid defaults) producing arguments valid
    against the request's tool `parameters` schema; `POST /v1/embeddings` (string or list input, `dimensions`
    default 1024, blake2b signed-bucket L2-normalized vectors, `encoding_format` float|base64, usage)
  - Acceptance: `openai` SDK forced tool call returns schema-valid heuristic arguments for each registered name;
    embeddings are deterministic, unit-norm, 1024-d by default, and similar texts score higher cosine (test).

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
  - Depends on: P03-T05a
  - Deliverables: `SttSession` (connect with query params, `send_audio(frame, commit=False)`, `commit()`, async iterator of `Partial/Committed` events), error classification, buffered reconnect
  - Acceptance: contract tests for every server message type and error type.

- [ ] P03-T12 `va_elevenlabs` REST client
  - Spec refs: specs/09-tts-elevenlabs.md (REST convert for phrase cache); specs/08-stt-elevenlabs.md (batch re-transcription); specs/15-voices.md (voices, cloning)
  - Depends on: P03-T05b, P03-T05c
  - Deliverables: `ElevenLabsRest` (tts_convert, list_voices v2 with pagination, add_voice multipart, edit_voice, get/update voice settings, delete_voice, batch_transcribe)
  - Acceptance: contract tests against FakeElevenLabs; multipart encoding verified.

- [ ] P03-T13 `va_llm` gateway: providers, streaming, fallback
  - Spec refs: specs/10-llm-gateway.md (provider registry, model roles, streaming, TTFT, fallback, circuit breaker, retries, token metering)
  - Depends on: P03-T06a
  - Deliverables: `LlmGateway` with `stream_chat(role, messages, tools, temperature)` yielding deltas with timing, `complete(...)`, per-org provider overrides (encrypted API keys), fallback model policy, circuit breaker, usage accounting hooks
  - Acceptance: tests for TTFT measurement, fallback on 5xx/timeout, breaker open/half-open/close.

- [ ] P03-T14 `va_llm` structured outputs and embeddings
  - Spec refs: specs/10-llm-gateway.md (structured outputs via forced tool call, embeddings client)
  - Depends on: P03-T13, P03-T06b
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
