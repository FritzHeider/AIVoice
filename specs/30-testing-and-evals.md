# 30 — Testing Strategy, Simulators, End-to-End Flows, Load, Live Smoke, Voice Evals

## 1. Test pyramid and locations

| Layer | Location | Runner | Dependencies | Target count / share |
|---|---|---|---|---|
| Unit | `packages/*/tests/unit`, `apps/*/tests/unit` | `pytest -n auto` | none (pure, fakes) | ~70 % of tests, < 60 s total |
| Integration | `packages/*/tests/integration`, `apps/*/tests/integration` | pytest + testcontainers | Postgres(pgvector), Redis, ClickHouse, MinIO, Temporal test env, in-process simulators | ~20 % |
| Contract | `packages/va_telnyx/tests/contract`, `packages/va_elevenlabs/tests/contract`, `sdks/*/tests` | pytest | recorded vendor response fixtures + OpenAPI | vendor payload shapes, SDK coverage |
| End-to-end (simulator) | `tests_e2e/` (repo root) | pytest `-m e2e` | docker compose `e2e` profile | §5 flows |
| UI e2e | `apps/dashboard/e2e/` | Playwright | compose `apps` profile | `25` §24 |
| Load | `scripts/load/` | k6 + Python swarm | compose or staging | §6 |
| Live smoke | `tests_live/` | pytest `-m live` with `LIVE_TESTS=1` | real Telnyx/ElevenLabs/Telnyx Inference | §7 |
| Voice evals | `evals/` | `uv run python -m evals.run` | recordings from e2e/live | §8 |

## 2. Conventions and fixtures

- Markers: `unit` (default), `integration`, `e2e`, `live`, `slow`, `load`. `pytest.ini` sets `asyncio_mode=auto`,
  `timeout=120` (e2e 600), `-p no:cacheprovider` in CI, `--strict-markers`.
- Shared fixtures in `packages/va_common/src/va_common/testing/` (importable by every package, excluded from coverage):
  - `pg_container` (session): `PostgresContainer("pgvector/pgvector:pg16")`, runs migrations once, creates roles.
  - `db` (function): transaction-per-test with SAVEPOINT rollback; `tenant_db(org)` sets `app.org_id`.
  - `redis_container` / `redis` (flushes db per test), `clickhouse_container`, `minio_container` (creates buckets).
  - `temporal_env`: `WorkflowEnvironment.start_time_skipping()`; `temporal_worker(env, workflows, activities)`.
  - `factories`: `OrgFactory`, `UserFactory`, `ApiKeyFactory` (returns plaintext), `PathwayFactory` (with node builders),
    `ToolFactory`, `KnowledgeBaseFactory`, `VoiceFactory`, `PhoneNumberFactory`, `CallFactory`, `BatchFactory`,
    `WidgetFactory`, `PersonaFactory`, `AgentFactory` — plain async functions inserting rows with sensible defaults.
  - `api_client(org, scopes=["*"])`: `httpx.AsyncClient(transport=ASGITransport(app))` with a real key.
  - `sims`: starts `FakeTelnyx`, `FakeElevenLabs`, `FakeLLM`, `FakeStripe` in-process on ephemeral ports (aiohttp-free:
    implemented with Starlette + uvicorn `Server` in a background task) and patches settings base URLs.
  - `frozen_clock` (freezegun) and `FakeClock` for audio pacing (`va_voice.clock.Clock` protocol with `RealClock` and
    `FakeClock` advancing deterministically).
- Golden/snapshot tests via syrupy for: prompt assembly outputs, Call Object serialization, webhook payloads, OpenAPI
  examples.
- Property tests (hypothesis): μ-law encode/decode round trip error bound, chunker never splits inside words, JSONPath
  mapping, template rendering, token bucket invariants, cursor pagination.
- Coverage: `--cov-fail-under=85` per package (configured with `[tool.coverage.report] fail_under` in each package and
  enforced in CI per package via `scripts/coverage_per_package.py`).

## 3. Simulators (`packages/va_sim`)

All simulators share a `SimState` (in-memory, optionally Redis-backed when run as the compose `sim` service so
multiple processes share it) and a control API under `/sim/*` on each port for tests to script and inspect.
Ed25519 test keypair: generated at sim startup unless `SIM_ED25519_PRIVATE_KEY` (base64) is provided; the public key
is served at `GET http://sim:9100/sim/telnyx/public-key` and `make up` writes it into `.env` as `TELNYX_PUBLIC_KEY`.

Simulator environment variables (read by `va_sim.settings.SimSettings`; realtime-mode defaults shown):

| Variable | Default | Meaning |
|---|---|---|
| `SIM_CLOCK` | `realtime` | `realtime` or `accelerated` (§3.2) |
| `SIM_STT_COMMIT_LATENCY_MS` | `60` | Delay from `commit` to `committed_transcript` (FakeElevenLabs STT) |
| `SIM_TTS_TTFA_MS` | `80` | First audio chunk latency per TTS context |
| `SIM_LLM_TTFT_MS` | `150` | FakeLLM time to first token |
| `SIM_LLM_TOKENS_PER_S` | `80` | FakeLLM streaming rate |
| `SIM_LLM_RULES` | empty | Comma-separated rule file paths (§3.4) |
| `SIM_ED25519_PRIVATE_KEY` | generated | Base64 Ed25519 private key for Telnyx signatures |
| `SIM_EDGE_WEBHOOK_URL` | `http://edge:8100/telnyx/voice` | Default voice webhook target |
| `SIM_MESSAGING_WEBHOOK_URL` | `http://edge:8100/telnyx/messaging` | Messaging webhook target |
| `SIM_STRIPE_WEBHOOK_URL` | `http://api:8000/v1/billing/stripe/webhook` | Stripe webhook target |
| `SIM_STATE_REDIS_URL` | empty | When set, `SimState` is shared via Redis (multi-process sims) |
| `SIM_WORKERS` | `1` | Worker processes for the sim server (load tests) |

The `fast` profile (default for unit/integration and fast e2e) overrides latencies to STT 10 ms, TTS 20 ms, LLM TTFT
20 ms, 1 000 tokens/s; the `realistic` profile sets STT 40 ms, TTS 250 ms, LLM TTFT 250 ms, 80 tokens/s. Control-API
config (`/sim/*/config`) overrides env values at runtime; values named elsewhere in this section (`stt_final_ms`,
`tts_ttfa_ms`, `ttft_ms`, `tokens_per_s`) are these same settings.

### 3.1 Synthetic caller speech and utterance markers (`va_sim.audio`)

- `synthesize_utterance(utterance_id: int, text: str, *, f0_hz=140, rate_ms_per_char=70, seed) -> bytes (μ-law 8 kHz)`:
  1. **Marker preamble (180 ms)**: 20 ms sync tone 1 700 Hz, then 16 bits × 10 ms FSK (bit 0 = 1 200 Hz, bit 1 = 2 200 Hz)
     encoding `utterance_id` big-endian, then 0 ms gap. Amplitude −12 dBFS.
  2. **Voiced body**: source-filter formant synthesis — Rosenberg glottal pulse train at F0 with ±3 % jitter, passed
     through three resonators per vowel (formant table: /a/ 730/1090/2440 Hz, /e/ 530/1840/2480, /i/ 270/2290/3010,
     /o/ 570/840/2410, /u/ 300/870/2240), one syllable per 180 ms with a raised-cosine amplitude envelope, vowel sequence
     chosen from the text's characters (deterministic), duration `len(text) * rate_ms_per_char` ms, peak −6 dBFS.
  3. Encoded to μ-law via the same codec as `va_voice.codec`.
- Requirement (unit test `test_synth_detected_by_silero`): Silero VAD (the production `SileroVad` with production
  thresholds) classifies ≥ 90 % of 32 ms windows in the voiced body as speech for 20 random utterances, and the FSK
  decoder recovers 100 % of ids after μ-law round trip and after resampling to 16 kHz (hd_audio path).
- `decode_marker(pcm16: np.ndarray, sample_rate) -> int | None`: Goertzel detection of sync then bits.
- Utterance registry: `SimState.utterances[utterance_id] = {"text", "language", "words": [...]}`; ids allocated
  sequentially per sim run.
- DTMF: `synthesize_dtmf(digit, ms=120)` for in-band tests; Telnyx-style `dtmf` WS events are also emitted.

### 3.2 FakeTelnyx (port 9100)

REST endpoints (base `/v2`), response bodies follow Telnyx shapes `{"data": {...}}` / errors
`{"errors":[{"code":"10015","title":"Invalid value","detail":"…","source":{"pointer":"/to"}}]}`:

| Endpoint | Behaviour |
|---|---|
| `POST /v2/calls` (Dial) | Validates required `connection_id`, `to`, `from`; returns `{"data":{"record_type":"call","call_control_id":"v3:<random>","call_leg_id":"<uuid>","call_session_id":"<uuid>","is_alive":false,"client_state":<echo>}}`; schedules scenario for `to` (§3.5) |
| `POST /v2/calls/{ccid}/actions/answer` | Inbound answer; starts media stream if `stream_url` given; emits `call.answered` |
| `POST /v2/calls/{ccid}/actions/reject` | `{cause}` → `call.hangup` (`hangup_cause: call_rejected`) |
| `POST /v2/calls/{ccid}/actions/hangup` | Emits `call.hangup` (`hangup_source: caller`→ for platform-initiated `hangup_cause: normal_clearing`, `hangup_source: callee` from scenario) |
| `POST /v2/calls/{ccid}/actions/transfer` | Creates leg B; scenario `transfer_target` decides answer/no-answer; emits `call.initiated` (B), `call.answered` (B), `call.bridged` (A and B) |
| `POST /v2/calls/{ccid}/actions/bridge` | `{call_control_id}` → `call.bridged` both |
| `POST /v2/calls/{ccid}/actions/streaming_start` / `streaming_stop` | Opens/closes media WS; `streaming.started` / `streaming.stopped` |
| `POST /v2/calls/{ccid}/actions/record_start` / `record_stop` | Starts/stops capturing both tracks; on stop or hangup emits `call.recording.saved` with `recording_urls: {"wav": "http://sim:9100/sim/recordings/<id>.wav", "mp3": "http://sim:9100/sim/recordings/<id>.mp3"}` and `channels: "dual"` |
| `POST /v2/calls/{ccid}/actions/send_dtmf` | Records digits in call log (asserted by tests) |
| `POST /v2/calls/{ccid}/actions/playback_start` / `playback_stop` | Records; emits `call.playback.started/ended` |
| `POST /v2/conferences` | `{call_control_id, name}` → conference object `{"data":{"id","name","record_type":"conference"}}`; `conference.created`, `conference.participant.joined` |
| `POST /v2/conferences/{id}/actions/join` / `leave` | participant events |
| `GET /v2/available_phone_numbers` | Supports `filter[country_code]`, `filter[national_destination_code]`, `filter[phone_number][contains]`, `filter[features][]`, `filter[limit]`; returns from a seeded inventory (`+1415555xxxx`, `+1212555xxxx`, `+1888555xxxx` toll-free, `+4420xxxxxxx`) with `{"phone_number","record_type":"available_phone_number","features":[{"name":"voice"},{"name":"sms"}],"cost_information":{"monthly_cost":"1.00","upfront_cost":"1.00","currency":"USD"},"region_information":[…]}` |
| `POST /v2/number_orders` | `{phone_numbers:[{phone_number}], connection_id, messaging_profile_id}` → order `status: pending` then after 1 s `success`, emits `number_order.complete` webhook; numbers become owned |
| `GET /v2/number_orders/{id}` | order status |
| `GET /v2/phone_numbers?filter[phone_number]=` / `PATCH /v2/phone_numbers/{id}` / `DELETE /v2/phone_numbers/{id}` / `PATCH /v2/phone_numbers/{id}/messaging` | owned numbers CRUD |
| `POST /v2/call_control_applications` / `GET` / `PATCH` | app CRUD (bootstrap and BYO) |
| `POST /v2/messaging_profiles` / `GET` / `PATCH` | messaging profiles |
| `POST /v2/outbound_voice_profiles` | profiles |
| `POST /v2/messages` | `{from,to,text,media_urls,messaging_profile_id,webhook_url}` → message `{"data":{"id","record_type":"message","direction":"outbound","to":[{"phone_number","status":"queued"}],"parts":1,…}}`; emits `message.sent` (+200 ms) and `message.finalized` with `delivered` (+600 ms) unless scenario says fail |
| `POST /v2/10dlc/brand`, `GET /v2/10dlc/brand/{id}`, `POST /v2/10dlc/campaignBuilder`, `GET /v2/10dlc/campaign/{id}`, `POST /v2/10dlc/phone_number_campaigns` | 10DLC flows with configurable approval delay; emits status webhooks |
| `GET /sim/recordings/{id}.wav` | 2-channel 8 kHz PCM16 WAV of the captured call |
| `GET /v2/ai/openai/*` | not served here (FakeLLM is separate) |

Behavioural rules:
- **Auth**: requires `Authorization: Bearer <key>`; key `sim-telnyx-key` (and any key registered via
  `POST /sim/telnyx/accounts` for BYO tests) else 401 Telnyx-shaped error.
- **Webhooks**: POSTed to the request `webhook_url` or the app's `webhook_event_url`, envelope
  `{"data":{"record_type":"event","id":"<uuid>","event_type":"call.answered","occurred_at":"…","payload":{…}},"meta":{"attempt":1,"delivered_to":"<url>"}}`
  with headers `telnyx-signature-ed25519` (base64 signature over `"{timestamp}|{raw body}"`) and `telnyx-timestamp`.
  Payload fields per event mirror Telnyx: `call_control_id`, `call_leg_id`, `call_session_id`, `connection_id`,
  `client_state`, `from`, `to`, `direction` (`incoming`/`outgoing`), `state`, `start_time`, plus `hangup_cause`
  (`normal_clearing`, `originator_cancel`, `timeout`, `time_limit`, `user_busy`, `not_found`, `no_answer`, `call_rejected`,
  `unspecified`), `hangup_source` (`caller`, `callee`, `unknown`), `sip_hangup_cause` for hangups; `result` for AMD
  events. Retries: if the receiver returns non-2xx, retry after 1 s and 3 s (then give up), reproducing duplicate
  deliveries for dedupe tests (`SimState.force_duplicate_webhooks=true` sends every webhook twice).
- **Media WS client**: on answer (dial with `stream_url`, or answer/streaming_start) the sim connects to `stream_url` with
  signed headers (`telnyx-signature-ed25519` over `"{timestamp}|{stream_url}"`), sends `connected`, `start` (with
  `media_format` matching `stream_codec`/`stream_bidirectional_codec` and sampling rate), then streams caller audio
  as `media` events every 20 ms (160-byte PCMU payloads, or 640-byte L16 16 kHz big-endian when hd_audio), with
  `chunk`/`timestamp`/`sequence_number`; silence frames (0xFF μ-law) when the scenario is idle. Optional jitter mode
  shuffles adjacent frames to test reordering. It receives agent `media` (RTP payload mode), appends to the agent
  track buffer paced by a virtual playout clock (20 ms per 160 bytes); `clear` drops queued-not-played agent audio
  immediately; `mark` is echoed when the playout clock passes the mark position (or immediately if queue empty), and
  a `clear` echoes all pending marks. Sends `dtmf` events for scenario DTMF and `stop` on hangup.
- **Clock**: `SIM_CLOCK=realtime` (20 ms wall per frame) or `accelerated` (frames sent as fast as the receiver reads,
  virtual timestamps advanced; the media worker must run with `FakeClock` injected via `VA_CLOCK=fake-shared` that reads
  virtual time from the sim's control API — only in e2e accelerated mode) — default e2e uses `realtime` for latency
  assertions and `accelerated` for long-flow tests (batch_100_calls, max_duration).
- **AMD**: when dial has `answering_machine_detection=premium`, after answer the scenario's `answered_by` drives
  `call.machine.premium.detection.ended` (`human_residence` | `human_business` | `machine` | `silence` | `fax_detected`)
  at +1 800 ms, and for machine with `beep_at_ms` emits `call.machine.premium.greeting.ended` (`beep_detected`) at that time.
- **Inbound**: `POST /sim/telnyx/inbound` `{to, from, scenario}` emits `call.initiated` (incoming) to the app webhook and
  waits for `answer`/`reject`/`transfer` commands.
- **Control API**: `POST /sim/telnyx/scenarios` (register scenario by destination number or id), `GET /sim/telnyx/calls`
  (all calls with command log, events sent, DTMF sent, transfers), `GET /sim/telnyx/calls/{ccid}/agent_audio.wav`,
  `GET /sim/telnyx/calls/{ccid}/timeline` (per-utterance timestamps: caller utterance start/end, first agent audio after
  each utterance → used to compute measured voice-to-voice latency), `POST /sim/telnyx/calls/{ccid}/hangup`,
  `POST /sim/reset`.

### 3.3 FakeElevenLabs (port 9200)

- **TTS multi-context WS** `GET /v1/text-to-speech/{voice_id}/multi-stream-input`: validates `xi-api-key` header (or
  `authorization` in first message), query `model_id`, `output_format` (`ulaw_8000`, `pcm_16000`, `pcm_22050`,
  `pcm_24000`, `pcm_44100`, `mp3_44100_128`), `sync_alignment`, `inactivity_timeout`. Per context: buffers text; generates
  audio when `flush: true`, on buffer length reaching `chunk_length_schedule[0]` (default 120 chars), or on
  `close_context`. Audio is deterministic: each character → 60 ms; characters map to a 300 Hz + (ord(c) mod 12) × 25 Hz
  tone (spaces = silence) at −10 dBFS, encoded in the requested format. Messages `{"audio": base64, "contextId": id,
  "alignment": {"chars": [...], "charStartTimesMs": [...], "charDurationsMs": [60,...]}, "normalizedAlignment": same}`,
  chunked in 200 ms audio pieces with alignment relative to each chunk; `{"isFinal": true, "contextId": id}` after
  close_context completes. First-chunk latency configurable (`SimState.tts_ttfa_ms` = `SIM_TTS_TTFA_MS`, default 80 ms; e2e "realistic"
  profile 250 ms). Inactivity timeout closes the socket with code 1008 and message
  `"Have not received a new text input within the timeout of 20 seconds."`. Records every context's text in
  `SimState.tts_requests` with timestamps and whether it was closed early (interrupted).
- **STT realtime WS** `GET /v1/speech-to-text/realtime`: query `model_id`, `audio_format` (`ulaw_8000`, `pcm_8000`,
  `pcm_16000`, `pcm_22050`, `pcm_24000`, `pcm_44100`, `pcm_48000`), `commit_strategy` (`manual`/`vad`), `include_timestamps`, `language_code`, `keyterms`,
  `entity_detection`. Sends `session_started` with config echo. For incoming `input_audio_chunk` messages it decodes audio,
  runs `decode_marker` on a sliding window to identify the current utterance id, and tracks voiced duration (energy
  threshold). While voiced audio flows it emits `partial_transcript` every 300 ms revealing the utterance's words in
  proportion to elapsed voiced time / expected duration. On `commit: true` it emits `committed_transcript` (and
  `committed_transcript_with_timestamps` with synthetic word timings when requested; `committed_transcript_entities` when
  `entity_detection` set — entities computed by regex for emails, digits sequences ≥ 13 as `pci`, names from
  `SimState.utterances[...].entities` if provided) for the text heard since the last commit, after `SimState.stt_final_ms`
  (`SIM_STT_COMMIT_LATENCY_MS`, default 60 ms). Commits with no voiced audio return `committed_transcript` with empty text. Unknown marker → returns
  text `"[unintelligible]"`. Error injection via `SimState.stt_fail_after_s` (sends `{"message_type":"transcriber_error","error":"…"}` and closes).
- **REST**: `GET /v2/voices` (seeded premade voices including one named `Karen` and 10 others with labels),
  `GET /v1/voices/{id}`, `GET /v1/voices/{id}/settings`, `POST /v1/voices/{id}/settings/edit`, `POST /v1/voices/add`
  (multipart `name`, `files[]`, `description`, `labels`, `remove_background_noise`; returns `{"voice_id": "<new>",
  "requires_verification": false}`; rejects files < 1 s with 400 `{"detail":{"status":"invalid_sample","message":"…"}}`),
  `POST /v1/voices/{id}/edit`, `DELETE /v1/voices/{id}`, `POST /v1/text-to-speech/{voice_id}` (non-streaming preview;
  same audio generator; `output_format` query), `POST /v1/speech-to-text` (batch: multipart `file` or `cloud_storage_url`,
  `model_id`, `diarize`, `timestamps_granularity`; for a 2-channel WAV produced by FakeTelnyx it decodes markers on the
  caller channel and tone-characters on the agent channel (reverse-maps tone frequency to character) to reconstruct both
  sides' text with word timestamps and `speaker_id` `speaker_0`/`speaker_1`), `GET /v1/user/subscription` (quota info).
- **Control API**: `/sim/elevenlabs/tts/requests`, `/sim/elevenlabs/stt/sessions`, `/sim/elevenlabs/config`
  (`tts_ttfa_ms`, `stt_final_ms`, failure injection), `/sim/reset`.

### 3.4 FakeLLM (port 9300, OpenAI-compatible at `/v1`)

- `POST /v1/chat/completions`: supports `model`, `messages`, `stream`, `tools`, `tool_choice`, `temperature`,
  `max_tokens`, `response_format`. Streaming SSE chunks `data: {"id","object":"chat.completion.chunk","created","model",
  "choices":[{"index":0,"delta":{"role"?, "content"?, "tool_calls"?},"finish_reason":null}]}` and final
  `finish_reason` (`stop`/`tool_calls`) plus a usage chunk (`stream_options.include_usage`), then `data: [DONE]`.
  Timing: first token after `ttft_ms` (`SIM_LLM_TTFT_MS`, default 150; realistic profile 250), then `tokens_per_s` (`SIM_LLM_TOKENS_PER_S`, default 80),
  tokens = whitespace-split words with trailing spaces.
- **Rules engine** (`va_sim.llm.rules`): YAML rule files loaded from `SIM_LLM_RULES` (path list) plus rules registered
  at runtime via `POST /sim/llm/rules`. First match wins (ordered by `priority` desc then registration order).

```yaml
rules:
  - id: greet-restaurant
    priority: 10
    match:
      system_contains: ["[[SCENARIO:restaurant]]"]      # all must be present in the system message(s)
      last_user_regex: "(?i)reservation|book a table"   # optional
      tool_name: null                                    # when set, matches forced tool_choice / presence of tool
      model: null
      call_index: null                                   # nth matching request (0-based) for sequencing
    respond:
      content: "Sure! For how many people?"              # streamed
      tool_calls: []                                     # [{name, arguments: {...}}]
      ttft_ms: 120                                       # optional override
      error: null                                        # {status: 500, body: {...}} to inject failures
      delay_ms: 0
```

- **Built-in structured-call heuristics** (used when no rule matches a forced tool call). Tool names are imported from
  `va_llm.structured.TOOL_NAMES` (defined in `10-llm-gateway.md`) so the simulator never drifts:
  - edge/route selection: score each option label by token overlap (lowercased, stopwords removed) with the last user
    message; choose max; ties → first; if max overlap = 0 choose the option flagged as fallback if present, else "none".
  - loop condition scoring: 8 if any condition keyword appears in the user text, otherwise 2.
  - variable extraction: per requested variable type — `integer`: first integer in text (words "one"–"twenty" mapped);
    `boolean`: true for yes/yeah/sure/correct/affirmative, false for no/nope/negative, else null; `string`: text after
    patterns "my name is", "this is", "I'm", "it's", or the whole utterance trimmed to 80 chars.
  - voicemail classification: true if text matches `(?i)leave (a|your) message|after the (tone|beep)|not available|voicemail`.
  - guard rail checks: opt-out true if `(?i)stop calling|do not call|remove me|unsubscribe`; custom guard rails false.
  - disposition: chooses the provided tag whose words overlap the transcript most, else `FOLLOW_UP_REQUIRED` if present.
  - summary: `"Summary: " + first 30 words of the transcript`.
  - analysis schema / citations: fills each field by type with the extraction heuristics, citing the first turn containing
    the value.
  - evals: score 80 for each criterion, rationale "Simulated evaluation.".
- **Default dialogue response** (no rule, not a tool call): `"Okay. You said: {last_user_message}."`, or for the first
  assistant turn with no user message `"Hello! How can I help you today?"`.
- `POST /v1/embeddings`: input string or list; returns `dimensions` (default 1024, honours request `dimensions`) floats
  per input: deterministic — tokens hashed (blake2b) into buckets with signed weights, L2-normalized (so similar texts
  share tokens → higher cosine), `usage.prompt_tokens` = word count.
- `GET /v1/models`: lists the env-configured model ids.
- Control API: `/sim/llm/requests` (full request log incl. messages for prompt assertions), `/sim/llm/rules`, `/sim/reset`.

### 3.5 Scenario DSL (YAML) — `tests_e2e/scenarios/*.yaml`

```yaml
# Schema (validated by va_sim.scenario.Scenario pydantic model)
id: outbound_basic                    # unique
description: Human answers, short exchange, agent ends call.
match:                                # how FakeTelnyx binds the scenario
  to: "+14155550101"                  # outbound destination, or
  inbound_from: null                  # for inbound scenarios started via /sim/telnyx/inbound
answer:
  after_ms: 1500                      # ring time; omit with `result` for non-answer outcomes
  result: answered                    # answered | busy | no_answer | rejected | failed
  answered_by: human_residence        # AMD result when AMD requested: human_residence|human_business|machine|silence|fax_detected
  beep_at_ms: null                    # machine: greeting beep time after answer
greeting_audio: null                  # optional utterance played right after answer (e.g. "Hello?")
llm_rules: [rules/outbound_basic.yaml]   # FakeLLM rule files to load for this scenario
steps:                                # executed sequentially by the caller simulator
  - wait_agent_speech: {min_ms: 300, then_silence_ms: 700, timeout_ms: 8000}   # agent spoke, then went quiet
  - say: {text: "Hi, yes this is Ana.", language: en}
  - wait_agent_speech: {then_silence_ms: 700}
  - say: {text: "Tomorrow at 3 works.", barge_in_after_agent_ms: null}         # set ms to start speaking while agent talks
  - dtmf: {digits: "1"}               # Telnyx dtmf event (and optionally in-band with `inband: true`)
  - silence: {ms: 2000}
  - expect_hangup: {by: agent, timeout_ms: 10000}   # or `hangup: {}` to have the caller hang up
transfer_target:                      # behaviour of transfer legs created from this call
  answer_after_ms: 2000
  result: answered
  steps: [{say: {text: "Hi, this is Bob from sales."}}]
profile: realistic                    # fast | realistic : vendor latency profile for the sims
assertions:                           # evaluated by the e2e harness after the call completes
  call_status: completed
  answered_by: human
  end_reason: agent_end_call
  min_turns: 4
  transcript_contains: ["Ana", "Tomorrow at 3"]
  variables: {name: "Ana"}
  max_e2e_ms_p95: 1200                # measured from sim timeline
  webhook_events: ["call.completed"]
```

Harness `tests_e2e/harness.py`: registers scenario + rules via control APIs, creates resources through the public API,
triggers the call, waits for `call.completed` on a local webhook receiver (`tests_e2e/receiver.py`, a Starlette app
recording deliveries with signature verification), then evaluates `assertions` plus test-specific checks.

## 4. Unit/integration requirements per package (minimum)

| Package | Must cover |
|---|---|
| va_common | settings parsing, ids, errors table parity, crypto, safe HTTP client, token bucket Lua via Redis |
| va_db | schema-matches-spec test, RLS, repositories, migrations up/down |
| va_telnyx | every client method request shape (respx) and error mapping; webhook signature verify (valid, tampered, stale); media message models round trip |
| va_elevenlabs | TTS WS client against FakeElevenLabs (contexts, flush, cancel/close_context, alignment accumulation, reconnect); STT WS client (partials, commits, errors, reconnect with buffering); REST methods |
| va_llm | streaming parse, tool call assembly from deltas, structured-call retries on invalid JSON, fallback model, embeddings batching |
| va_voice | codec, resampler, VAD wrapper, endpointing with adaptive thresholds, interruptibility levels 0–4, barge-in timing with FakeClock, playback tracker played-chars computation from marks + alignment, background mixer levels, jitter reorder buffer, latency tracer |
| va_engine | every node type, edges, global nodes with return stack, loop conditions, variables, templating, prompt assembly snapshots, task mode, transfer pathway, error paths |
| va_tools | tool execution (methods, headers, body templating, JSONPath response map, retries, timeouts), dynamic data caching, Deno sandbox (allowed, denied network, timeout kill, memory limit, bad output) |
| va_kb | parsers for each file type (fixtures in `packages/va_kb/tests/fixtures/`: sample.pdf, sample.docx, sample.html, sample.csv, sample.md, sample.txt), chunker token bounds, crawler (respx sites incl. sitemap index), hybrid retrieval ranking |
| va_analysis | summary/disposition/analysis/citations/evals with FakeLLM, schema validation, utterance citation mapping |
| va_compliance | DNC checks, calling windows across timezones and DST boundaries, guard rail config validation, redaction regexes |
| va_billing | rating per meter, holds/capture/release, ledger atomicity under concurrency (10 parallel tasks), auto-recharge trigger, Stripe webhook handling with FakeStripe |
| va_workflows | each workflow with time-skipping env (timers, signals, retries, child workflows, cancellation) |
| apps/api | every endpoint: happy path, validation errors, auth/scope, tenant isolation, envelope shape |
| apps/telephony_edge | webhook routing for every Telnyx event type, dedupe, inbound routing decisions |
| apps/media_worker | WS handshake auth, ownership lock, control messages, drain, full in-process call against sims (`FakeClock`) |
| apps/webhook_dispatcher | `19` §11 |
| apps/events_sink | consumer group semantics, batch insert, XAUTOCLAIM |

### 4.1 FakeStripe (port 9400)

Implements: `POST /v1/customers`, `POST /v1/checkout/sessions` (returns `url` = `http://sim:9400/sim/stripe/checkout/{id}`;
visiting it with `?outcome=success` completes the session and POSTs `checkout.session.completed` to
`SIM_STRIPE_WEBHOOK_URL` signed with `Stripe-Signature: t=…,v1=HMAC_SHA256(whsec_sim, "t.payload")`),
`POST /v1/billing_portal/sessions`, `POST /v1/payment_intents` (off-session auto-recharge; `SimState.stripe_decline=true`
returns `card_declined`), `GET /v1/payment_intents/{id}`, `POST /v1/subscriptions`, `POST /v1/subscriptions/{id}`
(update/cancel), `GET /v1/invoices/{id}`, events `invoice.paid`, `invoice.payment_failed`,
`customer.subscription.updated`, `payment_intent.succeeded`, `payment_intent.payment_failed`.

## 5. End-to-end flows (simulator, `tests_e2e/test_<flow>.py`)

Every flow runs against the compose `e2e` stack through the public API with a fresh org (created via the internal
seed helper `POST /internal/test/orgs` available only when `VA_ENV in (local,test)`), and asserts the listed items.

| Flow | Steps | Assertions |
|---|---|---|
| `outbound_basic` | Scenario above; `POST /v1/calls` with `task` prompt marked `[[SCENARIO:outbound_basic]]`, `first_sentence`, `record=true`, `webhook` | 200 response with call_id; `call.completed` webhook with Call Object fields of `19` §4; transcripts alternate roles; recording_url fetches a 2-channel WAV; latency events present for each turn; sim timeline p95 ≤ 1 200 ms (realistic profile) and platform overhead (e2e − sim vendor latencies) p95 ≤ 250 ms |
| `inbound_basic` | Buy number via API (FakeTelnyx inventory), set inbound config with pathway; `POST /sim/telnyx/inbound` | answered within 1 s of `call.initiated`; runner = pathway; first_sentence spoken first; call direction inbound |
| `voicemail_leave_message` | Scenario `answered_by: machine`, `beep_at_ms: 4000`; call with `voicemail: {message:"Hi, call us back", action:"leave_message"}` | agent audio starts after beep (≥ 4 000 ms after answer); TTS request text equals message; then hangup; `answered_by=voicemail`; `call.voicemail` event action `leave_message` |
| `voicemail_hangup` | machine; `voicemail.action=hangup` | hangup within 2 s of AMD result; no TTS dialogue; status completed, answered_by voicemail |
| `amd_ivr_mode` | `ivr_mode=true`, scenario with IVR prompts "Press 1 for sales" then DTMF expectations; `precall_dtmf_sequence="1w2"` | no voicemail action despite machine AMD; DTMF sent `1` then `2` with ≥ 500 ms gap (sim command log) |
| `barge_in` | Agent long first_sentence (300 chars); caller `say` with `barge_in_after_agent_ms: 1200` | `clear` sent within 120 ms of caller voice onset (sim timeline); assistant turn `interrupted=true`, `text_spoken` length ≈ 1.2 s / 60 ms chars ± 10 %; next LLM request history contains only spoken text |
| `interruptibility_levels` | Parametrized 0–4 with backchannel "uh-huh" (0.4 s) and a full sentence interruption | level 0: no interruption ever; 1: only interruptions ≥ 3 words and ≥ 1.2 s; 2: sentence interrupts, "uh-huh" doesn't; 3: ≥ 1 word interrupts; 4: "uh-huh" interrupts (rules per `07-media-pipeline.md`) |
| `cold_transfer` | Pathway with Transfer Call node; caller asks for a human | `transfer` command to configured number; `call.transferred`; transfer_seconds > 0 billed at transfer rate; AI stream stopped after final line mark echo |
| `warm_transfer` | Transfer node `transfer_type: warm` | second leg dialed with `link_to`; briefing TTS on leg B; conference created and both joined; AI removed; `warm_transfer_calls` in Call Object; billing split proxy leg vs merged |
| `pathway_restaurant_template` | Seed the restaurant template pathway (bundled JSON); scenario books a table for 4 at 7pm with name | route sequence equals expected node ids; variables `party_size=4`, `time="7pm"`, `name`; webhook node called the receiver with those variables; End Call speech played |
| `webhook_node` | Webhook node posting to receiver which returns `{"status":"confirmed","ref":"A1"}` with response_data mapping | `{{status}}` used in next agent utterance; `call.webhook_node` event with status 200 |
| `tool_call_with_speech` | Custom tool with `speech: "One moment while I check."` and receiver delaying 1.5 s | filler TTS played before tool response; tool event recorded; response var spoken |
| `kb_lookup` | KB from fixture text with a unique fact; KB node; caller asks question | KB retrieval event top hit is the fact chunk; LLM prompt (sim request log) contains chunk; answer content |
| `sms_conversation` | Number with SMS config; `POST /v1/sms/send` with objective; sim sends inbound replies via `/sim/telnyx/messages/inbound` | conversation created; replies processed by engine; outbound messages sent with statuses delivered; `conversation.message` webhooks; memory recent_messages updated |
| `widget_chat` | Create widget with allowed domain; public endpoints with `Origin` header | messages exchanged; disallowed origin → `WIDGET_DOMAIN_NOT_ALLOWED`; rate limit after N messages; billing `widget_message` usage |
| `widget_voice` | Widget voice WS with synthetic PCM 16 kHz utterances (same synth at 16 kHz) | transcript turns; agent audio frames returned; call row with `direction=web` |
| `batch_100_calls` | CSV of 100 numbers (10 DNC, 5 invalid), concurrency 20, accelerated clock | 85 dispatched; 10 skipped_dnc, 5 skipped_invalid; status webhook phases in order with correct counts; never > 20 concurrent (sim max simultaneous calls) |
| `scheduled_call` | `start_time` 2 min ahead (time-skipping in worker via Temporal test server? — no: e2e uses real Temporal; start_time 20 s ahead) | status `scheduled` then dials at ≥ start_time; stop before start cancels without dial |
| `retry_on_voicemail` | machine answer; `retry: {wait: 10, voicemail_action: "hangup"}` | second call created with `parent_call_id`, dialed ≥ 10 s later |
| `guard_rail_opt_out` | `guard_rails: [{type:"tcpa:opt_out", action:"end_call"}]`; caller says "stop calling me" | call ends; DNC entry created source `opt_out_voice`; subsequent call to the number → `NUMBER_BLOCKED` |
| `max_duration` | `max_duration: 1` (minute), accelerated | call ends at 60 s ± 1 s with `end_reason=max_duration` |
| `stop_via_api` | Long call; `POST /v1/calls/{id}/stop` at 10 s | hangup within 1 s; `ended_by=api` |
| `worker_crash_recovery` | Start call; `docker kill` the media container mid-call (harness restarts it) | workflow backstop hangs up within 30 s; `end_reason=worker_crash`; post-call completes with partial transcript |
| `recording_and_post_call` | record=true, summary_prompt, dispositions, analysis_schema | recording copied to MinIO; batch STT invoked; summary, disposition_tag in allowed set, analysis fields typed; `call.completed` after recording_ready |
| `citations_and_evals` | citation schema + eval setup on call | `citations.ready` and `evals.completed` webhooks; cited utterances reference real turn indices/timestamps |
| `memory_across_calls` | Persona with memory; call 1 learns name; call 2 to same number | call 2 system prompt (sim LLM log) includes memory summary/fact; memory_changes rows for call 1 |
| `experiment_assignment` | Agent with two versions, running experiment 50/50; 40 calls with `agent_id` only | both arms assigned (each ≥ 10), assignment deterministic per call id hash, `experiment_arm_id` stored |

(For `scheduled_call` the harness uses `start_time = now + 20 s` against real Temporal; durable-timer logic for long
delays is covered by Temporal time-skipping integration tests.)

## 6. Load testing (`scripts/load/`)

- **API load (k6)** `scripts/load/api.js`: stages ramp 0→200 VUs over 2 min, hold 5 min; mix: 40 % `GET /v1/calls`,
  20 % `GET /v1/calls/{id}`, 15 % `POST /v1/calls` (FakeTelnyx scenario that ends immediately busy), 10 % pathway GET,
  10 % voices list, 5 % analytics overview. Thresholds: `http_req_failed < 0.5%`, `p(95) < 300 ms` overall,
  `POST /v1/calls p(95) < 150 ms`.
- **Call swarm** `scripts/load/swarm.py`: asyncio program that (a) registers N scenarios on a dedicated sim cluster
  (sim runs with `SIM_WORKERS=8` processes sharing Redis state, each handling ~150 media streams), (b) creates a batch of
  N calls (or N parallel `POST /v1/calls`), (c) each scenario holds a 4-turn conversation over ~90 s with realistic
  vendor latency profile, (d) collects per-call sim timelines.
- Pass criteria for 1 000 concurrent calls across ≥ 20 media pods: zero dropped calls; audio underruns < 0.1 % of
  frames; platform overhead p95 ≤ 250 ms; e2e (with realistic sim vendor latency 250 ms LLM TTFT + 250 ms TTS TTFA +
  40 ms STT) p95 ≤ 1 300 ms; media pod event loop lag p99 < 50 ms; all `call.completed` webhooks delivered within 15 min;
  Postgres CPU < 70 %.
- **Rolling deploy under load**: during swarm, `helm upgrade` media with a new image tag; zero calls dropped (drain).
- Output: `scripts/load/report.py` writes `load-report.md` with percentiles and pass/fail; CI nightly on staging runs a
  200-concurrent variant.

## 7. Live smoke tests (`tests_live/`, `LIVE_TESTS=1`, staging)

Env required: `LIVE_TELNYX_API_KEY`, `LIVE_TELNYX_CONNECTION_ID`, `LIVE_FROM_NUMBER`, `LIVE_TEST_ANSWER_NUMBER` (a
Telnyx number owned by the test account routed to a second Call Control app run by the test harness acting as caller,
playing pre-synthesized ElevenLabs utterances via `playback_start`), `LIVE_ELEVENLABS_API_KEY`, `LIVE_LLM_API_KEY`.

| Test | Checks |
|---|---|
| `test_telnyx_webhook_signature_live` | Real Telnyx webhook verified with account public key |
| `test_outbound_call_live` | Platform calls `LIVE_TEST_ANSWER_NUMBER`; harness answers and plays 2 utterances; transcript contains them (WER ≤ 25 %); e2e p50 ≤ 1 000 ms measured from recording |
| `test_inbound_call_live` | Harness dials platform number; agent greets; call completes |
| `test_amd_live` | Harness answers playing a voicemail greeting with beep audio; platform detects machine and leaves message |
| `test_transfer_live` | Cold transfer to a second harness number; `call.bridged` received |
| `test_recording_live` | `call.recording.saved` URL downloaded and copied to S3 |
| `test_elevenlabs_tts_ws_live` | multi-context WS returns ulaw_8000 audio + alignment; close_context mid-stream stops audio |
| `test_elevenlabs_stt_ws_live` | streams a known WAV (generated once by ElevenLabs TTS, cached in `tests_live/fixtures`) → committed transcript WER ≤ 10 % |
| `test_elevenlabs_clone_live` | clone from fixture samples then delete |
| `test_telnyx_inference_live` | streaming chat completion with tool call; embeddings with `dimensions: 1024` return 1024 floats |
| `test_sms_live` | send SMS between two test numbers; `message.finalized` delivered |
| `test_number_search_live` | search returns inventory (no purchase) |

## 8. Voice quality evals (`evals/`)

- Input: recordings + transcripts from e2e realistic runs and live smoke runs (`evals/data/*.jsonl` index).
- Metrics:
  - **WER** of platform transcripts vs ground truth (scenario text) using `jiwer` after normalization (lowercase, strip
    punctuation, number normalization).
  - **Agent speech fidelity**: batch STT (ElevenLabs Scribe in live, FakeElevenLabs decode in sim) of the agent channel vs
    the text sent to TTS → WER (catches truncation/garbling).
  - **Latency distribution**: voice-to-voice computed from recording channel energy (caller speech end → agent speech
    start, 20 ms frames, energy threshold −45 dBFS) — independent of platform-reported numbers; compared to
    `call_turns.e2e_ms` (must agree within 60 ms median).
  - **Interruption correctness**: fraction of caller barge-ins honored within 300 ms.
  - **Turn-taking errors**: agent speaking over caller > 500 ms not followed by clear; premature EOT (agent started while
    caller continued within 400 ms).
- Thresholds (fail the eval job): live WER ≤ 15 %, agent fidelity WER ≤ 5 %, e2e p50 ≤ 900 ms / p95 ≤ 1 400 ms live,
  barge-in honored ≥ 95 %, premature EOT ≤ 5 % of turns.
- Output `evals/report.md` + JSON; nightly trend stored as CI artifact and pushed to ClickHouse table `va.eval_metrics`
  (`(run_id String, metric LowCardinality(String), value Float64, created_at DateTime64(3,'UTC'))`, see shared-contract
  additions).

## 9. CI matrix

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yml` | PR, push main | lint/type, python tests (Python 3.12 on ubuntu-latest; SDK tests additionally on 3.9 and 3.13), TS (Node 22), security, docs, helm, docker build (`29` §6.1) |
| `e2e.yml` | PR label `e2e`, push main, nightly | compose e2e all §5 flows (fast profile) + realistic subset (`outbound_basic`, `barge_in`, `interruptibility_levels`, `pathway_restaurant_template`) + Playwright |
| `load.yml` | nightly (staging), manual | 200-concurrent swarm + k6; weekly 1 000-concurrent |
| `live-smoke.yml` | nightly | §7 + §8 evals |
| `release.yml` | tags / main | `29` §6.3 |

Flaky test policy: a test failing intermittently is quarantined only with an issue link via
`@pytest.mark.flaky_quarantine(issue="#123")` (custom marker that still runs and reports but does not fail the job);
quarantine list reviewed weekly; max 5 quarantined tests.

## 10. Tests required

This spec defines the tests; additionally the simulators themselves have tests: protocol conformance of FakeTelnyx
media (frame sizes, mark/clear semantics), FakeElevenLabs (alignment sums equal audio duration), FakeLLM (SSE format
parseable by the `openai` SDK streaming client; tool-call deltas assemble correctly with the SDK), FakeStripe signature
verifiable by the official `stripe` library, synthetic speech VAD detection and marker decode (§3.1).

## 11. Acceptance criteria

- All §4 package suites green with ≥ 85 % coverage; all §5 flows green in CI on every push to main.
- Simulators are drop-in for the official clients (`openai`, `stripe`, our Telnyx/ElevenLabs clients) without code
  changes beyond base URLs.
- Load test 1 000 concurrent passes criteria in §6 at least once before GA and weekly thereafter.
- Live smoke suite green on staging for 7 consecutive nights before GA.
