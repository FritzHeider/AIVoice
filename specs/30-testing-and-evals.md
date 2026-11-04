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
| `SIM_EDGE_WEBHOOK_URL` | `http://localhost:8100/telnyx/voice` | Default voice webhook target |
| `SIM_MESSAGING_WEBHOOK_URL` | `http://localhost:8100/telnyx/messaging` | Messaging webhook target |
| `SIM_STRIPE_WEBHOOK_URL` | `http://localhost:8000/v1/billing/stripe/webhook` | Stripe webhook target |
| `SIM_STATE_REDIS_URL` | empty | When set, `SimState` is shared via Redis (multi-process sims) |
| `SIM_WORKERS` | `1` | Worker processes for the sim server (load tests) |

The `fast` profile (default for unit/integration and fast e2e) overrides latencies to STT 10 ms, TTS 20 ms, LLM TTFT
20 ms, 1 000 tokens/s; the `realistic` profile sets STT 40 ms, TTS 250 ms, LLM TTFT 250 ms, 80 tokens/s. Control-API
config (`/sim/*/config`) overrides env values at runtime; values named elsewhere in this section (`stt_final_ms`,
`tts_ttfa_ms`, `ttft_ms`, `tokens_per_s`) are these same settings.

> Decision (2026-10-29): the control API lives under `/sim/*` on every simulator port exactly as written in this
> section (the phase-03 plan's `/__sim/...` paths are superseded). Every simulator serves `POST /sim/reset`
> (optional JSON body `{"seed": <int>}` reseeds; without it the current seed is reused), `GET /sim/clock`
> (`{"mode", "now" (ISO-8601 UTC with `Z`), "virtual_ms"}`), and `GET /sim/state` (seed, clock, latencies, scenario
> ids, recording and utterance counts). `GET`/`PATCH /sim/elevenlabs/config` (`tts_ttfa_ms`, `stt_final_ms`) and
> `GET`/`PATCH /sim/llm/config` (`ttft_ms`, `tokens_per_s`) plus `POST /sim/{elevenlabs|llm}/config/profile`
> `{"profile": "fast"|"realistic"}` (applies all four latencies) are served only by the matching simulator; FakeTelnyx
> alone serves `/sim/telnyx/scenarios` (`POST` with an `application/yaml`/`text/yaml` or `application/json` body →
> 201 `{"id", "match"}`; `GET` list `{"scenarios": [...]}`; `GET /sim/telnyx/scenarios/{id}`) and
> `/sim/recordings/{id}.wav`. Errors are `{"error": {"code", "message", "issues"?}}` with codes `invalid_request`
> (422, `issues: [{path, line, column, message}]`), `not_found` (404), `unsupported_media_type` (415).
> `SimState` is seeded (`seed` 0 unless reset with another seed) and there is no `SIM_SEED` variable. The webhook
> URL defaults above are the specs/02 §6 values (the shared contract); the compose `sim` service sets the
> `edge:`/`api:` hosts (specs/29 §2). The state is in-process and shared by the four apps of one process; what
> `SIM_STATE_REDIS_URL` shares across processes is the P03-T15c Decision below. The
> library is the `va-sim` distribution (`packages/va_sim`); the runner `apps/sim` is distribution `va-sim-server`
> (module `va_sim_server`).

> Decision (2026-11-01): shared SimState (P03-T15c).
> 1. With `SIM_STATE_REDIS_URL` set, every sim process using that Redis shares exactly these control-plane records:
>    (a) registered scenarios, (b) latencies (`PATCH /sim/{elevenlabs|llm}/config` and `/config/profile`),
>    (c) reset + seed (`POST /sim/reset` on any process resets every process), (d) the utterance registry (ids
>    allocated once across processes, so a marker synthesized by process A's caller resolves in process B's
>    FakeElevenLabs STT, realtime or batch).
> 2. Everything else stays per process: FakeTelnyx accounts, applications, profiles, numbers, calls, conferences,
>    messages, webhook delivery logs, recordings, the FakeElevenLabs/FakeLLM/FakeStripe stores, faults, and the
>    clock. `GET /sim/state` reports the shared seed, scenarios, latencies and utterance count and the local counts
>    for the rest.
> 3. Keys (namespace `sim:state:`; the sim Redis is its own URL, so they never meet the specs/03 platform keys):
>    `sim:state:version` (INCR on every scenario, latency, or reset write), `sim:state:seed`, `sim:state:generation`
>    (INCR per reset), `sim:state:scenarios` (hash id → scenario JSON, paths already absolute),
>    `sim:state:scenario_order` (sorted set id → version of its last registration, so number bindings follow
>    registration order after a reload), `sim:state:latencies` (JSON), `sim:state:utterances` (hash id →
>    `{"text","language","words"}`), `sim:state:utterance_next` (counter). Channel `sim:state:events` (payload = the
>    new version; receivers just sync). No TTLs; a reset deletes them.
> 4. Consistency: every write is one Lua script (mutate + INCR version + PUBLISH), and reads use one `MULTI`, so
>    nobody sees half a write. Each process keeps a local copy (synchronous reads stay lock-free). `sync()` reads the
>    version and reloads everything when it differs from the applied one (a changed generation first resets the
>    process locally with the shared seed, without re-publishing). It runs at startup (a joining process adopts the
>    existing records and never wipes them), on every pub/sub message, and at the start of the entry points that read
>    shared records (scenario register/list/detail, `/sim/state`, `/sim/*/config`, Telnyx dial,
>    `POST /sim/telnyx/inbound`), which gives read-your-writes across processes without waiting for pub/sub. A process's own write
>    that directly follows the version it had applied does not trigger a reload.
> 5. Utterance allocation is a Lua `INCR sim:state:utterance_next` + `HSET`; past 65 535 it fails with the
>    unshared error message (409 `utterance_limit` on `say`). Allocations do not bump the version (dozens per call
>    would force every process to reload): resolution is the local registry first, then, on a miss inside the STT
>    worker thread, an `HGET` run on the event loop through `asyncio.run_coroutine_threadsafe` bounded to 1 s
>    (never from the loop thread); batch STT reads the whole hash. A failed or missing lookup is the existing
>    "unintelligible" path.
> 6. Redis failure: writes answer 503 `{"error": {"code": "state_unavailable", ...}}` (a caller-script utterance
>    fails its clip); a failed `sync()` logs `sim.shared_state.sync_failed` and keeps the local copy; the subscriber
>    resubscribes after 1 s, 2 s, 4 s, ... (capped at 10 s) and does a full sync once subscribed. Readiness checks
>    the Redis (`redis`).
> 7. `SIM_WORKERS` > 1 (several processes behind one port set) is deferred to the load-test task (plan/phase-20
>    P20-T06), because per-process FakeTelnyx calls make round-robin REST unusable; until then the runner exits 2
>    with `SIM_WORKERS > 1 is not supported yet (specs/30 §3 Decision P03-T15c)` on stderr. Several sim processes on
>    their own ports sharing one Redis are supported.

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

> Decision (2026-10-29): synthetic caller audio (P03-T03a). (1) The simulator has its own G.711 μ-law codec
> (`va_sim.audio.codec`: `ULAW_TO_PCM16`, `PCM16_TO_ULAW`, `ulaw_encode`/`ulaw_decode`, `pcm_to_l16be`/`l16be_to_pcm`)
> built table-first as specs/07 describes (bias 0x84, inverted bits, clip at 32 635); the sim never imports
> `va_voice`, and `va_voice.codec` (P04-T01) must be bit-identical (P04 adds the cross-check). (2) Randomness comes
> only from raw `numpy.random.PCG64(seed)` draws (`(raw >> 11) · 2⁻⁵³`); the per-utterance seed is
> `SimState.utterance_seed(id) = seed · 65 536 + id`, and sim seeds are non-negative (`POST /sim/reset` rejects a
> negative `seed` with 422). (3) The marker carries 16 bits, so utterance ids are 0..65 535: synthesis rejects other ids
> and `SimState.register_utterance` raises once 65 535 ids (1..65 535) are allocated in one run. (4) Preamble: peak
> −12 dBFS (8 231), phase-continuous FSK across symbols. (5) Body: `len(text) · rate_ms_per_char` ms; 180 ms
> syllables (the last may be shorter); syllable k speaks characters `[⌊k·c⌋, max(⌊(k+1)·c⌋, ⌊k·c⌋+1))` with
> `c = 180 / rate_ms_per_char`, and its vowel is `"aeiou"[Σ ord mod 5]`. Source: Rosenberg pulses (open 40 % of the
> period, closing 16 %), periods `T0 · (1 ± 3 %)` from the RNG, then a first difference (lip radiation). Each
> syllable runs through three parallel two-pole resonators (`rⁿ·sin(ω(n+1))`, unit peak, truncated at `rⁿ < 10⁻⁴`;
> gains 1.0/0.5/0.25, bandwidths 80/120/160 Hz) and a raised-cosine envelope from a 0.25 (−12 dB) floor up to 1,
> so the body never falls silent between syllables (a full Hann leaves ~13 % of 32 ms windows below −40 dBFS, too
> little for VAD). The body peak is −6 dBFS (`round(32 767 · 10^(−6/20))` = 16 422). (6) `text` needs a
> non-whitespace character, `rate_ms_per_char` ≥ 1, `f0_hz` in 50..400, `sample_rate` 8 000 or 16 000. 16 kHz audio
> (`synthesize_pcm16(..., sample_rate=16000)`, `synthesize_wire(..., sample_rate=16000)` → L16 big-endian) is
> synthesized natively for hd_audio; 8 kHz wire audio is μ-law. (7) `decode_marker` works at any sample rate
> ≥ 8 kHz: it scans in 1 ms steps for the first 20 ms window where the 1 700 Hz Goertzel power is ≥ 50 % of the
> window energy (RMS ≥ −30 dBFS), moves to the sync power peak within the next 12 ms (a passing window can start up
> to 10 ms early), and reads 16 symbols from 8 ms windows (1 ms trimmed each side): bit = P(2 200) > P(1 200), with
> P(1 200) + P(2 200) ≥ 50 % of the energy and RMS ≥ −30 dBFS for every symbol. A candidate that fails is skipped
> and the scan continues; it returns the first valid id or None. (8) `synthesize_dtmf(digit, ms=120, *,
> sample_rate=8000)` returns wire bytes (μ-law or L16 BE), each component at −10 dBFS; digits `0-9*#A-D`. (9)
> Synthesis is CPU-bound: async callers (the P03-T03b media client) run it in `asyncio.to_thread`. The
> `test_synth_detected_by_silero` requirement is checked in P04-T03, when `SileroVad` exists; until then
> `packages/va_sim/tests/audio` checks that ≥ 90 % of 32 ms body windows have RMS above −40 dBFS.

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
| `DELETE /v2/recordings/{id}` | removes the stored WAV (any authenticated key; recordings are process-wide), then the WAV URL answers 404; unknown id → Telnyx 404 (specs/06 §11 Decision P05-T14) |
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

> Decision (2026-11-02, P05-T17a): the compose e2e stack runs the simulators on the `realtime` clock only
> (`docker-compose.e2e.yml`, specs/29 §2 Decision P05-T17a). The services verify Telnyx webhook and media WS signature
> timestamps against wall time and run Temporal timers and 20 ms media pacing on the wall clock, while the accelerated
> clock starts at a fixed virtual date and jumps whenever the sim idles; there is no `VA_CLOCK=fake-shared` mode.
> Long flows (`max_duration`, `batch_100_calls`) use short configured limits instead of accelerated time; the
> accelerated clock stays for the in-process simulator tests.
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

> Decision (2026-10-29): FakeTelnyx REST, auth, and webhooks (P03-T02a). (1) Error codes: `10009` "Authentication
> failed" (401), `10005` "Resource not found" (404), `10015` "Invalid value" (422: missing, invalid, or unknown body
> field — every body model forbids unknown fields so client typos fail contract tests; body fields are located with
> `source.pointer` JSON pointers such as `/phone_numbers/0/phone_number`, query parameters with
> `source.parameter` such as `filter[limit]`), `40305` "Invalid 'from' address" (422, `/from`, the sender is not a
> number of the account), `85001` "Number not available" (422) for ordering a number that is not in the inventory,
> already owned, or reserved by a pending order; `90018` belongs to the call endpoints. (2) Resources are per
> account: `sim-telnyx-key` is built in and `POST /sim/telnyx/accounts {"api_key"}` → 201 `{"api_key"}` (idempotent)
> registers more; another account's id is a 404; `/sim/reset` drops registered accounts and every resource. The
> Bearer scheme is case-insensitive. (3) Webhook targets: an outbound message's events go to the messaging profile
> `webhook_url` when `use_profile_webhooks` is true (default) and the profile has one, otherwise to the request
> `webhook_url`, otherwise to `SIM_MESSAGING_WEBHOOK_URL`; `number_order.complete` goes to the `webhook_event_url`
> of the order's `connection_id` application, else `SIM_EDGE_WEBHOOK_URL`; inbound `message.received` goes to the
> owning number's messaging profile `webhook_url`, else `SIM_MESSAGING_WEBHOOK_URL`. (4) Retries: the first attempt
> is sent inline (events of one resource stay ordered); after a non-2xx answer or a transport error the event is
> re-sent 1 s after attempt 1 and 3 s after attempt 2 on the sim clock (`meta.attempt` 2 and 3, same event `id`, the
> payload as it was when emitted, re-signed with a fresh timestamp), then dropped. `force_duplicate_webhooks` sends
> each successful first attempt a second time (same id, `meta.attempt` 1). Every attempt is listed by
> `GET /sim/telnyx/webhooks` → `{"deliveries": [{event_id, event_type, url, attempt, status_code, error,
> delivered_at}]}` (last 10 000). (5) Signing key: `SIM_ED25519_PRIVATE_KEY` is base64 of the 32-byte Ed25519
> seed (the 64-byte seed + public key form is accepted when the halves match; blank means unset); otherwise a key is
> generated when the state is created and kept across `/sim/reset`. `GET /sim/telnyx/public-key` →
> `{"public_key"}` (base64 of the raw 32 bytes); `apps/sim` writes it plus a newline to `.sim/telnyx_public_key`
> (relative to its working directory) before serving. The signature timestamp is the sim clock's unix time.
> (6) Number search: `filter[limit]` default 10, 1–50 (else `10015`); `filter[phone_number_type]` must be `local`,
> `toll_free`, `mobile`, or `national`; repeated `filter[features][]` values must all be present; locality and
> administrative area match case-insensitively; owned and reserved numbers are excluded; `meta` is
> `{"total_results", "best_effort_results": 0}`. The inventory is 50 numbers per block: `+14155552000`–`2049`
> (San Francisco, CA), `+12125552000`–`2049` (New York, NY), `+18885552000`–`2049` (toll-free, voice/sms), and
> `+442079460000`–`0049` (London, national, voice). `filter[name]` on messaging profiles is exact; the
> `[contains]` filters of applications and outbound voice profiles are case-insensitive substrings. Lists page with
> `page[number]` (default 1) and `page[size]` (default 20, at most 250), `meta {"page_number", "page_size",
> "total_pages", "total_results"}`. Creating an application or outbound voice profile answers 201, a messaging
> profile or number order 200. A PATCH merges the body into the stored fields (nested objects key-wise) and
> re-validates the result like a create; omitted application fields take the specs/06 §2.1 values
> (`webhook_api_version "2"`, `webhook_timeout_secs 25`, `first_command_timeout true`, `first_command_timeout_secs
> 30`, `anchorsite_override "Latency"`, `dtmf_type "RFC 2833"`). Application, outbound voice profile, and phone number ids
> are 19-digit numeric strings; message, messaging profile, number order, order-entry, and webhook event ids are
> UUIDs; all are drawn from the seeded state RNG. (7) Message `parts`: GSM-7 (basic
> plus extension table, extension characters count 2) 160 septets single / 153 per part, otherwise UCS-2 70 / 67
> UTF-16 code units; `encoding` is `GSM-7` or `UCS-2`; `type` is `MMS` when `media_urls` is non-empty. Messages
> need `text` or `media_urls` (`10015` `/text`) and a messaging profile from the request or the sending number
> (`10015` `/messaging_profile_id`). `message.sent` is emitted 200 ms after the request and `message.finalized`
> 600 ms after it (`delivered`, or `delivery_failed` with error `40008` "Undeliverable" for destinations in the
> telnyx config `failing_message_destinations`). `POST /sim/telnyx/messages/inbound {from, to, text?, media_urls?}`
> emits `message.received` (`to[0].status: "webhook_delivered"`) and answers 202 `{"id"}`, or 422 when `to` is
> not owned; `GET /sim/telnyx/messages` lists every account's messages. (8) Number orders finish 1 s (sim clock)
> after creation: `success` (the numbers become `phone_number` records with the order's `connection_id`,
> `messaging_profile_id`, and `customer_reference`) or `failure` (numbers return to the inventory) when the telnyx
> config `fail_number_orders` is true. `GET`/`PATCH /sim/telnyx/config` carries `force_duplicate_webhooks`,
> `fail_number_orders`, and `failing_message_destinations` (partial PATCH, reset by `/sim/reset`). The
> `SimState.force_duplicate_webhooks` named above is this config field. `DELETE /v2/phone_numbers/{id}` answers the
> record with `status: "deleted"` and releases the number; `PATCH /v2/phone_numbers/{id}/messaging` answers
> `{record_type: "messaging_phone_number", id, phone_number, messaging_profile_id, updated_at}`.

> Decision (2026-10-29): FakeTelnyx call control (P03-T02b). (1) Ids: `call_control_id` is `"v3:"` + 24 seeded
> random bytes as url-safe base64; `call_leg_id`, `call_session_id`, conference, recording, and stream ids are seeded
> UUIDs. A transfer leg and a dial with `link_to` share the linked call's `call_session_id`. (2) Every call action
> answers 200 `{"data": {"result": "ok"}}`, conference create 200 `{"data": <conference>}`. State changes happen in
> the handler; their webhooks follow after the response, ordered per call session and per conference (a delivery
> waits for the previous delivery of every session or conference it touches, so `call.bridged` arrives on A then
> B and conference events in the order they happened). A command's `client_state` replaces the leg's state for
> its own and later events. (3) `command_id` dedup is per call (per account for dial); a repeat answers the same
> success without running again and is logged with `duplicate: true`; dedup is checked before the ended-call check
> so a retried `hangup` stays a success. (4) Errors: unknown call, another account's call, or unknown action → 404
> `10005`; a command for a call that has hung up → 422 `90018` "Call has already ended"; a command that does not
> fit the leg (answer/reject on an outgoing or already answered leg; transfer, bridge, record, streaming, playback,
> DTMF before answer; a second `record_start` or `streaming_start`; `record_stop`/`streaming_stop` with nothing
> running; bridging with itself, an unanswered leg, or an already bridged leg) → 422 `10015` without a `source`;
> unknown `connection_id`, `link_to`, or bridge/conference `call_control_id` → 422 `10015` with that pointer
> (an ended bridge peer or conference participant → `90018`). `streaming_start` needs `stream_url`; a
> `streaming_stop` `stream_id` must match the active stream (`/stream_id`). `from` is not checked against owned
> numbers; `to` accepts E.164 or `sip:` URIs. `playback_stop` with nothing playing is a no-op success.
> (5) Webhook target: the leg's `webhook_url` (dial, answer, or transfer body; a transfer leg inherits the A leg's),
> else the application's `webhook_event_url`, else `SIM_EDGE_WEBHOOK_URL`. (6) Outcomes: the scenario bound to the
> dialled `to` (§3.5 `answer`); a dial with `link_to` and no scenario of its own, and every transfer leg, use the
> linked/A leg's scenario `transfer_target`; without a scenario a leg is answered after 1 000 ms by
> `human_residence`. On the sim clock from the dial: `call.initiated` (state `bridging`) at once; answered →
> `call.answered` at `after_ms` (a ring longer than `timeout_secs` is a no-answer); busy → `call.hangup`
> `user_busy`/source `callee` at 500 ms; rejected → `call_rejected`/`callee` at 500 ms; failed →
> `unspecified`/`unknown` at 500 ms; no_answer → `timeout`/`unknown` at `timeout_secs` (default 30). An answered
> leg is hung up `time_limit`/`unknown` after `time_limit_secs` (default 14 400). `sip_hangup_cause` per cause:
> normal_clearing 200, originator_cancel 487, timeout 487, time_limit 200, user_busy 486, not_found 404,
> no_answer 480, call_rejected 603, unspecified 503 (the sim's table; production values are checked by the live
> smoke test of specs/06 §5.1). `call.hangup` also carries `end_time`. (7) Hang-up sides: a platform `hangup` has
> `hangup_source: caller` on legs the platform dialled and `callee` on inbound legs, cause `normal_clearing` once
> answered, else `originator_cancel`; `reject` is `call_rejected` (`CALL_REJECTED`) or `user_busy` (`USER_BUSY`),
> source `callee`. `POST /sim/telnyx/calls/{ccid}/hangup {"cause"?}` (default `normal_clearing`) is the far end
> (the opposite source) and answers 202 with the call summary, 404 `not_found`, or 409 `call_ended`. Hang-up order:
> `streaming.stopped`, `call.playback.ended` (`status: "call_hangup"`), `conference.participant.left` (and
> `conference.ended`), `call.hangup`, `call.recording.saved`. When a bridged leg hangs up its peer is hung up too
> (`normal_clearing`, the peer's far-end source) unless the peer issued the bridge with
> `park_after_unbridge: "self"`; then the peer goes back to `parked` without an event. (8) AMD from answer:
> `premium` → `call.machine.premium.detection.ended` `{result: <answered_by>}` at +1 800 ms and, for `machine` with
> `beep_at_ms`, `call.machine.premium.greeting.ended` `{result: "beep_detected"}` at +`beep_at_ms`; `detect`,
> `detect_words` → `call.machine.detection.ended` `{result}` (`human_*` → `human`, `machine` → `machine`,
> `silence`/`fax_detected` → `not_sure`), and `detect_beep`/`greeting_end` add `call.machine.greeting.ended`
> `{result: "beep_detected"}` for a beep; events are emitted in time order; `disabled` sends none; a hang-up
> cancels pending detection events. (9) Transfer (answered A leg only) creates leg B (`outgoing`, `from` defaults to
> A's `from`, `client_state` = `target_leg_client_state`) → B `call.initiated`; answered → B `call.answered`, then
> `call.bridged` on A then B; not answered → B `call.hangup` per (6) and A stays up. The summary of A lists the
> transfer. `bridge` sends `call.bridged` on the commanding leg then the other; `link_to` + `bridge_on_answer: true`
> bridges on answer. A leg that would bridge on answer (transfer leg B, or a `link_to` + `bridge_on_answer`
> dial) and is still ringing when its peer hangs up is cancelled right after the peer's events: `call.hangup`
> `originator_cancel`, source `caller`, SIP 487. A leg that answers when its peer has already ended (a dial
> `link_to` an ended call) sends `call.answered` then `call.hangup` `normal_clearing`, source `caller`, with no
> media started. (10) Recordings (`record_start`, or `record: "record-from-answer"` on dial/answer, or
> `record` on transfer for leg B) end on `record_stop` or hang-up with `call.recording.saved` `{recording_id,
> channels, format, recording_started_at, recording_ended_at, recording_urls: {"wav":
> "<base>/sim/recordings/<id>.wav", "mp3": null}, public_recording_urls}` — always WAV (specs/06 §10.2: no MP3
> encoder), so `mp3` is null even for `format: "mp3"`; `<base>` is the base URL of the request that created the
> call. The WAV is 8 kHz PCM16, channel 0 the caller track and channel 1 the agent track (`single`: their clipped
> sum), padded with silence to the recording's sim-clock duration, and is stored before the event is sent.
> (11) `streaming_start` (or dial/answer `stream_url`, started on answer) → `streaming.started` `{stream_url}`;
> `streaming_stop` → `streaming.stopped`; the WS connection arrives with the media client (P03-T03b).
> `playback_start` → `call.playback.started` `{media_url, overlay}`; `playback_stop` (`stop: "all"` or the latest
> with `"current"`, filtered by `overlay`) → `call.playback.ended` `{status: "cancelled"}`. `send_dtmf` is logged
> `{digits, duration_millis, at}`. (12) Conferences: `POST /v2/conferences` (answered leg not in a conference) →
> `{id, name, record_type: "conference", status: "in_progress", connection_id, created_at, expires_at (+24 h),
> region: "sim"}`, `conference.created` then `conference.participant.joined`; `join` (answered leg not in a
> conference) → `conference.participant.joined`; `leave` or a participant hang-up → `conference.participant.left`;
> when the last participant leaves, or one that joined with `end_conference_on_exit: true`, the conference becomes
> `completed`, `conference.ended` (`reason: "all_left"` | `"host_left"`) is sent, and the remaining participants
> get `conference.participant.left` (their calls stay up). Conference events carry `{conference_id,
> call_control_id, call_leg_id, call_session_id, client_state, connection_id}` and go to the participant leg's
> webhook URL. Unknown conference → 404 `10005`; joining a completed one → 422 `10015`. (13) `POST
> /sim/telnyx/inbound {to, from, scenario?}`: `to` must be owned by an account and assigned to a connection (else
> 422 `invalid_request`); `scenario` must be registered (else 404 `not_found`), otherwise the scenario bound by
> `inbound_from` applies. The incoming leg (state `parked`) emits `call.initiated` to the application's
> `webhook_event_url` and the route answers 202 `{call_control_id, call_leg_id, call_session_id}`. With the
> application's `first_command_timeout`, a leg that receives no command within `first_command_timeout_secs` is hung
> up `timeout`/`unknown`. (14) Control API: `GET /sim/telnyx/calls` → `{"calls": [summary]}` (creation order) and
> `GET /sim/telnyx/calls/{ccid}` → `{call_control_id, call_leg_id, call_session_id, connection_id, direction, from,
> to, state, is_alive, scenario_id, client_state, created_at, answered_at, ended_at, hangup_cause, hangup_source,
> commands: [{name, command_id, at, duplicate}], events: [{event_type, event_id, at}], dtmf_sent, transfers:
> [{call_control_id, to, from, at}], recordings, stream, conference_id}`; `GET .../timeline` → `{call_control_id,
> initiated_ms, answered_ms, ended_ms, first_agent_speech_ms, clears, marks, utterances}` (sim `virtual_ms`; utterances
> come with the media client; `first_agent_speech_ms` since P05-T17c, `clears` = the `virtual_ms` of every agent `clear`
> frame received, since P05-T17d; see the §5 Decisions; `marks` = `[{name, track_ms}]` for every agent `mark` echoed
> back, `track_ms` being the aligned agent-track offset at the echo, i.e. where that audio ended in the recording,
> since 2026-11-03, `Call.mark_echoes` in process).
> Payload `state`: `bridging` (outgoing initiated), `parked` (incoming initiated, or after unbridge), `answered`,
> `bridged`, `hangup`; `is_alive` is true only between answer and hang-up. `GET /sim/state` `telnyx` adds `calls`
> and `conferences` counts; `/sim/reset` drops calls and conferences and cancels pending progress. (15) Accelerated
> clock: virtual time moves only when the event loop is idle (no runnable callback), so a command sent through an
> in-process client mid-call is handled at the virtual time it was sent and in-process webhook deliveries finish
> before time moves; work blocked on real I/O (sockets, worker threads) leaves the loop idle and does not hold time
> back.

> Decision (2026-11-02): FakeTelnyx inbound transfer (P05-T12b). `transfer` is accepted on an answered leg and on
> a still-ringing incoming leg (otherwise `10015`). The target leg rings as for any transfer; when it answers, the
> ringing incoming leg is answered first (`call.answered` on it, its time limit starts) and the two legs bridge
> (`call.bridged` on each). A busy or unanswered target leaves the incoming leg ringing; the caller hanging up
> while the target rings cancels the target leg (`originator_cancel`).

> Decision (2026-10-29): FakeTelnyx media WebSocket client (P03-T03b). (1) Library `websockets` (asyncio client,
> no compression, no pings, 5 s open timeout, 4 MiB max message); one `MediaSession` per stream
> (`va_sim.telnyx.media_session`), started by `CallMedia.start_stream` on the supervisor right after
> `streaming.started`. (2) Handshake headers: `telnyx-signature-ed25519` = base64 Ed25519 signature of
> `"{timestamp}|{stream_url}"` (the URL exactly as given, query included), `telnyx-timestamp` = sim clock unix
> seconds, and `Authorization: Bearer <stream_auth_token>` when the stream has one. On the accelerated clock virtual
> time does not move while the session connects. (3) Codecs: caller audio is PCMU 8 kHz (`stream_codec` `PCMU` or
> `default`) or L16 big-endian (`L16`) at `stream_bidirectional_sampling_rate` when that is 8 000 or 16 000, else
> 16 000; agent audio is parsed in `stream_bidirectional_codec` (default: the stream encoding) at the bidirectional
> rate, else the stream rate (PCMU is always 8 kHz). Frames are 20 ms: 160 B PCMU, 320 B L16 8 kHz, 640 B L16 16 kHz.
> When a stream is requested (`stream_url` on dial/answer, or `streaming_start`), `stream_codec` or
> `stream_bidirectional_codec` PCMA/G722/OPUS/AMR-WB, a bidirectional rate other than 8 000/16 000, or
> `stream_bidirectional_mode: "mp3"` answer 422 `10015` with pointer `/stream_codec`,
> `/stream_bidirectional_codec`, `/stream_bidirectional_sampling_rate`, or `/stream_bidirectional_mode` before any
> state change. (4) Frames sent (JSON text): `{"event":"connected","version":"1.0.0"}`; `start` `{sequence_number:
> "1", start: {user_id (seeded uuid per stream), call_control_id, call_session_id, from, to, tags: [], client_state,
> media_format: {encoding, sample_rate, channels: 1}}, stream_id}`; `media` `{sequence_number, media: {track:
> "inbound", chunk: "<n from 1>", timestamp: "<20·(n−1)>", payload: <base64>}, stream_id}`; `dtmf` `{stream_id,
> occurred_at, sequence_number, dtmf: {digit}}`; `mark` `{stream_id, sequence_number, mark: {name}}`; `error`
> `{payload: {code, title, detail}, stream_id}` (no sequence number); `stop` `{sequence_number, stop: {user_id,
> call_control_id}, stream_id}` then close 1000. `sequence_number` is a per-stream decimal string counter (`start` =
> "1"). (5) A tick loop on the sim clock (absolute schedule, one tick per 20 ms) is the only writer: each tick plays
> one frame of queued agent audio (padded with silence; silence when idle) into the agent track, sends the marks now
> due, then pending `error` and injected frames, then at most one DTMF digit, then one caller `media` frame (queued
> caller audio, else silence 0xFF / zeros) whose 8 kHz decode goes to the caller track (L16 16 kHz is decimated to
> 8 kHz by averaging sample pairs). At connect both tracks are padded with silence to the time since answer, so
> recordings stay aligned with the sim clock. (6) Agent frames: text only; binary, invalid JSON, a non-object, an
> unknown or missing `event`, or `mark` without a non-empty string `mark.name` → `error` `100003`
> `malformed_frame`; `media` without a string `media.payload`, invalid base64, empty, an odd length for L16, or more
> than 30 s of audio → `100004` `invalid_media`; the connection stays open. `media` is queued for playout; `mark` is
> due once all audio queued before it has played (with nothing queued: at the next tick, before its media frame);
> `clear` drops the queued audio and makes every pending mark due, in order. Errors and immediate marks therefore go
> out at the next tick (≤ 20 ms). (7) `streaming_stop` and hang-up stop the session: it sends `stop` and closes with
> 1000, and `streaming.stopped` is delivered once the socket is closed (so it still precedes `call.hangup`); a
> stop while connecting cancels the attempt silently. A failed connect (refused, timeout, rejected handshake) →
> `streaming.failed` `{stream_id, stream_url, failure_reason: "connection_failed"}` and the agent closing first →
> `failure_reason: "connection_closed"`; both leave the call without a stream (a later `streaming_stop` is 422).
> `/sim/reset` cancels sessions and closes their sockets. (8) `GET /sim/telnyx/calls/{ccid}/agent_audio.wav` → the
> agent track as mono 8 kHz PCM16 WAV (`audio/wav`; 404 `not_found` for an unknown call). (9) Control API caller
> audio (409 `call_ended` for an ended call, 409 `not_streaming` without a connected session, 404 unknown call, 422
> body errors): `POST .../say {text, language="en"}` registers the utterance, synthesizes it in the stream format
> (`synthesize_wire`, seed `utterance_seed(id)`, in a worker thread), queues it, and answers 202 `{utterance_id,
> duration_ms}`; `POST .../dtmf {digits}` (`^[0-9A-D*#]+$`) → one `dtmf` event per digit at successive ticks, logged
> in the summary `caller_dtmf: [{digit, at}]`, 202 `{digits}`; `POST .../media/inject {frame: {...}}` → the frame
> verbatim before the next media frame, 202 `{queued: true}`. Telnyx config `media_jitter` (default false) sends media
> frames in swapped adjacent pairs (2, 1, 4, 3, …) with their original `chunk`, `timestamp`, and `sequence_number`.
> (10) Timeline utterances `{utterance_id, text, start_ms, end_ms, first_agent_audio_ms, latency_ms}`: `start_ms`
> when the frame with the utterance's first byte is sent, `end_ms` with its last byte, `first_agent_audio_ms` the
> arrival of the first agent `media` message after `end_ms` that is speech (frame RMS above 1024, Decision P05-T15
> under §3.5; was a sample louder than |256|), `latency_ms` their
> difference (null until then). (11) The summary `stream` adds `{connected, frames_sent, received: {media | mark |
> clear | invalid: count}, marks_echoed}`; the summary adds `caller_dtmf`. The scenario step runner (greeting,
> steps, barge-in) is P03-T03c.

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

> Decision (2026-10-29): FakeElevenLabs TTS (P03-T04). Package `va_sim.elevenlabs` (`routes.elevenlabs_routes`,
> added by `simulator_routes("elevenlabs", ...)`).
> 1. Audio: 60 ms per character; whitespace is silence, any other character a sine of `300 + (ord(c) % 12)·25` Hz,
>    −10 dBFS peak, phase 0 at each character start. `ulaw_8000` (G.711 μ-law), `pcm_16000|22050|24000|44100`
>    (signed 16-bit little-endian mono), `mp3_44100_128`: valid MPEG-1 Layer III mono 128 kbps 44.1 kHz frames with
>    all-zero side info and main data (they decode to silence; no encoder dependency), frame sizes 417/418 bytes
>    (padding bit on the 418-byte frames), frame count `ceil(duration·44.1/1152)` raised when needed so the last
>    200 ms piece holds at least one frame (at most one extra silent frame). Other formats are rejected.
> 2. Chunking: 200 ms pieces (last one shorter; mp3 pieces are the frames starting in the window); a piece's
>    `alignment` lists the characters starting inside it with start times relative to the piece, `null` when none
>    does; `normalizedAlignment` equals `alignment`.
> 3. WebSocket: always accepted, failures close it. Auth `xi-api-key` header, else the first message's
>    `xi_api_key` or `authorization` (`Bearer <key>` or raw); missing or not accepted → close 1008
>    `"Invalid API key"`. Accepted keys: any non-empty key unless `PUT /sim/elevenlabs/api-keys {"keys": [...]}`
>    set an allow list (`GET` reads it; reset clears). Query `model_id` (default `eleven_flash_v2_5`),
>    `output_format` (default `mp3_44100_128`), `inactivity_timeout` 1..180 (default 20), `sync_alignment`,
>    `auto_mode`, `enable_ssml_parsing`, `enable_logging` (`true|false`), `apply_text_normalization`
>    (`auto|on|off`), `language_code`; invalid → close 1008 `"invalid <param>: <why>"`. Any voice id is accepted.
>    Invalid JSON or a non-object message → close 1008 `"invalid message"`; unknown fields are ignored.
> 4. Contexts: `context_id` defaults to `"default"`; at most 5 open per socket (a 6th → error frame
>    `{"message": "Maximum of 5 concurrent contexts per connection exceeded", "error": "max_active_conversations",
>    "code": 1008}`, message ignored, socket stays open); a closed id → error frame `context_closed`; a bad
>    `generation_config.chunk_length_schedule` (1-4 integers, each 50..500), non-string `text`, or empty/non-string
>    `context_id` → error frame `invalid_message`. `voice_settings` and the schedule come from a context's first
>    message. Default schedule `[120, 160, 250, 290]` (ElevenLabs' default; "[0] (default 120)" above is its first
>    element).
> 5. Generation: text appends to the context buffer; a generation takes the whole buffer verbatim on `flush: true`
>    or when the stripped buffer reaches `schedule[min(k, len-1)]` (k = generations so far). A whitespace-only
>    buffer never generates (a flush discards it), so the keepalive `{"text": "", "context_id": "ka"}` only
>    refreshes the inactivity timer. Generations of a context run in order on a per-context worker; contexts
>    interleave; the first chunk of a context's first generation waits `tts_ttfa_ms` (sim clock) after its trigger,
>    later chunks go back to back.
> 6. `close_context: true` supersedes "generates audio on `close_context`" above: it discards the unflushed buffer,
>    stops the in-flight generation before its next chunk, drops queued generations, then sends
>    `{"isFinal": true, "contextId": id}` (specs/09 §8). `close_socket: true` discards unflushed buffers, lets
>    triggered generations finish, sends `isFinal` per open context, then closes 1000.
> 7. Inactivity: sim-clock timer from the last message carrying a `text` field; expiry closes 1008 with
>    `"Have not received a new text input within the timeout of {N} seconds."`. On an accelerated clock it fires as
>    soon as the loop idles past N virtual seconds (idle real-socket waits do not hold virtual time).
> 8. Records: `SimState.elevenlabs.tts_requests`, served at `GET /sim/elevenlabs/tts/requests` as
>    `{"requests": [{id ("ttsreq_<n>"), transport ("ws"|"rest"), voice_id, model_id, output_format, context_id (null
>    for REST), started_ms, text (all text received), generated_text, characters, ended_ms, interrupted,
>    end_reason (null|close_context|close_socket|inactivity|disconnect|fault|reset|completed)}]}`; `interrupted`
>    means non-whitespace text was discarded or audio dropped.
> 9. Faults: `PUT /sim/elevenlabs/tts/fault {"kind": "disconnect"|"error"|"delay", "after_chunks" = 0,
>    "delay_ms" = 0, "times" = 1}` (204), `GET` → `{"fault": null|{kind, after_chunks, delay_ms, remaining}}`,
>    `DELETE` (204); reset clears. WebSocket: checked before a context's chunk number `after_chunks` (0-based);
>    `disconnect` → close 1011 `"simulated disconnect"`; `error` → frame `{"message": "Simulated TTS failure",
>    "error": "internal_error", "code": 1011}` then close 1011; `delay` → wait `delay_ms` then send. REST: `error`
>    → 500 `{"detail": {"status": "internal_error", "message": "Simulated TTS failure"}}`, `delay` adds latency;
>    `disconnect` does not apply. Each firing consumes one of `times`.
> 10. REST: `POST /v1/text-to-speech/{voice_id}` (whole audio), `.../stream` (the 200 ms pieces, first after
>    `tts_ttfa_ms`), `.../with-timestamps` (`{"audio_base64", "alignment": {"characters",
>    "character_start_times_seconds", "character_end_times_seconds"}, "normalized_alignment"}`, absolute seconds).
>    Body `{text (1..40 000), model_id (default eleven_multilingual_v2), voice_settings?, language_code?,
>    apply_text_normalization?}`, query `output_format`, header `xi-api-key`; content types `audio/mpeg`,
>    `audio/pcm`, `audio/basic`; headers `x-character-count`, `request-id`. Missing key → 401 `needs_authorization`,
>    rejected key → 401 `invalid_api_key`, invalid body/query/JSON → 422 `{"detail": [{"loc", "msg", "type"}]}`.
> 11. Synthesis runs in a worker thread inside `va_sim.tasks.hold_clock` (no virtual time lost). `/sim/reset`
>    closes live sockets with 1012 `"simulator reset"` and clears records, keys, and the fault; `GET /sim/state`
>    adds `"elevenlabs": {"tts_requests", "tts_sockets"}`. The voice registry (`voice_not_found`) arrives with the
>    voices task.

> Decision (2026-10-29): FakeElevenLabs realtime STT (P03-T05a). `va_sim.elevenlabs` modules `stt_params`,
> `stt_stream` (pure analysis), `stt_text` (word timings, entities), `stt_store` (`ElevenLabsStore.stt`), `stt_ws`,
> `stt_control`; `ws_io` holds the JSON receive/close helpers shared with the TTS socket.
> 1. `WS /v1/speech-to-text/realtime`, always accepted. Auth: `xi-api-key` header only. Missing → frame
>    `{"message_type": "auth_error", "error": "Neither authorization header nor xi-api-key received, please provide
>    one."}` + close 1008 `"Missing API key"`; not accepted → frame `auth_error` `"Invalid API key"` + close 1008
>    `"Invalid API key"`. The query is validated before auth.
> 2. Query: `model_id` required; `audio_format` `ulaw_8000|pcm_8000|pcm_16000|pcm_22050|pcm_24000|pcm_44100|
>    pcm_48000` (default `pcm_16000`; pcm = signed 16-bit little-endian mono); `commit_strategy` `manual` (default)
>    | `vad`; `include_timestamps`, `include_language_detection`, `filter_background_audio`, `no_verbatim` (false),
>    `enable_logging` (true) as `true|false`; `language_code` `^[a-z]{2,3}$`; `keyterms` repeated (≤ 100, each
>    1..50 chars); `entity_detection` repeated (`pii|pci|phi|all`); `keepalive_interval_ms` 1000..60000 (recorded
>    only); `vad_silence_threshold_secs` 0.3..3.0 (default 1.5). Unknown parameters are ignored. Invalid → frame
>    `{"message_type": "invalid_request", "error": "invalid <param>: <why>"}` + close 1008 with the same text
>    (close reasons are cut to 123 UTF-8 bytes).
> 3. First frame `{"message_type": "session_started", "session_id": <32 hex>, "config": {model_id, audio_format,
>    sample_rate, commit_strategy, include_timestamps, include_language_detection, language_code, keyterms,
>    entity_detection, filter_background_audio, keepalive_interval_ms, enable_logging, no_verbatim,
>    vad_silence_threshold_secs}}`.
> 4. Client messages `{"message_type": "input_audio_chunk", "audio_base_64", "commit"?, "sample_rate"?,
>    "previous_text"?}`; unknown fields ignored. Invalid JSON/non-object, wrong `message_type`, missing or invalid
>    base64, non-boolean `commit`, `sample_rate` ≠ the format's rate, odd PCM byte count, non-string
>    `previous_text`, or `previous_text` after the first valid chunk → frame `{"message_type": "input_error",
>    "error"}`, message dropped, socket open. More than 1 000 ms of audio in one message → frame
>    `chunk_size_exceeded` `"Audio chunk exceeds 1000 ms"`, message (and its commit) dropped.
> 5. Analysis: 20 ms frames (a partial frame waits for the next message), voiced when RMS ≥ 328 (−40 dBFS). A
>    voiced frame with no open segment starts one; 300 ms of unvoiced audio ends it; a segment with < 100 ms voiced
>    audio is discarded and never resolved. Once a segment has ≥ 100 ms voiced audio and 260 ms of samples after
>    its first voiced frame (plus a 40 ms pre-roll), `decode_marker` runs once (zero-padded when the segment ends
>    earlier): registered id → that utterance's text and language; unregistered id → `"[unintelligible]"`; no
>    marker → the next text of the transcript script, else `"[unintelligible]"`. A commit does not force an
>    unresolved open segment (a partial preamble would misdecode); its words join a later commit. Expected duration:
>    marker segments `utterance_ms(text, 70)` (= 180 ms preamble + 70 ms/char, the caller's default rate; the body
>    starts 180 ms after the segment start), markerless `len(text)·70` (body = segment start). Revealed words
>    `ceil(n · voiced_ms / expected_ms)` (capped), all once the segment ended. The commit window is every
>    revealed, uncommitted word; a `partial_transcript` `{"message_type", "text": <window>}` goes out each time
>    the window's voiced audio reaches a multiple of 300 ms (reset by commits) and the text is non-empty — this
>    replaces specs/08 §10's 200 ms. `vad` commits by itself when unvoiced audio first reaches
>    `vad_silence_threshold_secs` after a segment ended and the window is non-empty.
> 6. Commits are sent `stt_final_ms` (sim clock) after the commit was read, in order, by a per-socket committer
>    task: `committed_transcript {"text"}`; with `include_timestamps` also `committed_transcript_with_timestamps
>    {"text", "language_code", "words"}` (words `{"text", "start", "end", "type": "word", "logprob": 0.0}` at
>    70 ms/char from the body start, session-relative seconds rounded to 3 decimals, alternating with `spacing`
>    entries); with `entity_detection` and non-empty text also `committed_transcript_entities {"text", "entities":
>    [{"text", "entity_type", "start_char", "end_char"}]}` (emails → `email_address` for pii/all; ≥ 13 digits with
>    optional single space/hyphen separators → `credit_card_number` for pci/all; the list may be empty). Utterance
>    entity names are not modelled (the registry has no entities). No voiced audio → empty text (`words: []`).
> 7. Faults: `PUT /sim/elevenlabs/stt/fault {"kind", "after_ms" = 0, "times" = 1}` (204), `GET` → `{"fault":
>    null|{kind, after_ms, remaining}}`, `DELETE` (204), separate from the TTS fault. Kinds: every specs/08 §4
>    error `message_type` plus `disconnect`. Checked after each chunk's audio is fed once the session's analysed
>    audio reaches `after_ms`; `commit_throttled` instead fires on the first client commit at/after `after_ms` and
>    swallows it (the window waits for the next commit). Frame `{"message_type": kind, "error": "Simulated
>    <kind>"}`, then close 1008 (`auth_error`, `quota_exceeded`, `unaccepted_terms`, `invalid_request`,
>    `rate_limited`, `session_time_limit_exceeded`) or 1011 (`error`, `transcriber_error`, `resource_exhausted`)
>    with the error as reason; the other kinds leave the socket open; `disconnect` closes 1011 `"simulated
>    disconnect"` without a frame. Each firing consumes one of `times`, across sessions. `SimState.stt_fail_after_s`
>    above is this route with kind `transcriber_error`.
> 8. Records at `GET /sim/elevenlabs/stt/sessions` → `{"sessions": [{id, model_id, audio_format, commit_strategy,
>    language_code, keyterms, entity_detection, include_timestamps, filter_background_audio (P05-T15), started_ms,
>    previous_text, ended_ms,
>    end_reason (null|disconnect|fault|reset), audio_ms (whole frames analysed), voiced_ms, partials, commits,
>    transcripts: [{text, utterance_ids, language_code, committed_ms}]}]}`, recorded once authenticated; commits not
>    yet sent when the socket closes are dropped.
> 9. Transcript script: `POST /sim/elevenlabs/stt/transcripts {"texts": [non-empty, ...]}` appends (204), `GET` →
>    `{"texts"}`, `DELETE` clears (204); it replaces specs/08 §10's Redis key `sim:stt:{session}`.
> 10. `/sim/reset` closes STT sockets 1012 `"simulator reset"` (records end `reset`) and clears sessions, script,
>    and fault; `GET /sim/state` `elevenlabs` adds `stt_sessions` and `stt_sockets`.
> 11. No server inactivity timeout and no spontaneous limit errors; errors come only from faults. Analysis runs in
>    a worker thread inside `va_sim.tasks.hold_clock`.

> Decision (2026-10-29): FakeElevenLabs batch STT (P03-T05b). Contract from the ElevenLabs speech-to-text/convert
> docs.
> 1. `POST /v1/speech-to-text`, multipart/form-data, header `xi-api-key` (401 `needs_authorization` /
>    `invalid_api_key`). Fields: `model_id` (required, non-empty); exactly one of `file` (upload),
>    `cloud_storage_url`, `source_url` (http/https URLs); `language_code` `^[a-z]{2,3}$`; `timestamps_granularity`
>    `none|word|character` (default `word`); `diarize`, `use_multi_channel`, `tag_audio_events` (`true|false` in any
>    case; defaults false, false, true); `num_speakers` 1..32; `multichannel_output_style` `separate|combined`
>    (default `separate`); `keyterms` repeated (≤ 1 000, each 1..49 chars, ≤ 5 words, none of `<>{}[]\`);
>    `entity_detection` repeated (`all|pii|phi|pci|other|offensive_language`); `file_format` `pcm_s16le_16|other`
>    (default `other`). Query `enable_logging` `true|false` (default true). Unknown fields are ignored. Invalid
>    input → 422 `{"detail": [{"loc": ["body", <field>, ...] | ["query", "enable_logging"] | ["body"], "msg",
>    "type"}]}`; `["body"]` covers the source count, `combined` with `timestamps_granularity=none` or with
>    `entity_detection`, a non-multipart body, and a multipart parse failure (more than one file included).
> 2. Audio: `pcm_s16le_16` is raw signed 16-bit LE mono 16 kHz; otherwise a 16-bit PCM WAV at ≥ 8 kHz, else 400
>    `{"detail": {"status": "invalid_audio", "message": "Only 16-bit PCM WAV or pcm_s16le_16 audio is supported by
>    the simulator"}}`. More than 5 channels with `use_multi_channel` → 400 `too_many_channels`; under 100 ms → 400
>    `audio_too_short`; over 100 MB (upload or fetched) → 400 `file_too_large`. Without `use_multi_channel` the
>    channels are down-mixed (clipped sum, as FakeTelnyx `single`), so overlapping speakers do not reconstruct.
> 3. URL sources: a URL whose path is `/sim/recordings/<id>.wav` with `<id>` stored is read in-process whatever the
>    host; any other URL is fetched (10 s timeout, no redirects). Non-200, transport error, or timeout → 400
>    `invalid_source_url` with message `Could not fetch audio from <url> (<HTTP nnn | timed out | error class>)`.
> 4. Each channel is cut with the realtime constants (`stt_stream`): 20 ms frames voiced at RMS ≥ -40 dBFS,
>    segments split by ≥ 300 ms unvoiced, dropped under 100 ms voiced, spanning first to last voiced frame.
> 5. Agent speech first: from the onset (first sample with |x| ≥ 64, minus one) 60 ms slots are read; a quiet slot
>    is a space and the onset is searched again after it; a voiced slot is the tone class with the largest Goertzel
>    power and counts only at ≥ 60 % of the slot energy, else the segment is not agent speech. The class string is
>    looked up in every TTS record's `generated_text` (mapped the same way, whitespace runs → one space; record
>    order, earliest offset); its original characters give the words and the slots the times. When the whole
>    string matches nowhere, the longest word-aligned prefix that matches is taken and the rest matched again
>    (back-to-back generations in one segment); unmatched runs become `[unintelligible]` spanning their slots, and
>    a segment matching nothing is one `[unintelligible]` spanning the segment. Short strings can match the wrong
>    record (12 classes); the first match wins, deterministically.
> 6. Otherwise caller speech: the marker is decoded from 40 ms pre-roll plus 260 ms from the segment start
>    (zero-padded); a registered utterance gives its words timed at 70 ms/char from segment start + 180 ms, in its
>    language; an unknown id, no marker, DTMF, or other audio is one `[unintelligible]` spanning the segment.
>    Batch transcription never consumes the realtime transcript script.
> 7. Words `{"text", "start", "end", "type": "word", "speaker_id", "logprob": 0.0}` in file-relative seconds
>    (3 decimals), with a `{"text": " ", "type": "spacing", "start": previous end, "end": next start, "speaker_id":
>    previous speaker, "logprob": 0.0}` entry between consecutive words of one channel. `character` granularity adds
>    `characters: [{"text", "start", "end"}]` to each word (agent: per slot; caller: 70 ms per char;
>    `[unintelligible]`: one entry spanning the word); `none` returns `words: []` with `text` filled. `speaker_id`:
>    multichannel `speaker_<channel>`; mono with `diarize` caller `speaker_0`, agent `speaker_1`; else `speaker_0`.
>    Words (and spacing) carry `channel_index` only with `use_multi_channel`. `text` joins the words with single
>    spaces; `language_code` is the first caller utterance language heard, else the request `language_code`, else
>    `en`; `language_probability` 1.0.
> 8. Shapes: without `use_multi_channel` `{language_code, language_probability, text, words, transcription_id,
>    audio_duration_secs, entities?}`; `separate` `{transcripts: [{language_code, language_probability, text, words,
>    channel_index, entities?}], transcription_id, audio_duration_secs}` (a mono file gives one transcript);
>    `combined` the single-channel shape with every channel's entries ordered by (start, channel_index, original
>    order) and `language_code` from the first channel that heard a caller utterance. `transcription_id` is 32 hex
>    characters; `audio_duration_secs` is rounded to 3 decimals. `entities` (per transcript or top level) only
>    when `entity_detection` was sent, from the realtime regex rules.
> 9. Records: `GET /sim/elevenlabs/stt/batch` → `{"requests": [{transcription_id, model_id, source (file|url),
>    source_url, file_format, channels, use_multi_channel, multichannel_output_style, diarize, num_speakers,
>    timestamps_granularity, language_code, keyterms, entity_detection, enable_logging, audio_duration_secs,
>    received_ms}]}`, written only for successful requests.
> 10. Fault: `PUT /sim/elevenlabs/stt/batch/fault {"kind": "error"|"rate_limited"|"delay", "delay_ms" = 0,
>    "times" = 1}` (204), `GET` → `{"fault": null|{kind, delay_ms, remaining}}`, `DELETE` (204). Checked after auth
>    and before validation: `error` → 500 `internal_error` "Simulated batch STT failure", `rate_limited` → 429
>    `too_many_concurrent_requests`, `delay` waits `delay_ms` on the sim clock and serves normally; each firing
>    consumes one of `times`. `/sim/reset` clears records and fault; `GET /sim/state` `elevenlabs` adds `stt_batch`.
> 11. Analysis (decoding, segmentation, tones, markers) runs in a worker thread inside `va_sim.tasks.hold_clock` on
>    snapshots of the utterance registry and the TTS generated texts.

> Decision (2026-10-31): FakeElevenLabs voices and subscription (P03-T05c). Contract from the ElevenLabs voices
> (v2 search, add IVC, edit, settings, delete) and user/subscription docs and specs/15 §2-§3, §7. Modules
> `va_sim.elevenlabs.voice_catalog`, `voice_store` (`ElevenLabsStore.voices`), `voice_samples` (pure),
> `voice_params`, `voice_json`, `voice_rest`, `voice_control`. Supersedes P03-T04 rule 3 "Any voice id is
> accepted" and closes the last sentence of P03-T04 rule 11.
> 1. Registry: seeded on construction and on `/sim/reset` with 11 premade voices with fixed 20-character
>    `[A-Za-z0-9]` ids; the first is `Karen` (`KAREN_VOICE_ID`, labels `{"accent": "american", "description":
>    "warm", "age": "young", "gender": "female", "use_case": "conversational"}`), then Aria, Roger, Sarah, Laura,
>    Charlie, George, Callum, River, Liam, Charlotte, all with the five label keys. Every voice verifies English;
>    Aria also verifies `es`, Charlotte `de`. `verified_languages` items are `{language, model_id:
>    "eleven_flash_v2_5", accent, locale, preview_url: null}`. Clone and sample ids are seeded `SimIds.alnum(20)`.
> 2. Voice object: `{voice_id, name, samples, category ("premade"|"cloned"), labels, description, preview_url,
>    available_for_tiers: [], settings, sharing: null, high_quality_base_model_ids (premade
>    ["eleven_flash_v2_5", "eleven_multilingual_v2"], clone []), verified_languages (clone []), is_owner (clone
>    true), is_legacy: false, is_mixed: false, created_at_unix (premade null; clone the sim clock's Unix second at
>    add)}`. `samples`: premade null; clone `[{sample_id, file_name, mime_type, size_bytes, hash (sha256 hex),
>    duration_secs (3 decimals or null)}]`. Premade `preview_url` is `<base>/sim/elevenlabs/previews/<id>.mp3`,
>    clone null. Settings default `{stability 0.5, similarity_boost 0.75, style 0.0, use_speaker_boost true,
>    speed 1.0}`.
> 3. Order of checks on every voices route: `xi-api-key` (401 `needs_authorization` / `invalid_api_key`), the
>    armed voices fault (rule 11), validation (422 `{"detail": [{"loc": ["query", name] | ["body", name, ...] |
>    ["body"], "msg", "type"}]}`), lookup (404 `{"detail": {"status": "voice_not_found", "message": "A voice with
>    the voice_id <id> was not found."}}`), then the operation.
> 4. `GET /v2/voices`: `page_size` 1..100 (default 10), `next_page_token`, `search` (case-insensitive substring of
>    name, description, any label value, or category), `sort` `created_at_unix|name` (default
>    `created_at_unix`), `sort_direction` `asc|desc` (default `desc` for `created_at_unix`, `asc` for `name`),
>    `voice_type` (`default` → premade; `personal`, `workspace`, `non-default` → cloned; `community`, `saved` →
>    none), `category` `premade|cloned|generated|professional`, `voice_ids` repeated (≤ 100),
>    `include_total_count` `true|false` (default true); unknown parameters are ignored. `created_at_unix` order is
>    the creation sequence (seed order, then clones); `name` order is the casefolded name, ties by sequence. The
>    token is the unpadded url-safe base64 of the next offset in decimal; anything else is 422
>    `["query", "next_page_token"]`. Response `{voices, has_more, total_count (null when include_total_count is
>    false), next_page_token (null when !has_more)}`. Paging is offset-based over the filtered, sorted list at
>    request time, so concurrent deletes may shift pages.
> 5. `GET /v1/voices` → `{"voices": [...]}` in sequence order; `GET /v1/voices/{id}` → the voice (`with_settings`
>    ignored); `GET /v1/voices/{id}/settings` → the settings; `POST /v1/voices/{id}/settings/edit` JSON with any
>    subset of the five settings (stability, similarity_boost, style in [0, 1]; speed in [0.7, 1.2];
>    use_speaker_boost a JSON boolean; `null` and unknown fields ignored), merged into the stored settings for any
>    voice → `{"status": "ok"}`; invalid JSON or a non-object → 422 `["body"]`.
> 6. `POST /v1/voices/add`, multipart: `name` 1..256 characters, `files` repeated uploads 1..25 (missing, non-file,
>    or more than 25 → 422 `["body", "files"]`; more than 26 is a parse failure → 422 `["body"]`), `description` ≤
>    500, `labels` a JSON object of string → string (else 422 `["body", "labels", ...]`),
>    `remove_background_noise` `true|false` in any case (default false). A non-multipart body → 422 `["body"]`.
>    Then the samples (rule 7), then the voice limit: cloned voices ≥ `voice_limit` → 400 `{"detail": {"status":
>    "voice_limit_reached", "message": "You have reached your maximum amount of custom voices (<n> / <n>)."}}`.
>    Success → `{"voice_id": "<new>", "requires_verification": false}`.
> 7. Samples (400 `{"detail": {"status": "invalid_sample", "message": ...}}`): over 25 MB → "Sample '<file>' is
>    larger than 25 MB"; RIFF/WAVE (stdlib `wave`), MP3 (optional ID3v2 tag skipped by its syncsafe size and
>    footer flag, then MPEG-1/2/2.5 Layer III frames walked to the first non-header or truncated frame, summing
>    samples per frame / rate), and FLAC (STREAMINFO total samples / rate; total 0 = unmeasured) are measured;
>    Ogg (`OggS`), MP4/M4A (`ftyp` at offset 4), and WebM (EBML `1A45DFA3`) are accepted unmeasured; anything
>    else or a broken WAV/MP3/FLAC → "Sample '<file>' could not be read as audio"; a measured duration under 1 s →
>    "Sample '<file>' is shorter than 1 second". `mime_type` comes from detection (`audio/wav`, `audio/mpeg`,
>    `audio/flac`, `audio/ogg`, `audio/mp4`, `audio/webm`). Parsing runs in one worker thread under `hold_clock`.
> 8. `POST /v1/voices/{id}/edit`, multipart: the add fields with `files` optional (0..25; when present they
>    replace the whole sample set, pass rule 7, and count as an add/edit); `description` is replaced by what was
>    sent (absent clears it), `labels` only when sent. Premade → 400 `{"detail": {"status":
>    "voice_not_editable", "message": "Premade voices cannot be edited or deleted"}}`; validation precedes the
>    lookup. → `{"status": "ok"}`. `DELETE /v1/voices/{id}`: premade → 400 `voice_not_editable`, clone removed →
>    `{"status": "ok"}`.
> 9. TTS: the WebSocket checks the voice after the query and the key: an unknown voice gets the frame
>    `{"message": "A voice with the voice_id <id> was not found.", "error": "voice_not_found", "code": 1008}`,
>    then close 1008 `voice_not_found`, no record. REST synthesis (all three routes): auth → 422 → 404
>    `voice_not_found` (no record, fault not consumed) → TTS fault → synthesis. Batch STT is unaffected.
> 10. `GET /v1/user/subscription` → `{tier, character_count (sum of TTS record characters), character_limit,
>    can_extend_character_limit: false, allowed_to_extend_character_limit: false, next_character_count_reset_unix
>    (sim clock Unix second + 30 days), voice_slots_used (cloned count), professional_voice_slots_used: 0,
>    voice_limit, max_voice_add_edits, voice_add_edit_counter (adds plus edits with files),
>    professional_voice_limit: 0, can_extend_voice_limit: false, can_use_instant_voice_cloning: true,
>    can_use_professional_voice_cloning: false, currency: "usd", status: "active", billing_period:
>    "monthly_period", character_refresh_period: "monthly_period"}`. Defaults: tier `creator`, character_limit
>    100 000, voice_limit 30, max_voice_add_edits 95. Only `voice_limit` is enforced.
> 11. Fault: `PUT /sim/elevenlabs/voices/fault {"kind": "error"|"rate_limited"|"delay", "delay_ms" = 0, "times" =
>    1}` (204), `GET` → `{"fault": null|{kind, delay_ms, remaining}}`, `DELETE` (204). Applies to every rule 4-8
>    and 10 route after auth (not TTS): `error` → 500 `internal_error` "Simulated voices failure",
>    `rate_limited` → 429 `too_many_concurrent_requests` "Simulated rate limit", `delay` waits `delay_ms` on the
>    sim clock and serves normally; each firing consumes one of `times`.
> 12. Control: `GET /sim/elevenlabs/voices` → `{"voices": [voice object + remove_background_noise, edits]}`;
>    `GET /sim/elevenlabs/voices/requests` → `{"requests": [{id ("voicereq_<n>"), operation
>    (add|edit|delete|settings_edit), voice_id, name, description, labels (null for delete/settings_edit and when
>    not sent), files ([{file_name, mime_type, size_bytes, hash, duration_secs}]), remove_background_noise,
>    settings (settings_edit only: the merged settings), received_ms}]}` for successful mutations only;
>    `GET /sim/elevenlabs/subscription` → the rule 10 body, `PATCH` `{tier? (non-empty), character_limit? ≥ 0,
>    voice_limit? ≥ 0, max_voice_add_edits? ≥ 0}` (unknown fields 422) → 200 with the body;
>    `GET /sim/elevenlabs/previews/{id}.mp3` → `audio/mpeg`, the TTS generator's audio of "Hi, this is
>    <name>." (unknown → 404 `not_found`). `/sim/reset` reseeds the voices and clears clones, records, the fault,
>    the add/edit counter, and subscription changes; `GET /sim/state` `elevenlabs` adds `voices` (registry size).

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

- **Built-in structured-call heuristics** (used when no rule matches a forced tool call). Tool names mirror
  `va_llm.structured.TOOL_NAMES` (defined in `10-llm-gateway.md`; the simulator keeps its own copy and a drift test
  compares them, Decision P03-T06b rule 1):
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

> Decision (2026-10-31): FakeLLM chat completions, rules, faults, and models (P03-T06a). Built from the OpenAI
> contract independently of `va_llm`. Numbered rules:
> 1. Auth: `Authorization: Bearer <key>` on `/v1/*`; `sim-llm-key` (the specs/02 §6 `LLM_DEFAULT_API_KEY` dev value)
>    always authenticates, plus keys added with `POST /sim/llm/api-keys {"api_key"}`. Every error body is OpenAI's
>    `{"error": {"message", "type", "param", "code"}}`; a missing key is 401 type `invalid_request_error`, code null;
>    a wrong key 401 code `invalid_api_key` (the key is not echoed).
> 2. Check order: auth → JSON body (400, code null, for invalid JSON or a non-object) → validation (400; `param` is
>    the field path such as `messages[0].role`; code `missing_required_parameter` or `invalid_value`) → model (404
>    `model_not_found`, param `model`) → armed fault → rule match → reply.
> 3. Request: unknown parameters are accepted. Validated: `model`; `messages` (≥ 1; roles system, developer, user,
>    assistant, tool; content string, list of parts (only `text` parts contribute text), or null for an assistant
>    message with `tool_calls`; a tool message needs `tool_call_id`); `stream`; `stream_options.include_usage`;
>    `tools` (type `function`, name `^[a-zA-Z0-9_-]{1,64}$`); `tool_choice` `none`/`auto`/`required`/named function
>    (`required` or a named function needs `tools`, and the name must be one of them); `temperature` 0–2;
>    `max_tokens`/`max_completion_tokens` ≥ 1 (the latter wins); `response_format.type` text/json_object/json_schema;
>    `n` must be 1.
> 4. `GET /v1/models` → `{"object": "list", "data": [{"id", "object": "model", "created", "owned_by": "sim"}]}` with
>    the shared specs/02 §6 variables `LLM_DIALOGUE_MODEL`, `LLM_ROUTER_MODEL`, `LLM_ANALYSIS_MODEL`,
>    `LLM_FALLBACK_MODEL`, `EMBEDDINGS_MODEL` (in that order, blanks and duplicates dropped; read by
>    `SimSettings`, no new variables), plus models added with `POST /sim/llm/models {"id"}`.
> 5. Tokens: `re.findall(r"\S+\s*", text)` with leading whitespace glued to the first token. Usage:
>    `prompt_tokens` = whitespace words of every message's text and tool-call arguments plus the tool definitions'
>    JSON; `completion_tokens` = content tokens + per tool call (1 + words of its argument JSON). `max_tokens` keeps
>    the first n content tokens, then whole tool calls that still fit; any cut gives `finish_reason: "length"`.
> 6. Pacing (sim clock): a streamed reply sends the role chunk at once, then its first token after `ttft_ms` (rule
>    override, else the configured value) + rule `delay_ms`, then one token every `1 / tokens_per_s` s; content
>    tokens, tool-call header chunks, and argument chunks each count as one token. A non-streamed reply waits as
>    long as the stream would and then answers.
> 7. Stream wire (`text/event-stream`, `data: <json>\n\n`): every chunk has `id` (`chatcmpl-` + 29 alphanumerics,
>    one per response), `object: "chat.completion.chunk"`, `created` (sim clock, s), `model` (echo),
>    `system_fingerprint: "fp_sim"`, `choices: [{index: 0, delta, logprobs: null, finish_reason}]`. Order: delta
>    `{"role": "assistant", "content": ""}`; one `{"content": token}` per token; per tool call a header
>    `{"tool_calls": [{"index", "id": "call_" + 24 alphanumerics, "type": "function", "function": {"name",
>    "arguments": ""}}]}` and the argument string in at most three `{"tool_calls": [{"index", "function":
>    {"arguments"}}]}` parts; an empty delta with `finish_reason` (`stop`/`tool_calls`/`length`); with
>    `include_usage` every earlier chunk carries `"usage": null` and a last chunk has `"choices": []` and `usage`;
>    then `data: [DONE]`. Non-streamed: `object: "chat.completion"`, `choices[0].message` `{role, content (null when
>    only tool calls), refusal: null, tool_calls?}`, `logprobs: null`, `finish_reason`, `usage`.
> 8. Replies: the winning rule's `content`/`tool_calls` (a rule with neither answers the default reply with its own
>    timing); no rule with a named or `required` `tool_choice` calls the named (or first) tool with arguments `{}`
>    (superseded by the structured-call heuristics of Decision P03-T06b); otherwise `"Okay. You said: {last user text,
>    stripped}."`, or `"Hello! How can I help you today?"` without a user message. `tool_choice: "none"` drops every
>    tool call, a rule's included.
> 9. Rules: YAML `{rules: [...]}` as above plus `match.call_id` (compared with the `X-VA-Call-Id` header, specs/10
>    §12). `system_contains` needs every string in the system and developer messages joined by newlines;
>    `last_user_regex` is `re.search` on the last user message (an invalid regex is a load error); `tool_name`
>    matches the named `tool_choice` function, or any offered tool when none is named; `model` is exact.
>    `call_index` n fires only on the n-th (0-based) request that met the rule's other conditions, counted per
>    (rule id, `X-VA-Call-Id` or none) for every rule whose other conditions held, even when an earlier rule won.
>    `respond.tool_calls[].arguments` is an object (sent as compact JSON) or a string (sent verbatim, e.g. malformed
>    JSON); `respond.error` `{status 400–599, body?, headers?}` answers after `delay_ms` (default body: an OpenAI
>    error whose type follows the status: 401 `authentication_error`, 403 `permission_error`, 429
>    `rate_limit_error`, ≥ 500 `server_error`, else `invalid_request_error`). Unknown keys and duplicate ids within
>    one document are load errors (`RulesError` issues `{path, line, column, message}` like scenarios); registering
>    an id that exists replaces the earlier rule at the new position. `SIM_LLM_RULES` files load at startup (an
>    invalid file stops the simulator) and survive reset.
> 10. Faults: `PUT`/`GET`/`DELETE /sim/llm/fault`, body `{kind, times = 1, status?, retry_after_s?, after_tokens?}`
>    (each extra field only for its kind). `rate_limit`: 429 code `rate_limit_exceeded`, `Retry-After:
>    retry_after_s` (default 1). `server_error`: `status` 500 (default), 502, or 503. `timeout`: without
>    `after_tokens` (or on a non-streamed request) no response headers are sent until the client disconnects or
>    the simulator resets (then 504 code `timeout`); with `after_tokens` n a stream sends the role chunk and n tokens
>    and stalls the same way. `disconnect`: a stream closes the connection after the role chunk without `[DONE]`; a
>    non-streamed request sends its headers and closes before the body. `malformed_tool_args`: the next reply with
>    tool calls has every argument string cut to its first half (invalid JSON); replies without tool calls leave it
>    armed. `rate_limit`, `server_error`, and a header-less `timeout` answer before rule matching and never advance
>    a `call_index` counter. Every firing consumes one of `times`.
> 11. Control: `GET`/`DELETE /sim/llm/requests` (`{"requests": [{id, received_at, model, call_id, stream, messages,
>    tools, tool_choice, temperature, max_tokens, response_format, rule_id, fault, status, finish_reason}]}`, one
>    entry per request that passed validation); `GET /sim/llm/rules` (`{"rules": [... rule, "source": file path or
>    "runtime"]}` in match order); `POST /sim/llm/rules` (YAML or JSON `{rules: [...]}` → 201 `{"ids"}`; 415/422 as
>    for scenarios); `DELETE /sim/llm/rules` (runtime rules); `DELETE /sim/llm/rules/{id}` (404 `not_found`);
>    `GET`/`POST /sim/llm/api-keys`; `GET`/`POST /sim/llm/models`. `GET /sim/state` adds `llm` counts (`requests`,
>    `rules`, `models`, `held`). `POST /sim/reset` drops runtime rules, `call_index` counters, the log, the fault,
>    and added keys and models, and releases held requests; file rules stay.

> Decision (2026-10-31): FakeLLM structured-call heuristics and embeddings (P03-T06b). Numbered rules:
> 1. Names: `va_llm` must not share code with the simulator, so `va_sim.llm.heuristics.STRUCTURED_TOOL_NAMES` is
>    the simulator's own copy of the 14 specs/10 §3 names (a test parses them from specs/10); P03-T14 adds a test
>    comparing it with `va_llm.structured.TOOL_NAMES`. Aliases: `select_disposition` → `classify_disposition`,
>    `record_citations` → `extract_citations` (the names the specs/18 prompts use); `record_analysis` shares the
>    `extract_analysis_schema` heuristic.
> 2. A forced (or `required`, first tool) call without a matching rule gets the heuristic's candidate, with keys
>    outside the tool's `properties` dropped, conformed to the tool's `parameters` (`va_sim.llm.schema_fill`): valid
>    parts are kept (numbers clamped into their bounds, strings cut to `maxLength`), invalid parts replaced by the
>    schema default, unknown keys dropped when `additionalProperties` is false, missing `required` keys filled.
>    A tool without a heuristic gets the schema default; a tool without `parameters` gets `{}`. Supported keywords:
>    `type` (string or list), `enum`, `const`, `default`, `minimum`/`maximum`/`exclusiveMinimum`/
>    `exclusiveMaximum`, `minLength`/`maxLength`, `pattern`, `minItems`/`maxItems`, `items`, `prefixItems`,
>    `properties`, `required`, `additionalProperties`, `anyOf`/`oneOf`/`allOf`, local `$ref` (`#/$defs/…`,
>    `#/definitions/…`). Schema default: `default` → `const` → enum (null when listed, else the first value) → null
>    when nullable → a combinator's default → by type (string `examples[0]` or `"x"` × `minLength`; integer/number
>    0 clamped into the bounds; boolean false; array of `minItems` defaults; object of its `required` defaults).
> 3. Text sources: words = `[a-z0-9']+` of the lowercased text, content words drop a fixed English stopword set
>    (yes/no are not stopwords); latest = the quoted `Latest caller message: "…"` of the last user message, else its
>    text; caller text = the `Caller:`/`user:` lines of the user messages plus the latest message, else every user
>    message; transcript = the block after a `Transcript:` or `Conversation (…):` line of the last user message,
>    else every user and assistant message, with role prefixes and `[T{n}|…]` turn ids removed.
> 4. Heuristics: `select_path` scores each `path_id` enum option (except `stay`) by content-word overlap of its
>    `- id: … | condition: … | description: …` system line with the latest message; the maximum wins (ties: first),
>    confidence 8, `alternatives` = other options with overlap (confidence min(10, 2 × overlap)); no overlap picks
>    `stay`, else an option whose id/label contains "fallback", else `none`, else the first option, confidence 5.
>    `score_condition`: 8 when a content word of `Completion condition:` occurs in the caller text, else 2; `met` =
>    score ≥ 7. `extract_variables`, per property type over the caller text: integer = first digits or number word
>    one–twenty; number = first `-?\d+(\.\d+)?` or number word; boolean = first of yes/yeah/yep/sure/correct/
>    affirmative/absolutely (true) or no/nope/negative/nah (false); string = the words after "my name is", "this
>    is", "I'm", "I am", "it's", "it is", else the latest message cut to 80 characters. `classify_answer` reads
>    `Transcript so far: "…"` (else the latest message): voicemail on the §3.4 regex plus `you've reached|mailbox`,
>    ivr on `press \d|please (say|enter)|your call is important`, human when non-empty, else unknown; confidence 0.9
>    (unknown 0.5); boolean properties = is voicemail. `evaluate_guard_rails`: one result per `- name: description`
>    line of the `Rules:` block; a name containing `opt_out` triggers on `stop calling|do not call|don't call|remove
>    me|unsubscribe` in the latest message (confidence 0.95, evidence = the match), others never (0.05, "").
>    Disposition: the `tag` enum value whose words plus its `- TAG: description` line overlap the transcript most,
>    else `FOLLOW_UP_REQUIRED`, else the first; confidence 0.7. `summarize`: every string property = `Summary: ` +
>    the first 30 transcript words; a request without tools whose system text contains `You write concise, factual
>    summaries of phone calls` (specs/18 §3) answers that text as content. Analysis: enums by overlap (else null),
>    booleans/integers/numbers as above, strings = email / phone (`\+?\d[\d\s().-]{6,}\d`) / date regex when the
>    name or description says email / phone / starts with `YYYY`, else the name pattern, else null — all over the
>    transcript. `answer_questions`: one answer per `prefixItems` position (at least `minItems`), a
>    human/voicemail enum by the voicemail regex. `record_sentiment`: positive minus negative word counts of the
>    caller text (net ≥ 3 very_positive, > 0 positive, 0 neutral, < 0 negative, ≤ −3 very_negative), score =
>    clamp(net / 4, −1, 1), frustration on `frustrat|annoy|ridiculous|angry`, no notable moments. Citations: per
>    `variable_name` enum value an analysis value of the item `value` schema, `turn_numbers` = the first turn line
>    containing it (number words count for integers; any yes/no word for booleans), topics [], `statement`.
>    `grade`: booleans true, number fields named `*score*` 80, strings `Simulated evaluation.`, arrays of criteria
>    `max(minItems, 1)` such objects. `update_memory`: the summary text, `facts_set` `{"name": …}` when the caller
>    states a name, empty lists. JSON mode (`response_format`) is not heuristic: tests use rules for it.
> 5. Embeddings `POST /v1/embeddings` (check order: auth → JSON → validation → log entry → model 404 → armed
>    `embeddings` fault → vectors, no pacing): `model`; `input` = string, 1–2048 strings, a token array, or 1–2048
>    token arrays (empty strings are 400 `invalid_value` param `input`); `dimensions` 1–4096 (default 1024);
>    `encoding_format` `float` (default) or `base64` (little-endian float32; the `openai` SDK asks for base64 by
>    default); unknown parameters accepted. Vector: per word (token ids as strings; no tokens hash `""`)
>    `blake2b(token, digest_size=8, person=b"va-sim-embed")`, bucket = first 4 bytes big-endian mod dimensions,
>    sign + when byte 4 is even else −, summed, L2-normalized in float64, emitted as float32. Response `{"object":
>    "list", "data": [{"object": "embedding", "index", "embedding"}], "model", "usage": {"prompt_tokens",
>    "total_tokens"}}` with tokens = whitespace words (token arrays: their length).
> 6. Control: `GET`/`DELETE /sim/llm/embeddings` (`{"requests": [{id, received_at, model, inputs, dimensions,
>    encoding_format, status, fault}]}`, one per request that passed validation); `/sim/state` `llm` adds
>    `embeddings`; reset clears the log. The fault body gains `endpoint` `chat` (default) | `embeddings`; an
>    `embeddings` fault fires only on embeddings requests and allows `rate_limit`, `server_error`, and a
>    header-less `timeout` (422 otherwise); chat faults never fire on embeddings.

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

> Decision (2026-10-29): `va_sim.scenario` fills the gaps of the schema above. Every mapping rejects unknown keys
> and duplicate keys. `match` needs exactly one of `to`/`inbound_from` (E.164). `answer.after_ms` is required when
> `result` is `answered` (the default) and forbidden otherwise; `answered_by` requires an answered call and
> `beep_at_ms` requires `answered_by: machine`; inbound scenarios must not set `answer` (the platform answers).
> `transfer_target.answer_after_ms` follows the same rule as `answer.after_ms`. A step is a one-key mapping;
> defaults: `wait_agent_speech` `min_ms: 0`, `then_silence_ms: 700`, `timeout_ms: 10000`; `say.language: en`;
> `dtmf.inband: false` with `digits` matching `^[0-9A-D*#w]+$` (`w` = 500 ms pause); `silence.ms` required;
> `expect_hangup` `by: any` (`agent` | `caller` | `any`), `timeout_ms: 10000`; `hangup: {}`. `profile` defaults to
> `fast`, `id` matches `^[a-z0-9][a-z0-9_-]{0,63}$`, and relative `llm_rules` resolve against the scenario file's
> directory. Errors list every issue top to bottom as `<source>:<line>:<column>: <path>: <message>` (1-based; the
> position of the offending key, or of the deepest existing parent for a missing field), e.g.
> `scenarios/x.yaml:9:15: steps[3].silence.ms: Input should be greater than or equal to 0`.

> Decision (2026-10-29): scenario caller runner (P03-T03c). (1) Start: one `CallerScript` per leg
> (`va_sim.telnyx.caller_script`), started once, when a media session of the leg has sent `connected` and `start`,
> if the leg has a scenario with something to run; a restarted stream does not restart it. Outgoing and inbound legs
> (role `caller`) speak `greeting_audio` and then run `steps`; transfer legs (role `transfer_target`: legs created
> by `transfer`, and `link_to` legs dialled to a number without a scenario of its own, which already follow the
> linked call's `transfer_target`) run only `transfer_target.steps`. A transfer leg without its own stream runs on
> its bridged peer's connected session (the platform hears the transfer target on the original call's stream),
> started at the bridge or when the peer's stream connects. No media → no script (`script: null`). (2) All audio
> (each utterance registered in document order as `register_utterance(text, language, text.split())`, synthesized
> at the stream rate with seed `utterance_seed(id)`; in-band DTMF tones; the greeting WAV) is rendered in one worker
> thread while the accelerated clock is held (`va_sim.tasks.hold_clock`), so preparation takes no virtual time. An
> exhausted utterance id space fails that step. (3) `MediaFormat.encode` writes the stream encoding (μ-law, or L16
> big-endian at 8 or 16 kHz); `POST /sim/telnyx/calls/{ccid}/say` shares the renderer, so L16 8 kHz streams now get
> L16 audio. (4) `greeting_audio` ending in `.wav` (any case) is a 16-bit PCM WAV path (any channel count, averaged;
> any rate, linearly resampled; ≤ 60 s); otherwise it is text spoken in `en`. A relative WAV path resolves against
> the scenario file's directory (scenarios POSTed to the control API keep the path as given). A missing or invalid
> WAV fails the greeting (`detail` says why) and the steps still run; a WAV greeting's timeline entry has
> `utterance_id: null` and `text` = the file name. (5) Agent speech: a 20 ms frame of played-out agent audio
> (8 kHz) is speech when its RMS exceeds 1024 (-30 dBFS; 256 before Decision P05-T15 below). Per step a fresh detector tracks the current turn (speech ms; silent
> gaps shorter than the gap keep the turn, speech after a longer silence starts a new turn) and the silence since
> the last speech frame. (6) Steps record `{index, type, status, started_ms, ended_ms, detail}` (+ `utterance_id`
> for `say`, set when queued), status `pending | running | ok | timeout | failed | cancelled`, times in sim
> `virtual_ms`; a timeout or failure is recorded and the next step runs. `wait_agent_speech`: gap =
> `then_silence_ms`; ok when the turn is ≥ max(`min_ms`, 20) ms and the silence ≥ `then_silence_ms` (0: as soon as
> the turn qualifies), else `timeout` after `timeout_ms`. `say`: queued at once, or with `barge_in_after_agent_ms`
> N on the tick after the agent's turn (gap 700 ms) reaches max(N, 20) ms — `timeout` (nothing spoken) when that
> does not happen within 10 000 ms; ok when its last byte has been sent (`ended_ms` = the timeline `end_ms`).
> `dtmf`: digits split at `w`; each run is sent (one `dtmf` event per tick, or with `inband: true` only tones: per
> digit 100 ms tone + 100 ms silence as caller audio) and each `w` then waits 500 ms; ok once the last digit has gone
> out. `silence`: the caller stays quiet for `ms`. `expect_hangup`: ok when the leg ends by `by` (`agent` ⇔
> `hangup_source` is the platform side: `caller` for outgoing legs, `callee` for incoming; `caller` ⇔ the remote
> side; `any`), `failed` with `detail: "hung up by <hangup_source>"` otherwise, `timeout` after `timeout_ms`.
> `hangup`: the far end hangs up (`normal_clearing`, `hangup_source` = the remote side: `callee` outgoing, `caller`
> inbound). (7) When the leg ends, every running or pending step (and greeting) becomes `cancelled` with `detail:
> "call ended"`; when the session the script runs on stops while the leg is up, `"media stream stopped"`. Script
> status `running` → `done` | `cancelled`; `/sim/reset` cancels scripts. (8) The call summary and timeline add
> `script: null | {role, status, greeting: null | {source: "text" | "wav", status, utterance_id, started_ms,
> ended_ms, detail}, steps: [...]}`.

> Decision (2026-11-02): agent speech level (P05-T15). Every media-worker call now plays a background bed by default
> (`static`, -40 dBFS, frame RMS up to 436, specs/07 §10 Decision P05-T15), which the former 256 level counted as
> agent speech, so `wait_agent_speech` never saw silence. `AGENT_SPEECH_LEVEL` is 1024 (frame RMS, -30 dBFS) for
> the runner, and the timeline's `first_agent_audio_ms` uses the same frame-RMS test instead of a peak sample (the
> static bed peaks at ≈ 1 500). The `office`/`cafe`/`restaurant` beds (-22 dBFS, frame RMS up to ≈ 9 600) overlap
> TTS speech levels and are not told apart. FakeElevenLabs STT session records add `filter_background_audio`.

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

> Decision (2026-11-01): FakeStripe core (P03-T07a). `va_sim.stripe` serves the Stripe subset below on the FakeStripe
> port; checkout, subscriptions, invoices and their lifecycle events follow in P03-T07b. The simulator never imports
> `stripe`; the official SDK is only the acceptance client in tests.
> 1. Auth: `Authorization: Bearer <key>` or Basic (`<key>:` base64, as `curl -u` sends it). `sk_test_sim` (specs/02 §6
>    `STRIPE_SECRET_KEY`) and keys added through the control API are accepted; missing → 401 `invalid_request_error`
>    "You did not provide an API key…", unknown → 401 "Invalid API Key provided: sk_test_****<last4>". Objects are global
>    to the simulator (Stripe accounts are not modelled).
> 2. Requests: POST bodies and GET query strings are form-encoded with Stripe bracket notation (`metadata[k]=v`,
>    `a[0][b]=c`, `a[]=x`; keys exactly `0..n-1` form a list; a scalar/nested conflict is 400 with `param`). Values stay
>    strings except declared integers (`parameter_invalid_integer`) and booleans (`true`/`false`,
>    `parameter_invalid_boolean`). A parameter an endpoint does not know (nested keys included, e.g.
>    `invoice_settings[x]`) is 400 `parameter_unknown` "Received unknown parameter: x"; `expand` is accepted and ignored.
>    Missing required parameters are 400 `parameter_missing` "Missing required param: x.".
> 3. Responses: objects carry `id`, `object`, `created` (sim clock, unix s), `livemode: false`. Every answer has
>    `Request-Id: req_<14 alnum>` and `Stripe-Version` (the request's header, else `2026-10-28.endive`, the version
>    stripe-python 16.1 pins). Errors are `{"error": {type, message, code?, doc_url? (docs.stripe.com/error-codes/…),
>    decline_code?, param?, …}}`; an unknown id is 404 `resource_missing` "No such <kind>: '<id>'"; any other `/v1` URL,
>    or a known path with another method, is 404 "Unrecognized request URL (<METHOD>: <path>)…".
> 4. `Idempotency-Key` (POST only) is stored per API key with a fingerprint of method, path and decoded parameters. A
>    replay answers the stored status and body with `Idempotent-Replayed: true` and `Original-Request: <request id>` and
>    creates or emits nothing; the same key with different parameters is 400 `idempotency_error`; a replay while the
>    first request still runs is 409 `idempotency_error` code `idempotency_key_in_use`. Error answers are stored too
>    (401 is decided before the key is looked at). A handler crash releases the key. Reset clears the cache.
> 5. Ids come from the seeded `SimState.ids`: `cus_`+14, `pm_`+24, `pi_`+24, `ch_`+24, `bps_`+24, `evt_`+24 alphanumerics;
>    client secrets `{pi_id}_secret_`+25.
> 6. Payment methods: magic ids `pm_card_visa`, `pm_card_mastercard` (succeed), `pm_card_chargeDeclined`
>    (`generic_decline`), `pm_card_chargeDeclinedInsufficientFunds` (`insufficient_funds`),
>    `pm_card_authenticationRequired` (needs authentication). Using a magic id (attach, customer `payment_method`,
>    payment intent `payment_method`) creates a new `pm_…` card (brand/last4/funding from the table, `exp_month` 12,
>    `exp_year` sim year + 3) that remembers the behaviour. `POST /v1/payment_methods/{id}/attach {customer}` (attaching a
>    method of another customer is 400), `GET /v1/payment_methods/{id}`, `GET /v1/customers/{id}/payment_methods`
>    (`type=card` only, newest first, `limit` 1..100 default 10, `starting_after`; `ending_before` is 400).
> 7. Customers: `POST /v1/customers` (email, name, description, phone, metadata, payment_method,
>    `invoice_settings[default_payment_method]`; a magic id given as both `payment_method` and the default means the
>    method just created; a rejected create leaves nothing behind), `GET`/`POST /v1/customers/{id}`. Updates merge
>    metadata key-wise (empty value deletes a key, `metadata=` clears), an empty string unsets a scalar, and the default
>    payment method must be attached to the customer (else 400 `resource_missing` on that param). No `customer.*`
>    events are emitted.
> 8. Payment intents: `POST /v1/payment_intents` (amount ≥ 50 else `amount_too_small`; currency 3 letters, lowercased;
>    customer; payment_method; confirm; off_session `true`/`one_off`/`recurring`, only with `confirm=true`; description;
>    metadata; receipt_email; setup_future_usage; payment_method_types; automatic_payment_methods), `GET
>    /v1/payment_intents/{id}`. Unconfirmed: `requires_payment_method`, or `requires_confirmation` with a method.
>    Confirming uses the given method, else the customer's default (none → 400 `parameter_missing` `payment_method`).
>    Outcome: the armed config `decline` (the `SimState.stripe_decline` switch above) declines with its `decline_code`
>    (default `generic_decline`); else the card decides. Success → `succeeded`, `amount_received`, `latest_charge`
>    `ch_…`, event `payment_intent.succeeded`. Decline (or an authentication card off-session, code and decline_code
>    `authentication_required`) → HTTP 402 `card_error` `card_declined` with `decline_code`, `charge`, `payment_method`,
>    and the stored intent (`requires_payment_method`, `last_payment_error`), event `payment_intent.payment_failed`. An
>    authentication card on-session → 200 `requires_action` with `next_action.type = use_stripe_sdk`, no event.
> 9. `POST /v1/billing_portal/sessions` (customer required and existing, return_url) → `billing_portal.session` whose
>    `url` is `{base URL the client used}/sim/stripe/portal/{id}`; that page is a small HTML page naming the customer
>    with a link back to `return_url` (404 `not_found` for an unknown id).
> 10. Events: `{id, object: "event", api_version, created, data: {object, previous_attributes?}, livemode: false,
>    pending_webhooks: 1, request: {id, idempotency_key}, type}`; `data.object` is a snapshot at emit time and
>    `api_version`/`request` come from the causing request. The log (newest 10 000) is served by `GET /v1/events/{id}`
>    and `GET /v1/events` (`type` filter, paging as rule 6). Each event is POSTed to config `webhook_url` (default
>    `SIM_STRIPE_WEBHOOK_URL`) with `Content-Type: application/json; charset=utf-8` and `Stripe-Signature:
>    t=<sim-clock unix s>,v1=<hex HMAC-SHA256(secret bytes, "t.body")>` signed per attempt (secret default
>    `whsec_sim`); delivery and retries (1 s, then 3 s on the sim clock, then dropped) are the FakeTelnyx rules
>    (`va_sim.delivery`). `webhook_url: null` disables delivery; events are still logged.
> 11. Control (FakeStripe port only): `GET`/`PATCH /sim/stripe/config` `{decline, decline_code, webhook_url,
>    webhook_secret}` (strict, 422 `invalid_request`; only sent fields change), `POST /sim/stripe/api-keys
>    {"api_key": "sk_…"}` → 204, `GET /sim/stripe/events` (`{"events"}` oldest first), `POST /sim/stripe/events
>    {"type", "object"}` → 201 the emitted event (for refunds, disputes, …), `GET /sim/stripe/webhooks`
>    (`{"deliveries"}`). `/sim/reset` drops every object, event, idempotency entry, added key and delivery and restores
>    the config; `/sim/state` adds `stripe: {customers, payment_methods, payment_intents, events, webhook_deliveries}`.

> Decision (2026-11-01): FakeStripe checkout, subscriptions, invoices (P03-T07b). Builds on the P03-T07a pipeline
> (auth, form decoding, idempotency, `card_error` declines, signed events). Current API shapes: subscription period
> dates live on the items; invoices reference their subscription through `parent.subscription_details`.
> 1. Seeded ids: `prod_`+14, `price_`+24, `cs_test_`+58, `li_`+24 (checkout item), `seti_`+24, `sub_`+24, `si_`+14,
>    `in_`+24, `il_`+24. Every object has `id`, `object`, `created` (sim clock), `livemode: false`.
> 2. `POST /v1/products` (`name` required; optional custom `id`, duplicate → 400 `resource_already_exists`;
>    `description`, `metadata`, `active`), `GET /v1/products/{id}`. `POST /v1/prices`: `currency` (lowercased),
>    `unit_amount` ≥ 0, exactly one of `product` | `product_data[name, description, metadata]`, optional
>    `recurring[interval]` day|week|month|year with `recurring[interval_count]` 1..(365|52|12|1) (licensed only),
>    `nickname`, `lookup_key` (unique; a duplicate is 400 unless `transfer_lookup_key=true` moves it), `metadata`,
>    `active`. `GET /v1/prices/{id}`, `GET /v1/prices` (filters `product`, `active`, `type`, `lookup_keys[]`; paging
>    as P03-T07a rule 6). A referenced object that does not exist is 404 `resource_missing` (as P03-T07a).
> 3. `POST /v1/checkout/sessions`: `mode` payment|subscription|setup and `success_url` required; `cancel_url`;
>    `customer` (must exist) or `customer_email` (not both); `client_reference_id`; `metadata`;
>    `line_items[n]{price | price_data{currency, unit_amount, product | product_data, recurring?}, quantity ≥ 1}`;
>    `payment_intent_data{setup_future_usage, description, metadata}` (payment mode only);
>    `subscription_data{description, metadata}` (subscription mode only); `currency`; `payment_method_types[]`;
>    `expires_at` (30 min..24 h ahead, default +24 h, reported but not enforced). Payment mode needs ≥ 1 line and only
>    one-time prices; subscription mode needs ≥ 1 recurring price, all recurring prices on one interval and count
>    (one-time items are billed on the first invoice); setup mode takes no line items and needs `currency` or
>    `payment_method_types`; all lines share one currency. Stored prices must be active; `price_data` is stored as an
>    inactive price only after the whole request validated (a rejected request stores nothing). The session's `url`
>    is `{request base}/sim/stripe/checkout/{id}`; `amount_total` = Σ `unit_amount`×`quantity` (null in setup mode).
>    Line items are listed by `GET /v1/checkout/sessions/{id}/line_items` (`item` objects embedding the full price).
>    `POST /v1/checkout/sessions/{id}/expire` (open only, else 400) → `expired`, `url: null`,
>    `checkout.session.expired`.
> 4. Hosted page `GET /sim/stripe/checkout/{id}` (control route): unknown → 404 `not_found`, not open → 409
>    `conflict`; no `outcome` → 200 HTML; `outcome=cancel` expires it (`checkout.session.expired`) and answers 303 to
>    `cancel_url` (200 HTML without one); `outcome=success[&card=<P03-T07a magic id>]` (default `pm_card_visa`;
>    unknown → 400) completes it and answers 303 to `success_url` with `{CHECKOUT_SESSION_ID}` substituted; any other
>    outcome → 400. The page passes 3-D Secure, so authentication cards succeed; a decline (the card or the armed
>    `decline` config; setup mode never declines) answers 402 HTML naming the decline code and creates nothing. The
>    customer is the session's, else a new one with `customer_email`; the card becomes a new `pm_…`. Payment mode: a
>    `succeeded` PaymentIntent with `payment_intent_data` fields (`payment_intent.succeeded`); the payment method is
>    attached to the customer only with `setup_future_usage` and the customer's default never changes. Subscription
>    mode: the payment method is attached and becomes the subscription's `default_payment_method`; rule 5 with
>    `subscription_data` (`customer.subscription.created`, `invoice.paid`). Setup mode: the payment method is attached;
>    a `succeeded` off-session SetupIntent (`GET /v1/setup_intents/{id}`; `setup_intent.succeeded`). Then
>    `checkout.session.completed` (session `complete`, `payment_status` paid | no_payment_required, `url: null`,
>    `customer`, `customer_details`, and `payment_intent` | `subscription` + `invoice` | `setup_intent`). Page events
>    carry `request: {id: null, idempotency_key: null}`.
> 5. `POST /v1/subscriptions`: `customer` and `items[n]{price, quantity}` required (active recurring prices on one
>    interval, count and currency); `default_payment_method` (attached to the customer); `metadata`; `description`;
>    `cancel_at_period_end`; `payment_behavior` allow_incomplete (default) | default_incomplete | error_if_incomplete;
>    `off_session`, `proration_behavior` accepted. The first period is `[now, now + interval×count)` (months and years
>    keep the anchor day, clamped to the month end: Jan 31 → Feb 28 → Mar 31). It is invoiced at once
>    (`subscription_create`) and charged to `default_payment_method`, else the customer's invoice default (API charges
>    are off-session: an authentication card declines with `authentication_required`). Nothing due → paid without a
>    charge; no payment method or `default_incomplete` → `incomplete`, invoice open, no invoice event; a decline under
>    `error_if_incomplete` → 402 `card_error` (400 without a payment method) storing nothing; another decline →
>    `incomplete`, invoice open with `attempt_count` 1 (`invoice.payment_failed`); success → `active`
>    (`invoice.paid`). `customer.subscription.created` (final status) precedes the invoice event. `GET
>    /v1/subscriptions/{id}`; `GET /v1/subscriptions` (filters `customer`, `price`, `status`: default excludes
>    canceled, `all`, `ended` = canceled, or one status). `POST /v1/subscriptions/{id}` (not canceled, else 400):
>    `items[n]{id (required, an item of the subscription), price, quantity}` — a new price must be recurring with the
>    same interval, count, and currency (else 400 `parameter_invalid`, a sim limitation; adding or removing items is
>    not supported) —, `proration_behavior` create_prorations (default) | none | always_invoice,
>    `cancel_at_period_end` (`cancel_at` = period end and `canceled_at` = now, or both null), `metadata` (merge rules
>    of customers), `default_payment_method` (attached; empty unsets), `description`. A changed item prorates
>    unless `none`: a credit −round_half_up(old×qty×f) and a charge +round_half_up(new×qty×f), f = unused share of the
>    item's period; `create_prorations` keeps them for the next invoice, `always_invoice` bills them (plus pending
>    lines) now (`subscription_update`; a failed charge makes the subscription `past_due`). A change emits
>    `customer.subscription.updated` with `previous_attributes` holding the old values of the changed top-level
>    fields (`items` = the whole old items list); `always_invoice` emits the invoice event first; a no-op emits
>    nothing. `DELETE /v1/subscriptions/{id}` (`invoice_now`, `prorate`, `cancellation_details{comment, feedback}`
>    accepted; parameters come from the query string and the form body) → `canceled`, `canceled_at` = `ended_at` =
>    now, pending prorations dropped, `customer.subscription.deleted`; already canceled → 400.
> 6. Invoices: status open | paid; `total` = Σ lines; the customer's `balance` (negative = credit) is applied when
>    the invoice opens (`amount_due` = max(total + balance, 0), the rest stays as credit; `starting_balance` /
>    `ending_balance`); lines carry `pricing.price_details` and `parent.subscription_item_details{subscription,
>    subscription_item (null for one-time checkout items), proration}`; `number` = `{invoice_prefix}-NNNN`. `GET
>    /v1/invoices/{id}`, `GET /v1/invoices` (filters `customer`, `subscription`, `status`; paging), `POST
>    /v1/invoices/{id}/pay` (`payment_method` resolved like P03-T07a rule 6 for the invoice's customer, else the
>    subscription's; open only, else 400 "Invoice is already paid"; none → 400): success → paid (`invoice.paid`) and a
>    `past_due`/`incomplete` subscription becomes `active` (`customer.subscription.updated`); a decline → 402
>    `card_error` with `invoice`, `attempt_count`+1, `invoice.payment_failed`. Invoice charges create no
>    PaymentIntent and no `payment_intent.*` event (sim simplification).
> 7. Control: `POST /sim/stripe/subscriptions/{id}/renew` with optional strict body `{"fail": bool}` (default false;
>    422 `invalid_request` otherwise) ends the current period of an `active`/`past_due` subscription (other statuses
>    → 409 `conflict`, unknown → 404 `not_found`); the sim clock does not move. `cancel_at_period_end` → `canceled`,
>    `ended_at` = period end, `customer.subscription.deleted`, answer `{"subscription", "invoice": null}`. Otherwise
>    every item moves to `[old end, old end + interval)` (anchor day kept) and a `subscription_cycle` invoice (items
>    plus pending prorations) is charged off-session (`fail: true` forces `generic_decline`; the armed config, the
>    card, or no payment method also fail): paid → `active`; failed → `past_due`, invoice open. Events: the invoice
>    event, then `customer.subscription.updated` (`previous_attributes`: `items`, plus `status` when it changed).
>    Answer 200 `{"subscription", "invoice"}`.
> 8. `/sim/reset` also drops products, prices, checkout sessions, setup intents, subscriptions, pending prorations,
>    and invoices; `/sim/state` `stripe` adds `products`, `prices`, `checkout_sessions`, `subscriptions`, `invoices`,
>    `setup_intents`.

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
| `interruptibility_levels` | Parametrized 0–4 with backchannel "uh-huh" (0.4 s) and a full sentence interruption | level 0: no interruption ever; 1: only interruptions ≥ 4 words (not all backchannel) and ≥ 1.2 s; 2: sentence interrupts, "uh-huh" doesn't; 3: ≥ 1 word interrupts; 4: "uh-huh" interrupts (rules per `07-media-pipeline.md`) |
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
| `webhooks_call_completed_and_live` | Org endpoint (`call.completed`, `call.ended`) + test send; `outbound_basic` call with `webhook`, `webhook_events: ["queue","latency"]`, `metadata`; receiver answers the first `call.completed` with 500 | every request verifies (`X-VA-Signature`, `X-Webhook-Signature`, headers); only `call.queued`/`call.latency`/`call.completed` reach the per-call webhook, live bodies per `19` §5; `call.completed` retried once ~10 s later with the same event id and body; delivery rows and `call.webhook_delivery` log entries; final Call Object fields and equality with `GET /v1/calls/{id}`; endpoint envelopes per `19` §7 |
| `stop_via_api` | Long call; `POST /v1/calls/{id}/stop` at 10 s | hangup within 1 s; `ended_by=api` |
| `worker_crash_recovery` | Start call; `docker kill` the media container mid-call (harness restarts it) | workflow backstop hangs up within 30 s; `end_reason=worker_crash`; post-call completes with partial transcript |
| `recording_and_post_call` | record=true, summary_prompt, dispositions, analysis_schema | recording copied to MinIO; batch STT invoked; summary, disposition_tag in allowed set, analysis fields typed; `call.completed` after recording_ready |
| `citations_and_evals` | citation schema + eval setup on call | `citations.ready` and `evals.completed` webhooks; cited utterances reference real turn indices/timestamps |
| `memory_across_calls` | Persona with memory; call 1 learns name; call 2 to same number | call 2 system prompt (sim LLM log) includes memory summary/fact; memory_changes rows for call 1 |
| `experiment_assignment` | Agent with two versions, running experiment 50/50; 40 calls with `agent_id` only | both arms assigned (each ≥ 10), assignment deterministic per call id hash, `experiment_arm_id` stored |

(For `scheduled_call` the harness uses `start_time = now + 20 s` against real Temporal; durable-timer logic for long
delays is covered by Temporal time-skipping integration tests.)

> Decision (2026-11-02): until the docker harness of `tests_e2e/` exists (P05-T17), `inbound_basic` (task runner)
> is covered in process by `apps/media_worker/tests/test_inbound_flow.py`: FakeTelnyx delivers its webhooks to a
> running edge app, whose accept answers with the media stream; a served media worker runs `CallRuntime` over
> FakeElevenLabs and FakeLLM and the end-of-call routine on Postgres (edge and media share one Redis). It also covers
> the maintenance message (specs/16 §4 Decision P05-T12d). The ring-to-answer histogram is asserted against the
> edge's 3 s accept budget there (the 1 s target belongs to the harness, which measures it without xdist load).

> Decision (2026-11-02): e2e harness (P05-T17b). (1) Seed: `POST /internal/test/orgs` (envelope, no credentials,
> `x-public: e2e seed (VA_ENV local/test only)`, audit-exempt) answers `NOT_FOUND` unless `VA_ENV` is `local` or
> `test`. Body (unknown keys rejected): `name` (default `E2E org`), `plan_id` (`build`), `dollars` (decimal ≥ 0,
> `50`), `settings` (sparse org settings, validated as `OrgSettings`; errors `INVALID_INPUT` with `field`
> `settings.<path>`), `voices` (1-10 `{name, provider_voice_id}`, provider `elevenlabs`), `numbers` (0-10 `{e164,
> telnyx_phone_number_id?, capabilities = ["voice"], inbound_config = {}}`; `inbound_config` validated as
> specs/16 §4 `InboundConfig`, errors with `field` `numbers[i].inbound_config.<path>`). One `tenant_session` of the
> new org creates it without members (`created_at` backdated 30 days so new-account limits do not apply), the
> settings, a `grant` ledger row (idempotency key `test-org:{org_id}`), the voices, the active numbers, and one live
> `*` API key named `e2e seed`; 201 `{data: {org_id, api_key, voices: [{id, name}], numbers: [{id, e164}]}}`. A
> requested number still active in another org (a previous run against a restarted simulator, whose number inventory
> starts over) is first set `released` in that org's own tenant session. (2) Isolation: the harness never calls
> `/sim/reset` (it would drop the platform Call Control application created by `telnyx_bootstrap`). Each test has a
> `run_token`; scenario ids become `<name>-<token>`, `match.to`/`match.inbound_from` a fresh random US number, and
> LLM rule files substitute `{{run}}` (rule ids) and `{{marker}}` (the call prompt's `[[SCENARIO:<name>-<token>]]`).
> Rules are deleted and ordered simulator numbers released after each test; scenarios stay registered (the control
> API has no delete) but are inert. Loading a scenario applies its `profile` to the vendor simulators. (3)
> Completion: until the phase-06 webhook dispatcher delivers webhooks (and `tests_e2e/receiver.py` arrives with it;
> phase 06: `tests_e2e/support/e2e_receiver.py`, see Decision P06-T08), flows wait for `GET /v1/calls/{id}` `queue_status == "complete"` and read the event log `GET
> /v1/calls/{id}/events`; `assertions.webhook_events` entries are checked against that log, by fixed message
> (`call.completed` = "Call completed.", `call.ended` = "Call ended.") or by a category no other event type shares,
> and an entry the log cannot identify is a failure. `outbound_basic` sends no `webhook` for the same reason;
> `assertions.answered_by` `machine` compares with the Call Object's `voicemail`. (4) Platform overhead per caller
> turn = sim timeline `latency_ms` − (`eot_ms` + `stt_final_ms` + `llm_ttft_ms` + `tts_ttfa_ms`) of the matching
> `"E2E: {n}ms"` `call.latency` event (the n-th measured utterance pairs with the n-th summary); `eot_ms` is the
> endpointer's deliberate silence wait, so it counts as policy time. p95 values are nearest-rank. (5)
> `inbound_basic` runs the task runner (`inbound_config.prompt`) until pathway execution arrives in phase 07; the
> number is ordered from the FakeTelnyx inventory onto the platform Call Control application through the Telnyx
> REST API and seeded with the org. (6) Presigned recording URLs name `minio:9000`, which the host cannot resolve;
> SigV4 signs the `Host` header, so the harness connects to `localhost:9000` and sends the original `Host`. (7)
> `make e2e` runs `scripts/dev_up.sh e2e` (rebuilding images) before `pytest tests_e2e -m e2e`; a stack that does
> not answer stops the session with exit code 3 instead of skipping. The pure evaluator and rewriting helpers live
> in `tests_e2e/support/` and are unit-tested by `tests_e2e/unit/` under `make test`. (8) The served simulators
> (`apps/sim`) start from a fresh random seed (`startup_seed()`, 32 bits) instead of seed 0: the platform persists
> simulator ids (Telnyx webhook event ids are the edge's dedup key), so a restarted simulator replaying the ids of
> its previous run had its `call.initiated` dropped as a duplicate. `/sim/reset` still accepts a `seed`, and
> in-process tests build their state with an explicit seed.

> Decision (2026-11-02): voicemail, IVR, and transfer flows (P05-T17c). (1) The FakeTelnyx timeline adds
> `first_agent_speech_ms`: the sim `virtual_ms` of the first agent frame *played out* to the far end whose frame RMS
> exceeds the agent-speech level (`va_sim.telnyx.agent_speech`, 1 024; the `static` bed stays below it), `null` until
> then. `voicemail_leave_message` asserts `first_agent_speech_ms − answered_ms ≥ 4 000` as the "agent audio after the
> beep" evidence (both flows send `background_track: none` as well); the TTS log since the scenario was loaded must
> equal the message alone (no opener, no dialogue, no LLM request carries the call id). (2) `voicemail_hangup`
> measures from the FakeTelnyx `call.machine.premium.detection.ended` event `at` to the first `hangup` command `at`
> (summary ISO timestamps, wall clock under the realtime e2e sim): ≤ 2 000 ms, no TTS text, no LLM request. (3)
> `amd_ivr_mode`: `ivr_mode=true` disables AMD (`va_domain.voicemail.amd_mode`), so the scenario's machine answer
> produces no `call.machine.*` event and no voicemail action even with `voicemail.action=hangup`; the DTMF evidence
> is the summary `dtmf_sent` (`1` then `2`, ISO `at` gap ≥ 500 ms, after the answer). The far-end hang-up ends it
> `user_hangup`. (4) `cold_transfer` runs on an outbound task call through the `transfer_call` system tool (a FakeLLM
> rule answers the caller's "human" with the tool call) to `transfer_phone_number`; the pathway Transfer Call node
> arrives in phase 07. A transfer-target leg runs no scenario steps and the primary's caller script stops with the
> media stream, so the harness hangs the bridged caller up itself (`POST /sim/telnyx/calls/{ccid}/hangup`, the remote
> side) 3 s of sim time after `streaming_stop`. Assertions: transfer to the configured number, no `hangup` before
> `transfer`, `streaming_stop` after it, the final line in the TTS log, `call.transfer_started {type: cold, target}`
> followed by `call.transferred` (category `transfer`, empty data), Call Object `transferred_to`, `transfer_duration
> > 0`, `call_ended_by = SYSTEM`, and a price between the metered amount (connected minutes × connected rate +
> transfer minutes × transfer rate of the seed `build` plan) and that amount plus the minimum attempt top-up. The
> mark-echo ordering of the final line before `transfer` is covered in process by `apps/media_worker/tests/
> test_transfer.py` and `test_transfer_flow.py`. (5) `call.voicemail` and `call.transfer_started` rows carry fixed
> messages (`va_media.voicemail.VOICEMAIL_MESSAGE`, `va_media.transfer.TRANSFER_STARTED_MESSAGE`), so the evaluator
> recognises them in `webhook_events` (`call.amd` shares the `voicemail` category, so flows select the `call.voicemail` row by
> its message); `call.transferred` shares its category and has no fixed message, so flows check
> its row directly.

> Decision (2026-11-02): barge-in, interruptibility, and worker crash flows (P05-T17d). (1) The FakeTelnyx timeline
> adds `clears`: the sim `virtual_ms` of every agent `clear` frame received on the media stream, in arrival order.
> Caller *voice onset* = the utterance's timeline `start_ms` + `MARKER_MS` (the 180 ms FSK marker preamble that
> precedes every synthesized caller body). (2) `barge_in` runs at `interruptibility: 4`, so the decision needs no STT
> words (levels 1-3 are proven by `interruptibility_levels`). The "clear within 120 ms of voice onset" row is checked
> as onset → first `clear` ≤ the level's speech minimum (150 ms) + Silero's onset (the synthetic voice's first
> 180 ms syllable rises from -12 dB and is sometimes not yet called speech, measured ≈ 200 ms late, plus the start
> confirmation `START_WINDOWS` × `WINDOW_MS`) + the 120 ms reaction budget + one 20 ms frame each way: no barge-in
> policy can clear before the speech minimum has elapsed. The 120 ms reaction itself is the tight check below. The `call.interrupt` row's `reaction_ms` (speech start → clear inside the pipeline) must
> stay ≤ speech minimum + one VAD window + 120 ms. The spoken length is checked at the decision's `played_ms` (≥
> 1 200 ms by the scenario). `played_chars` is the length of `text_spoken`, which keeps whole words only, so it
> must lie between the whole-word prefix of the opener at 90 % of `played_ms / 60` characters and 110 % of
> `played_ms / 60`. `text_spoken` is not public, so the
> "history contains only spoken text" row is read from the first FakeLLM request carrying the caller's words: the
> assistant message right before it is a prefix of the opener (no longer than `played_chars`) followed by
> `va_llm.prompts.INTERRUPTION_MARK`, and no message contains the unspoken remainder. The Call Object keeps the full
> planned text with `interrupted: true`. (3) `interruptibility_levels`: one call per level 0-4 with the 300-character
> opener; the caller says "uh-huh" 1 500 ms into the agent's speech, stays quiet 2 500 ms, then says a 10-word
> question 800 ms into the agent's (current) speech. Windows are each utterance's `[start_ms, end_ms + 500)`.
> Level 0: no `clear`, no `call.interrupt`, no interrupted turn. Levels 1-3: no `clear` for "uh-huh" (backchannel,
> and shorter than the levels' speech-only minimum), exactly one for the question no earlier than the level's
> `min_speech_ms` after voice onset, one `call.interrupt`. Level 4: one `clear` for each utterance (each ≥ 150 ms
> after onset) and two `call.interrupt` rows (the barged "uh-huh" becomes a real turn whose reply the question then
> interrupts). Thresholds are imported from `va_voice.interrupt.LEVEL_RULES`. (4) `worker_crash_recovery`: the
> harness `docker kill`s (SIGKILL) the compose `media` container (found by its compose labels) once the agent has
> answered the caller and the turn writer has flushed the opener and the caller turn, and starts it again (waiting
> for `/readyz`) in a `finally`. The FakeTelnyx stream failure does not end the call: `call_presence` still sees the
> `call:{id}:owner` key (15 s TTL) after the 5 s grace, so the heartbeat watch decides: `call:{id}:hb` outlives the
> worker by up to `HEARTBEAT_TTL_S` (10 s), the watch needs it missing for `HEARTBEAT_MISSING_AFTER_S` (20 s) checked
> every `HEARTBEAT_POLL_INTERVAL_S` (3 s), then `end_call` and `hangup_call` run. The bound is therefore kill → leg
> end ≤ 10 + 20 + 3 + 2 s = 35 s (and ≥ 20 s, proving the watch ended it); the table's "30 s" is the typical case.
> The call ends `worker_crash` (`call_ended_by` `ERROR`, specs/05 §9), the platform side sends the `hangup`, and
> post-call processing completes with the transcripts written before the kill.

> Decision (2026-11-03): webhooks e2e flow (P06-T08). (1) Receiver: `tests_e2e/support/e2e_receiver.py` (the
> `tests_e2e/receiver.py` of §3.5 and Decision P05-T17b (3)), a Starlette app served by real uvicorn in the pytest
> process (fixture `receiver` → `ServedReceiver(receiver, url)`), bound on `E2E_RECEIVER_BIND` (default `0.0.0.0`),
> port `E2E_RECEIVER_PORT` (default 0 = ephemeral). The containers reach it as `http://{E2E_RECEIVER_HOST}:{port}`
> (default `host.docker.internal`): `docker-compose.e2e.yml` maps that name to the docker host
> (`extra_hosts: host-gateway`) on `api` and `webhooks`, and pins `WEBHOOK_ALLOW_PRIVATE: "true"` on `api` (specs/29
> §2 Decision P05-T17a (5)). Replies are scripted per path and optionally per event type (FIFO, then 200); every
> request is recorded with raw body, lower-cased headers, monotonic and wall receipt times, and the status answered.
> (2) Secret: the flow rotates the fresh org's signing secret (`POST /v1/webhooks/signing-secret/rotate`, the only
> response with the plaintext) before any delivery. Verification is independent of the signer: any `v1` of
> `X-VA-Signature` (one per active secret, so the dual-sign window verifies too) equals HMAC-SHA256(secret,
> `"<t>.<raw body>"`), `t` equals `X-VA-Timestamp` and lies within 300 s of the receipt time,
> `X-Webhook-Signature` is the HMAC of the raw body with the current secret, `X-VA-Event-Id` equals `X-Event-Id`
> and starts `evt_`, `User-Agent` is `VoiceAgents-Webhooks/1.0`, `Content-Type` JSON. Every received request must
> verify. (3) Flow: an org endpoint for `call.completed` and `call.ended`, whose test send must answer
> `succeeded: true, status_code: 200` (the preflight that proves the stack reaches the receiver); then the
> `outbound_basic` scenario with `webhook=<receiver>/call`, `webhook_events: ["queue", "latency"]`, `metadata`,
> `record=true`; the receiver answers the first `call.completed` on `/call` with 500; the flow waits (event-driven)
> for a 200-answered `call.completed` and for every `call.latency` row of the event log. (4) Assertions: per-call
> event types ⊆ {`call.queued`, `call.latency`, `call.completed`}, one `call.queued`, latency `seq`s equal the event
> log's, live bodies exactly the §5 keys with the call's `metadata`, a latency message `^(E2E|STT|LLM|TTS): \d+ms$`;
> two `call.completed` requests with the same event id and byte-identical bodies, 500 then 200, 9-25 s apart (10 s
> ± 10 % plus poll and send time); `GET /v1/webhooks/deliveries` shows the per-call row (`bland`) succeeded on
> attempt 2 and the endpoint row (`envelope`) on attempt 1; the event log holds the delivery's `call.webhook_delivery`
> entries (`warning` attempt 1 "failed: 500 (attempt 1), retrying in", `info` attempt 2 "Webhook delivered to
> <host>: 200", never the URL path); the Call Object carries every §4 field, `status completed`, `completed true`,
> the metadata, alternating transcripts that start with the first sentence, and equals `GET /v1/calls/{id}` on every
> key except the per-read presigned `recording_url` and `recording_expiration`; the endpoint got one `call.ended`
> (§5 body, `end_reason agent_end_call`) and one `call.completed` (data = the per-call Call Object), as §7
> envelopes matching the request headers. `outbound_basic` keeps its event-log path.

> Decision (2026-11-04): pathway e2e flows (P07-T12c). (1) Template: no dashboard templates exist before phase 17
> (specs/25 §3), so the bundled restaurant reservation template is `tests_e2e/pathways/restaurant_template.json`
> (Bland camelCase graph with `global_config`): Greeting → (label "Caller wants to book a table reservation") Take
> Reservation (static text, `extractVars` `party_size` integer / `time` / `name`, all required, `before_response`) →
> (`alwaysPick`) Book Table (Webhook POST of the three variables, `responseData` `status`/`ref`, `routeOnStatus`
> every class → Goodbye) → Goodbye (End Call, static templated speech). Validation refuses a templated host in a
> webhook URL, so the flow rewrites every Webhook node's `url` to the run's receiver path before creating the
> pathway, then publishes it and sends `pathway_id` with `wait_for_greeting: false`. (2) FakeLLM: decision prompts
> (router, extraction) carry neither the call prompt nor a scenario marker, so routing uses FakeLLM's edge-label
> word heuristic (and `alwaysPick`), and extraction a rule matching `tool_name: extract_variables` plus a caller
> phrase only that flow says (rule ids still carry `{{run}}`); every agent line is static text, so no dialogue rule
> is needed. (3) Receiver: `Reply(status, body)` answers `body` as JSON (Webhook nodes read `responseData` from it).
> Webhook nodes run in the media worker, so `docker-compose.e2e.yml` gives `media` the receiver's `extra_hosts` and
> `WEBHOOK_ALLOW_PRIVATE: "true"` (specs/29 §2 Decision P05-T17a (5)). (4) Assertions: `pathway_restaurant_template`
> = route targets exactly `reserve, book, goodbye`, one `call.extraction` at `reserve`, Call Object `variables` ⊇
> `party_size 4, time "7pm", name` plus the receiver's `status`/`ref`, exactly one receiver POST whose JSON body is
> the three typed values, one `call.webhook_node` (`ok`, 200, route `goodbye`), the templated End Call speech
> synthesized and last in the transcript, agent hangup; `webhook_node` = one POST with the call id, the assistant
> turn right after the first caller turn is "Your booking is confirmed, reference A1.", `call.webhook_node` `ok`
> 200 with `variables_set` `status`/`ref`, route targets `confirm, bye`.

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

> Decision (2026-11-02): single-pod concurrency check (P05-T17e). The phase-05 one-pod variant of the call swarm is
> `tests_e2e/test_concurrent_calls.py` (marker `capacity`; runs against the compose stack's single media-worker
> process and single-process simulator). An org on the `scale` plan (concurrency 100, `calls_send` capacity 50, so no call
> is refused) funded with $500 dispatches 50 `concurrent_call` scenarios at once (realistic latency profile, three
> caller turns, the agent ends the call). Gated: zero dropped calls (every scenario's assertions hold), all 50
> attached to the media worker at the same time (peak of `va_media_active_calls`), zero platform underruns (delta of
> `va_audio_underruns_total`; definition: specs/07 §9 Decision P05-T17e), and platform overhead p50 ≤ 150 ms over
> all 150 caller turns (specs/00 §5; overhead per specs/30 §5 Decision P05-T17b). Reported, not gated: overhead p95
> (vs 250 ms), simulator voice-to-voice p50/p95, and `va_event_loop_lag_ms` p50/p99 between two scrapes (specs/07
> §21's 10 ms target). The 1 000-call swarm, `scripts/load/`, and the §21 lag/lateness asserts stay with phase 20.
>
> Decision (2026-11-02): until P05-T17e2 the capacity flow is opt-in (P05-T17e1): `make e2e` selects
> `-m "e2e and not capacity"`, `make e2e-capacity` runs `-m capacity -s`, and `E2E_CONCURRENT_CALLS` (integer
> 1..50, default 50) sets the call count. Baseline on the c7i.2xlarge dev host (whole compose stack on 8 vCPU):
> 50 calls — none dropped, 50 attached at once, overhead p50/p95 335/895 ms, 667 underruns, loop lag p99 ≤ 150 ms;
> 10 calls — none dropped, overhead p50/p95 92/255 ms, 2 underruns, loop lag p99 ≤ 5 ms. P05-T17e2 (media-worker
> capacity work) makes the 50-call gates pass and moves the flow into `make e2e`.
>
> Decision (2026-11-03): the capacity flow is part of `make e2e` (P05-T17e2): `make e2e` runs `-m e2e` (the flow keeps
> its `capacity` marker, so `make e2e-capacity` still runs it alone, with `-s` for the report). The report adds the
> overhead p50 of each caller turn across calls (the calls' turns are nearly simultaneous; turn 1 is the slowest).
> Result on the same host after the media-worker work (batched VAD, per-window gate, uvloop, GC tuning, backlog-proof
> stream origin; specs/07 §5.3, §10.1, §16, §21 Decisions P05-T17e2): 50 calls — none dropped, 50 attached at once,
> 0 underruns, overhead p50/p95 82–87/110–137 ms (by turn ≈ 95/85/65 ms), loop lag p99 ≤ 10 ms, ≈ 39 s; 10 calls —
> overhead p50/p95 76/103 ms. The simulator runner also runs on uvloop, and its per-frame agent-speech check is an
> integer `sum(x²) > 1024² × n` (same decision as the RMS rule, no float conversion).

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

> Decision (2026-11-01): vendor-client live smoke (P03-T16).
> 1. `tests_live/test_vendor_clients.py` covers the phase-03 clients with seven tests:
>    `test_telnyx_account_read_live` (balance; `voiceagents` call control apps and outbound voice profiles; the
>    `voiceagents-{VA_ENV}` messaging profile, `VA_ENV` defaulting to `staging`; `LIVE_FROM_NUMBER` lookup when set),
>    `test_number_search_live` (US, `limit=5`, E.164, no purchase), `test_elevenlabs_voices_live`,
>    `test_elevenlabs_tts_ws_live` (ulaw_8000 audio whose alignment characters spell the sent text; a second context
>    cancelled after its first chunk yields no further audio and still ends with `is_final`),
>    `test_elevenlabs_stt_ws_live`, `test_elevenlabs_batch_stt_live` (`scribe_v2`), and `test_telnyx_inference_live`
>    (streaming chat with TTFT and no fallback, a streamed tool call, a forced `record_sentiment` structured call,
>    embeddings of `EMBEDDINGS_DIMENSIONS` unit vectors). The other rows of the table land with the phase that builds
>    the flow (calls, AMD, transfer, recording, webhook signature: telephony phases; SMS: messaging; clone: voices).
> 2. Every test carries `@pytest.mark.live`; the module is skipped unless `LIVE_TESTS=1` exactly, and each test skips
>    when its key (`LIVE_TELNYX_API_KEY`, `LIVE_ELEVENLABS_API_KEY`, `LIVE_LLM_API_KEY`) is empty, naming the variable.
>    Keys come from the process environment only (no `.env` loading) and never appear in a repr or assertion message.
> 3. Endpoints are the specs/02 §6 "real:" values; models are the specs/02 §6 defaults read from `LlmSettings`,
>    `DEFAULT_TTS_MODEL`, and `SttConnection` (the batch model `scribe_v2` is spelled out until the worker settings
>    declare `ELEVENLABS_BATCH_STT_MODEL`).
> 4. The STT fixture is `tests_live/fixtures/stt_phrase.wav` (PCM16 mono 8 kHz, ElevenLabs TTS `pcm_8000` of a fixed
>    phrase in a premade voice) plus `stt_phrase.json` (`text`, `voice_id`, `model_id`, `sha256` of the WAV). A live
>    run that finds neither file synthesizes and saves both; the developer commits them. A missing half or a sha256
>    mismatch fails the test instead of regenerating. The realtime test sends the fixture as μ-law in 20 ms frames
>    paced in real time, then 0.5 s of silence and a manual commit.
> 5. WER = word-level Levenshtein distance / reference words after lowercasing, joining apostrophes, and splitting on
>    other punctuation (`tests_live/support/live_wer.py`, no new dependency); realtime and batch STT must be ≤ 10 %.
> 6. `tests_live` is in pytest `testpaths`, so `make test` collects the suite and reports the live tests skipped;
>    `tests_live/test_live_gate.py` (not live) proves that in a child pytest, with and without `LIVE_TESTS=1`.

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
