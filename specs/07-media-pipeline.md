# 07 — Real-Time Media Pipeline

Packages: `packages/va_voice` (pure, transport-agnostic primitives; no Telnyx/ElevenLabs imports) and
`apps/media_worker` (`va_media`: server, transports, `CallSession` orchestration). The conversation logic lives in
`va_engine` (`11-pathways-engine.md`); the media worker drives it through the `ConversationEngine` interface (§16).

## 1. Process model

- One `media-worker` process per pod (2 vCPU, 4 GiB), uvicorn ASGI app with `uvloop`, single event loop.
- One `CallSession` per active call, run as an `asyncio.TaskGroup` with these tasks:
  `inbound_reader` (Telnyx WS → frames), `vad_loop`, `stt_sender`, `stt_receiver`, `turn_manager`, `tts_receiver`,
  `outbound_clock` (20 ms sender), `control_listener` (Redis pub/sub), `ownership_renewer`, `heartbeat`,
  `duration_watchdog`, `event_flusher`. A failure in any task cancels the group; the session's `finally` block runs
  the end-of-call routine (§18).
- CPU-heavy work (VAD inference) runs inline (≈ 0.3 ms per 32 ms window); no thread pool on the hot path. The ONNX
  `InferenceSession` is created once per process (`intra_op_num_threads=1`, `inter_op_num_threads=1`,
  `execution_mode=ORT_SEQUENTIAL`) and each call keeps its own recurrent state tensor.
- Admission control: `MEDIA_MAX_CALLS_PER_POD` (60). At the limit the WS upgrade is refused with close code 4503.
  Because Telnyx does not retry a refused stream connection, the readiness probe flips to failing at 90 % capacity
  (54 calls) so the load balancer stops sending new streams well before the hard limit, and the worker publishes
  `media:pods` load every 2 s.

## 2. WebSocket endpoints (`va_media.server`)

| Path | Peer | Auth | Purpose |
|---|---|---|---|
| `/ws/telnyx` | Telnyx | Ed25519 header + `token` (`06` §4.2) | Primary and warm-proxy legs (`leg` query: `primary` default, `warm_proxy`) |
| `/ws/telnyx/listen` | Telnyx | same | Listen-only post-transfer transcript legs |
| `/ws/widget` | Browser widget | widget session token (`22-web-widget.md` §6) | Web voice transport (PCM 16 kHz) |
| `/ws/sim` | `va_sim` caller | `test_mode` token | Simulator caller transport (same protocol as Telnyx) |
| `/healthz`, `/readyz`, `/metrics` | k8s/Prometheus | none (cluster-internal) | |

Readiness fails when draining, when active calls ≥ 90 % of `MEDIA_MAX_CALLS_PER_POD`, or when Redis is unreachable.

## 3. Session attach sequence

1. Verify auth; parse `call_id`, `leg`.
2. `SET call:{id}:owner {POD_ID} NX PX 15000` — for `leg=primary`. If held by another pod → close 4409 (duplicate
   stream). Secondary legs (`warm_proxy`, listen) must attach to the owner's `CallSession`. The `MEDIA_PUBLIC_HOST`
   ingress uses consistent hashing on the `call_id` query parameter (`29-infrastructure.md`, annotation
   `nginx.ingress.kubernetes.io/upstream-hash-by: "$arg_call_id"`), so every leg of a call reaches the same pod.
   If a secondary leg nevertheless arrives at a non-owner pod (hash ring changed by a scale event), that pod closes
   it with 4421; the owner then hangs up the proxy leg and performs a cold transfer instead of a warm one (listen
   legs: post-transfer transcript is skipped and a `call.error` event with code `listen_leg_misrouted` is emitted).
3. Load `call:{id}:ctx` (CallContext JSON). Missing → read `calls.config` from Postgres and rebuild context inline.
4. Wait for Telnyx `connected` and `start` (timeout 5 s). Record `media_format` (PCMU 8 kHz or L16 16 kHz).
5. Open ElevenLabs STT and TTS sockets concurrently (`08`, `09`); both must be ready within 2 s, else retry once,
   else fail the call with the error path.
6. Subscribe to `call:{id}:control` and `call:{id}:carrier`; read `call:{id}:carrier_state` for state that arrived
   before subscription (answered, amd).
7. Emit `call.started`; transition handled by the edge (answered) — the worker only sets `started_at` if null.
   (Decision P06-T07 under specs/19 §1: `call.started` carries `{direction, media_format}` and is emitted right
   before the pipeline runs; the edge always sets `started_at` first, so the media worker writes nothing.)
8. Start greeting logic (§11).

> Decision (2026-11-02): media WS attach (P05-T07a).
> 1. `va_media.server` serves `/ws/telnyx`. Close codes are sent after `accept()` (a refusal before it is an HTTP
>    403 without a code), and a refused stream is closed without reading a message. Checks in order:
>    query (`call_id` UUID, one non-empty `token`, `leg` `primary` (default) or `warm_proxy`) → Ed25519
>    `verify_media_ws` over `{scheme}://{MEDIA_PUBLIC_HOST}{path}?{raw query}` (scheme `ws` in `local`/`test`,
>    `wss` elsewhere: `va_common.stream_tokens.media_ws_scheme`, the same function the dial uses) → stream token
>    (`verify_stream_token`, current or previous secret, bound to `call_id`) → an `Authorization` header, when
>    present, must equal `Bearer <query token>`; any failure closes 4401 (reason logged, never the token). The
>    `calls.status` check of specs/28 §6.2 runs with the CallContext load (P05-T07b). Then: `leg != primary` →
>    4421 (secondary legs attach to the owner session once warm transfers exist; until then every one takes the
>    misrouted fallback); draining → 4503; the call already attached on this pod → 4409; `MEDIA_MAX_CALLS_PER_POD`
>    reached → 4503; `SET NX PX 15000` held by any pod → 4409; Redis error → 1011. Each refusal counts
>    `va_media_admission_rejected_total{reason}` (`auth`, `misrouted_leg`, `draining`, `duplicate`, `capacity`,
>    `redis`, `start_timeout`).
> 2. Step 4: after the lock the worker reads until `start` (`connected` is optional) for 5 s of the injected
>    clock; a timeout (counted `start_timeout`), `stop`, or a malformed message closes 1008, a disconnect closes
>    nothing; the lock is released and no session starts.
> 3. Renewal (§19): every 5 s a Lua compare-and-`PEXPIRE 15000`. A renewal returning 0 is a loss at once; a
>    renewal that raises is retried at the next tick and counts as a loss once no renewal succeeded for the 15 s
>    TTL. Loss ends the session with `end_reason='worker_lost'`, `ended_by='error'` and closes the WS 1000 without
>    releasing the key (it is not ours). Otherwise the session end releases with a Lua compare-and-`DEL`.
> 4. Heartbeat: `SET call:{<id>}:hb <ISO-8601 UTC> EX 10` at session start, then every 3 s; Redis errors are
>    logged and retried; the key is deleted at session end.
> 5. `media:pods`: `HSET media:pods <POD_ID> {"active_calls","draining","updated_at"}` every 2 s from a
>    supervised task, `HDEL` at shutdown; Redis errors are logged. `va_media_active_calls{transport="telnyx"}`
>    follows the attached calls.
> 6. Readiness `capacity` fails from `ceil(0.9 × MEDIA_MAX_CALLS_PER_POD)` attached calls (54 of 60, 1 of 1);
>    admission refuses only at the maximum. The lifespan refuses to start (before opening anything) without
>    `TELNYX_PUBLIC_KEY` (`local` falls back to `.sim/telnyx_public_key`, shared with the edge through
>    `va_common.settings.TelnyxPublicKeySettings`) or `STREAM_TOKEN_SECRET`.
> 7. The session (`va_media.session.CallSession`) runs a body seam next to the renewer and the heartbeat; the
>    first to finish ends it (`SessionEnd(cause, end_reason, ended_by)`; `va_media_calls_ended_total{end_reason}`
>    uses the end reason, else the cause). Until the transport and pipeline (P05-T08 onward) replace it, the body
>    reads the stream and ends with `stream_closed` on Telnyx `stop` or a disconnect. A body exception closes the
>    WS 1011 after the same cleanup.

> Decision (2026-11-02): media session channels and drain (P05-T07b).
> 1. Step 3 runs after step 4, because the org comes from the `start` frame's `call_control_id` when the Redis
>    context is missing (`start` is not media, so the call is still refused without reading media).
>    `va_media.context.DbCallContextLoader`: `call:{<id>}:ctx` present → org = its `org_id`; absent → the system
>    `CallLegLookupRepo.by_call_control_id` must return the same call with role `primary`, else 4401 reason
>    `call_unknown`. The `calls` row is then read in `tenant_session(org)`: missing → 4401 `call_unknown`;
>    `status` not in `dialing`/`ringing`/`in_progress` → 4401 `call_status` (specs/28 §6.2, also when a context
>    was stored). A missing context is rebuilt with `va_db.call_context.build_call_context` and saved (a save
>    error is logged, the call proceeds). Database / Redis failures close 1011 (reasons `database` / `redis`).
>    The ownership lock is released on every refusal after it was taken.
> 2. Step 6 uses ONE pod-wide pub/sub connection (`va_media.channels.ChannelHub`, supervised task `channels`),
>    not one per session (a pooled connection per call would exhaust the pool at 60 calls). `subscribe(call_id)`
>    subscribes `call:{<id>}:control` and `call:{<id>}:carrier` and returns once Redis confirmed both (2 s on the
>    injected clock, else 1011 reason `subscribe`); only then is `HGETALL call:{<id>}:carrier_state` read
>    (`va_media.carrier_state.CarrierSnapshot`; a Redis error closes 1011 `redis`). Subscribe-before-snapshot:
>    an event published after the confirmation is delivered, one stored before is in the snapshot, one in both is
>    harmless (the snapshot is state, events are changes). The session end unsubscribes. The hub task is the only
>    reader of the connection; its read errors are logged and retried after 1 s (redis-py re-subscribes on
>    reconnect).
> 3. Per call (`CallFeed`): carrier events are parsed as `va_domain.carrier.CarrierEvent` into a 256-entry queue
>    (overflow drops the oldest, `media.carrier_overflow`); malformed events and events of another call are
>    dropped (`media.carrier_invalid`), redeliveries of the last 512 `telnyx_event_id`s are dropped silently.
> 4. Control messages are `va_domain.control` models (discriminated on `type`, unknown fields rejected, `id`
>    required, `text`/`phone_number` non-blank); an invalid message is dropped (`media.control_invalid`, the
>    text is never logged). The session task `control_listener` hands them in arrival order to a
>    `ControlHandler`; `True` → `SET call:{<id>}:control_ack:<id> 1 EX 30` (written even when the message ended
>    the session; a Redis error is logged); a handler exception fails the session (1011). Until the pipeline and
>    transfers exist, the default handler acknowledges `stop` (`end_reason='api_stop'`, `ended_by='api'`),
>    `credits_exhausted` (`insufficient_credits`, `system`) and `drain_notice` (logged), and leaves `say`,
>    `inject_context`, `transfer`, `takeover` unacknowledged (`media.control_unsupported`).
> 5. Drain (§19): every live session gets a local `drain_notice` (delivered to its listener, never published,
>    never acknowledged), the worker waits for its calls up to `MEDIA_DRAIN_TIMEOUT_S` on the injected clock, then
>    ends the remaining sessions with `end_reason='worker_lost'`, `ended_by='error'` (logged
>    `media.drain_timeout`) and waits up to 10 s more for their cleanup. New streams keep being refused 4503.
> 6. `va_media_admission_rejected_total{reason}` gains `call_unknown`, `call_status`, `database`, `subscribe`.

## 4. Audio formats and framing (`va_voice.audio`)

| Stream | Format | Frame |
|---|---|---|
| Telnyx inbound (default) | PCMU 8 kHz mono | 20 ms = 160 bytes (Telnyx may deliver other sizes; we re-frame) |
| Telnyx outbound (default) | PCMU 8 kHz mono | 20 ms = 160 bytes per `media` message |
| hd_audio inbound/outbound | L16 16 kHz mono, **big-endian** on the wire | 20 ms = 640 bytes |
| To Scribe | `ulaw_8000` (default) or `pcm_16000` (hd) | 100 ms batches (5 frames) per `input_audio_chunk` |
| From ElevenLabs TTS | `ulaw_8000` (default) or `pcm_16000` little-endian (hd) | arbitrary chunk sizes → re-framed to 20 ms |
| VAD input | float32 PCM 16 kHz | 512 samples (32 ms) + 64-sample context |
| Widget | PCM16LE 16 kHz | 20 ms = 640 bytes |

Codec helpers (numpy, precomputed at import):

- `ULAW_TO_PCM16: np.ndarray[int16]` shape (256,) — ITU-T G.711 μ-law decode (bias 0x84, invert bits).
- `PCM16_TO_ULAW: np.ndarray[uint8]` shape (65536,) indexed by `int16 + 32768` — G.711 encode with clipping at 32635.
- `ulaw_decode(b: bytes) -> np.ndarray[int16]`, `ulaw_encode(x: np.ndarray[int16]) -> bytes`.
- `l16be_to_pcm(b)`: `np.frombuffer(b, dtype='>i2').astype(np.int16)`; `pcm_to_l16be(x)`: `x.astype('>i2').tobytes()`.
- Resampling 8 k → 16 k for VAD: `soxr.ResampleStream(8000, 16000, 1, dtype='int16', quality='LQ')` per call
  (stateful, streaming). 16 k → 8 k for widget-to-telephony is not needed (widget uses `pcm_16000` end-to-end).
- Unit tests: round-trip encode/decode error ≤ 1 quantization step for all 65 536 inputs; known vectors (0 → 0xFF,
  32 767 → 0x80, −32 768 → 0x00).

`Framer` collects arbitrary byte chunks and yields exact 20 ms frames; `flush()` pads the remainder with codec
silence (`0xFF` for μ-law, zeros for L16).

> Decision (2026-11-01): P04-T01 module layout is `va_voice.audio.codec` (tables, `ulaw_*`, `l16be_*`, plus
> `l16_to_pcm`/`pcm_to_l16(x, byte_order)` for `TELNYX_L16_BYTE_ORDER` and `pcm16le_to_pcm`/`pcm_to_pcm16le` for
> ElevenLabs `pcm_16000` and the widget; VAD float32 is `x / 32768`), `formats` (`AudioFormat`, `ULAW_8000`,
> `L16_16000`, `PCM16LE_16000`, `FRAME_MS`), `framing` (`Framer` with `feed`/`flush`/`reset`/`pending`,
> `split_frames`, `join_frames`), `resample` (`Resampler.process`/`flush`, one-shot `resample`; both 8 k↔16 k
> directions are supported) and `levels`; everything is re-exported from `va_voice.audio`. A `Framer` keeps a
> dangling odd byte of a 16-bit format until the next chunk (frames are always whole samples). dBFS is RMS relative
> to full scale 32 767 (the background-asset convention of `scripts/generate_audio_assets.py`); silence and empty
> signals are `-inf`. The tables are tested to be bit-identical with the simulator's independent codec.

## 5. Inbound path

### 5.1 Reorder buffer

Telnyx does not guarantee order. `ReorderBuffer(window=5)` keyed on `int(media.chunk)`: emits in order; if the next
expected chunk is missing while 5 later chunks are buffered (≈ 100 ms), it inserts one silence frame for the gap and
continues (counts `va_audio_gap_frames_total`). Duplicates (chunk ≤ last emitted) are dropped. Only `track` inbound
frames are processed on primary legs.

> Decision (2026-11-02): the `track` filter lives in the Telnyx transport (specs/06 §8 Decision P05-T08); frames
> without a `track` field are treated as inbound.

> Decision (2026-11-01): `va_voice.reorder.ReorderBuffer(fmt=ULAW_8000, *, window=5, first_chunk=1, max_gap=50,
> observer=...)` is transport-agnostic (callers pass `int(media.chunk)` and the payload; Telnyx chunks start at 1).
> `push(chunk, payload)` returns the frames now ready as `OrderedFrame(chunk, payload, filled)`. Gap fill is purely
> count-based (no timer): while the expected chunk is missing and `window` later chunks are held, one silence frame
> (format silence byte × 20 ms) is emitted per missing chunk. Late frames (chunk below the next expected one,
> including chunks whose slot was already filled with silence) and repeats of a held chunk are dropped and counted in
> `ReorderStats.duplicates` only. A hole wider than `max_gap` chunks (1 s) is skipped without silence (`resyncs`) so a
> sequence jump cannot flood the pipeline. `drain()` at stream `stop` emits every held frame, filling interior gaps.
> Metrics (specs/27 §3.3): `va_audio_reorder_total` counts frames that arrived ahead of the expected chunk,
> `va_audio_gap_frames_total` counts silence fill frames (formerly named `media_gap_frames_total` here).

### 5.2 Fan-out

Each in-order frame goes to: (a) `stt_sender` queue (raw codec bytes — no transcoding), (b) `vad_loop` (decoded,
resampled to 16 kHz), (c) local recording buffer only when `test_mode` (Telnyx records real calls).

### 5.3 Silero VAD (`va_voice.vad.SileroVad`)

- Model: Silero VAD v5 ONNX (`models/silero_vad.onnx`, MIT, fetched by `scripts/fetch_models.sh` from the
  `snakers4/silero-vad` repository path `src/silero_vad/data/silero_vad.onnx` at tag `v5.1.2`, sha256 recorded in
  `models/MANIFEST.json` on first fetch and verified on every later fetch).

> Decision (2026-10-25): the fetch logic lives in `scripts/fetch_models.py` (typed, tested);
> `scripts/fetch_models.sh` is the entry point and only runs it via `uv run --no-sync`. The manifest is
> `{"version": 1, "models": [{"name", "path", "url", "source", "tag", "license", "sha256", "bytes"}]}` (sorted keys,
> 2-space indent). A download whose sha256 differs from the recorded one is rejected and not written; a manifest
> pinned to another url/tag is rejected. `--check` verifies without downloading (exit 1 on missing, unrecorded, or
> mismatching files); `--update` re-records a new digest or pin. The `.onnx` is git-ignored, the manifest is committed.
- I/O: inputs `input` float32 `[1, 576]` (64 samples of context from the previous window + 512 new samples, values in
  [−1, 1]), `state` float32 `[2, 1, 128]`, `sr` int64 scalar `16000`; outputs `output` float32 `[1, 1]`
  (speech probability) and `stateN` float32 `[2, 1, 128]`. State starts as zeros, context starts as zeros.
- Decision with hysteresis: `p ≥ 0.5` → speech frame; `p < 0.35` → non-speech frame; between → keep previous state.
- Speech start: ≥ 3 consecutive speech windows (96 ms) → `SpeechStarted(t_start)` where `t_start` is the first window start.
- Speech end candidate: first non-speech window after speech → `SilenceStarted(t_last_voiced_end)`; resets if speech resumes.
- Energy floor: windows with RMS < −55 dBFS are forced non-speech (protects against model false positives on line noise).
- Local noise gate (§10) is applied to the VAD path only.
- Reset state when the call leg changes (warm proxy uses its own instance).

> Decision (2026-11-01): P04-T03 layout. `va_voice.vad_model.SileroModel.load(models_dir)` verifies the file
> against `models/MANIFEST.json` (`verify_model_file`, also used by the media-worker `vad` readiness check) before
> opening one onnxruntime CPU session per process (1 intra-op thread, sequential); it rejects any signature other
> than the v5 `input,state,sr` → `output,stateN` above. `va_voice.vad.SileroVad(model, executor)` holds one leg's
> state, context, and pending samples; `feed(pcm16k)` accepts any chunk size, keeps the remainder below 512
> samples, and runs all completed windows in ONE hop to `VadExecutor` (a thread pool of `MEDIA_VAD_THREADS`
> threads, specs/02 §6); the decision logic (`VadDecider`) runs on the loop. Feeds on one instance are serialised;
> the pool works on copies of the state, committed only when the batch completes (a cancelled feed drops its
> audio and leaves the stream consistent). Events are `VadEvent(kind, at_ms, probability, speech)` with kinds
> `probability` (every window, `at_ms` = window start), `speech_start` (= `SpeechStarted`, `at_ms` = first of the 3
> windows) and `speech_end` (= `SilenceStarted`, `at_ms` = end of the last speech window); "resets if speech
> resumes" is a new `speech_start` after the start rule fires again. `at_ms` is stream audio time (windows × 32 ms
> since the stream start or `reset()`), never wall time. Windows under the energy floor still run the model so the
> recurrent state stays continuous. The fixture WAVs of §22 are generated (`packages/va_voice/tests/support/
> vad_fixtures.py`): tests may not call ElevenLabs and the simulator's TTS emits tones, so "speech" is the specs/30
> §3.1 formant synthesis at 16 kHz, noise is the `office` background asset, plus digital silence and DTMF.
> Measured with the production thresholds: starts within +12 ms of the voiced body, ends +56..+98 ms after it (the
> model's hangover), no speech on silence, office noise (max p 0.09), or DTMF (max p 0.15); ≈ 0.1 ms per window.
>
> Decision (2026-11-03) P05-T17e2: pod-wide batched inference. One pool hop per leg feed cost ≈ 1 550 thread
> handoffs per second at 50 calls (GIL re-acquisition after every ONNX call), and the queueing behind them drove the
> 50-call underruns. `VadModel` is now `infer_batch(samples (B, 576), state (2, B, 128)) -> (probabilities (B,),
> states (2, B, 128))` (Silero v5 takes a batch dimension; rows are independent streams, results equal B single
> runs within 1e-5, measured bit-identical); `SileroModel.infer` is a batch of one. `VadExecutor.infer(model,
> samples, state)` queues one window; the first request of an event-loop turn schedules a flush on the next turn,
> which stacks the queued windows of a model (at most `MAX_BATCH` = 64 per batch) into one `infer_batch` on the
> pool, with at most `MEDIA_VAD_THREADS` batches in flight (new windows wait for the next free slot, so an idle pod
> adds no wait and a busy one batches more). A waiter cancelled before the flush is left out; one cancelled after
> it has its result discarded; a failing batch fails each of its waiters and the executor keeps serving; closing it
> fails queued windows with `RuntimeError("VAD executor closed")`. `SileroVad.feed` awaits its completed windows
> one by one (the recurrent state chains them) and still commits state/context only when all of them completed.
> Measured on one media worker (8 vCPU dev host, whole compose stack): 50 calls went from 155–667 underruns to 0.

## 6. Endpointing (`va_voice.endpointing.Endpointer`)

Inputs: VAD events, STT partial transcripts (with arrival times), node timing settings. Output: `EndOfTurn(t)`.

Base silence by `resumption_speed` (node setting; default 2 unless the call sets `interruption_threshold`):

| resumption_speed | base_ms |
|---|---|
| 1 (fast) | 300 |
| 2 (medium) | 500 |
| 3 (slow) | 800 |

Adaptive per-caller adjustment (disabled when `interruption_threshold_ms` set — then `timeout = interruption_threshold_ms`):

1. Maintain `pause_samples`: durations of intra-utterance pauses (silence 120–1 500 ms followed by resumed speech in
   the same user turn). Keep the last 20.
2. `p90 = percentile(pause_samples, 90)` when ≥ 4 samples, else `base_ms`.
3. `adaptive = clamp(max(base_ms, 1.25 * p90 + 80), 250, 1500)`.
4. Completeness multiplier from the latest partial transcript (normalized lowercase, punctuation-stripped tail):
   - Ends with `.`, `?`, `!` in the raw partial, or the whole utterance is in the short-answer set
     {`yes`,`no`,`yeah`,`yep`,`nope`,`sure`,`okay`,`ok`,`correct`,`right`,`thanks`,`thank you`,`bye`,`goodbye`,`hello`,`hi`}
     → ×0.7.
   - Last word in the continuation set {`and`,`but`,`so`,`or`,`because`,`um`,`uh`,`er`,`like`,`the`,`a`,`an`,`to`,`of`,
     `with`,`my`,`is`,`was`,`if`,`then`,`that`,`which`} → ×1.6.
   - Partial ends with a digit and the node is collecting a number (node variable type `integer` or name contains
     `phone`,`zip`,`number`,`code`) → ×1.8.
   - Otherwise ×1.0.
5. `timeout = clamp(adaptive × multiplier, 250, 2000)`.
6. When silence reaches `timeout` since `t_last_voiced_end` and STT has produced ≥ 1 non-empty partial since
   `SpeechStarted`, emit `EndOfTurn`. If no partial text exists (noise burst), discard the speech segment silently.
7. `ivr_mode` floor: `timeout = max(timeout, 900)`.

On `EndOfTurn`, the turn manager sends `commit: true` to Scribe (`08` §4) and starts the turn (§8).
`eot_ms = t_eot_decision − t_last_voiced_end`.

> Decision (2026-11-01): P04-T04. `va_voice.endpointing.Endpointer` is pure and synchronous: every input carries an
> explicit ms timestamp on one caller-chosen timeline (`on_vad(VadEvent)`, `on_partial(text, at_ms)`); the caller
> re-reads `deadline_ms()` after each input and calls `poll(now_ms)` at that instant, which returns
> `EndOfTurn(at_ms, reason, last_voiced_end_ms, timeout_ms, text)` or `TurnDiscarded`. Rules fixed here:
> (a) a turn opens at its first `speech_start`; partials outside an open turn (trailing updates of an ended turn) and
> blank partials are ignored; (b) multiplier order is number → complete → continuation → 1.0, so `"90210."` while
> collecting a number is ×1.8; the number-name rule is a plain substring match (`postcode` contains `code`);
> (c) a set `interruption_threshold_ms` (50–5000, specs/05) is the timeout as is: no multiplier and no 250–2 000 clamp,
> but the `ivr_mode` 900 ms floor still applies; (d) percentile = linear interpolation between closest ranks (numpy's
> default); (e) when the timeout is reached without text, the endpointer keeps waiting for a late partial until the
> silence reaches `max(timeout, 2000)` ms and then discards the segment; a partial arriving in that window ends the
> turn immediately with `reason = "late_transcript"` (otherwise `"silence"`); (f) pause samples (120–1 500 ms gaps
> between a `speech_end` and the next `speech_start` inside one open turn) persist across nodes (`configure`) and are
> cleared only by `reset()` (new leg); `ivr_mode` keeps adaptation and only adds the floor. Token normalization is
> `va_voice.text_norm` (shared with the §7 backchannel matcher).

## 7. Interruptibility and barge-in

Effective level: `0` if `block_interruptions`; else node `interruptibility` (0–3) if set; else call `interruptibility` (0–4).

While the agent is speaking (`PlaybackTracker.is_playing`), user speech is evaluated continuously:

| Level | Interrupt when |
|---|---|
| 0 block | never; user audio is still transcribed and appended to the next user turn after playback ends |
| 1 difficult | speech ≥ 1 200 ms continuous AND partial has ≥ 4 words that are not all backchannel |
| 2 balanced (default) | (speech ≥ 500 ms AND ≥ 2 non-backchannel words) OR speech ≥ 900 ms |
| 3 easy | (speech ≥ 250 ms AND ≥ 1 non-backchannel word) OR speech ≥ 600 ms |
| 4 always | any speech ≥ 150 ms, including backchannels |

Backchannel set (matched on normalized partial tokens; multi-word entries match as phrases): `uh-huh`, `uh huh`,
`mm-hmm`, `mhm`, `mm`, `hmm`, `yeah`, `yep`, `yes`, `ok`, `okay`, `right`, `sure`, `got it`, `i see`, `alright`,
`all right`, `oh`, `ah`, `cool`, `great`, `nice`, `uh`, `um`, `wow`, `true`, `exactly`, `perfect`, `gotcha`.

> Decision (2026-11-01): P04-T05. `va_voice.interrupt` is pure and synchronous.
> `InterruptPolicy.should_interrupt(speech_ms, words, text, level, agent_speaking)` returns an `InterruptDecision`
> (truthy when playback must stop; `reason` one of `not_speaking`, `blocked`, `speech_and_words`, `long_speech`,
> `any_speech`, `insufficient`). `speech_ms` is the duration of the current continuous VAD speech segment (now minus
> the last `speech_start`), measured by the caller. `words` (the STT word list) wins over `text` when given; both are
> split with `va_voice.text_norm.tokens`, so `uh-huh` ≡ `uh huh`. Non-backchannel words are the tokens not covered by
> a greedy left-to-right longest-phrase match against the backchannel set (`BackchannelClassifier`). Level 1 reads
> "≥ 4 tokens in total and at least one of them not a backchannel". All thresholds are inclusive (≥). Nothing
> interrupts while the agent is not speaking. `effective_level(call_level, node_level, block_interruptions,
> interruptible)`: `block_interruptions` → 0, then a non-interruptible utterance (`Say(interruptible=False)`) → 0,
> then the node level (0–3) when set, else the call level (0–4); `interruptible=True` never raises the level; out-of-
> range levels are `ValueError`. DTMF (§13): `should_interrupt_dtmf(level, agent_speaking)` interrupts iff the agent is
> speaking and the level is ≥ 1.

Barge-in sequence (target ≤ 120 ms from decision to `clear`):

1. Decision at time `t_i`. Emit nothing yet.
2. Send `{"event":"clear"}` to Telnyx; drop all frames queued in `outbound_clock`.
3. `TtsSession.cancel(context_id)` → `close_context`; late audio for that context is discarded by `contextId` (`09` §8).
4. Cancel the LLM stream task (`asyncio.Task.cancel()`; the gateway closes the HTTP stream).
5. Cancel any pending tool *speech* (tool HTTP requests continue to completion; their results are attached to the
   next turn context).
6. `played_ms = PlaybackTracker.played_ms(t_i)`; `text_spoken = AlignmentTimeline.text_until(played_ms)` (whole words
   whose end ≤ played_ms). Persist the assistant turn with `text` = full planned text so far, `text_spoken`,
   `interrupted=true`; the conversation history keeps only `text_spoken` followed by `"—"`.
7. Emit `call.interrupt` with `played_chars`, `total_chars`, `reaction_ms = t_clear_sent − t_speech_start`.
8. Continue collecting the user utterance; endpointing proceeds normally.

False-barge-in protection: if the user's speech after an interrupt produces no committed text within 1 500 ms of
EOT (noise), the agent resumes by re-speaking the unspoken remainder of the interrupted sentence prefixed with
"Sorry — " (only once per turn); counts `false_barge_in_total`.

## 8. Turn manager (`va_media.turns.TurnManager`)

Per user turn:

1. `EndOfTurn` → commit STT; await `committed_transcript` (timeout 1 500 ms; on timeout use latest partial and log
   `stt_commit_timeout`). Record `stt_final_ms`.
2. Build `UserTurn(text, start_ms, end_ms, words, language_code)`, persist (write-behind), emit `call.turn`.
3. Call `engine.on_user_turn(turn)` → returns an async iterator of `EngineOutput` items: `SpeakText(token_stream)`,
   `SpeakStatic(text)`, `PlayCached(key)`, `SendDtmf(digits)`, `Transfer(...)`, `EndCall(final_text|None)`,
   `SendSms(...)`, `Wait()`, `SwitchPathway(...)`, `Hold(audio)`. Router/extraction/global checks happen inside the
   engine (parallel speculative design in `11` §7).
4. For speech outputs: token stream → `ClauseChunker` (`09` §5) → `PronunciationApplier` → `TtsSession.send(ctx, text)`;
   final `flush`. Record `llm_ttft_ms` (engine-provided) and `tts_ttfa_ms`.
5. TTS audio → `outbound_clock` queue with segment boundaries; marks sent after each segment.
6. Assistant turn persisted when playback completes (mark echo) or is interrupted.
7. Emit `call.latency` events: one summary (`"E2E: {e2e}ms"`) and stage events `"STT: {stt_final_ms}ms"`,
   `"LLM: {llm_ttft_ms}ms"`, `"TTS: {tts_ttfa_ms}ms"`.

Concurrency rule: at most one active agent utterance; a new user turn arriving while the engine is still generating
(but before audio started) cancels the generation (same as barge-in steps 3–4 without `clear`) and the new user text
is concatenated to the previous user turn text (`"{prev} {new}"`) before re-running the engine.

> Decision (2026-11-01) P04-T12b: barge-in, the specs/07 §7 policy and this concurrency rule live in
> `va_voice.pipeline.CallPipeline` with the pure `va_voice.pipeline.barge_in.BargeInMonitor`. The policy is evaluated
> after every inbound frame batch and every STT partial while `Speaker.is_playing` (first frame sent, not stopped,
> not completed) and a VAD speech segment is open: `speech_ms = now − speech_start` of that segment, text = latest
> partial of the open user turn, level = `effective_level(call interruptibility, node timing.interruptibility
> (cached after every reply), block_interruptions, the utterance's interruptible)`; at most one decision per
> segment (only an honored decision ends the segment's evaluation: an attempt closed because playback stopped
> counts as unhonored, and the same segment is evaluated again against the next utterance). Sequence: (a) `Speaker.interrupt(t_i)` stops the tracker at `t_i` and drops the local queue before its
> first await (a second trigger in the same tick finds nothing playing), sends `transport.clear` (`t_clear`) and
> cancels the TTS context unless `is_final` arrived; (b) the reply task is cancelled, which closes the LLM token
> stream (`Speaker` closes `SpeakText.tokens` explicitly) and the Brain output stream, and records the interrupted
> assistant turn with the frozen `played_ms`; (c) `Brain.on_interrupted(text_spoken + "—")`; (d)
> `PipelineObserver.on_interrupt(InterruptRecord)` (§7 step 7 fields, `reaction_ms = t_clear − speech_start`, which
> includes the level's speech threshold; `clear_delay_ms = t_clear − t_i` is the ≤ 120 ms target),
> `va_barge_in_total{honored="true"}` + reaction histogram. A segment that overlapped playback and ended (or outlived
> the utterance) without interrupting counts once as `honored="false"`. False barge-in: after an honored barge-in of
> a non-resume utterance with an unspoken remainder, a user turn whose committed transcript is blank or missing
> after `stt_commit_timeout_ms` (no partial fallback), or a discarded turn, is not given to the Brain; the pipeline
> speaks `"Sorry — " + unspoken` itself as an interruptible static line that is never resumed again;
> `PipelineStats.false_barge_ins` (no specs/27 metric yet). Any delivered user turn clears the pending resume. Turn
> routing at end of turn: a turn that overlapped interruptible playback (level ≥ 1) without a barge-in and is only
> backchannels is dropped (`ignored_backchannels`); while a reply runs and none of its audio went out, its
> generation is cancelled (TTS context, then the reply task; no `clear`) and the new turn is delivered as
> `merge_turns(answered turn, new)` with `UserTurn.supersedes` = the answered turn's index (the opener has no turn,
> so nothing is prepended); while a reply runs whose audio started, the turn is held (level 0, non-interruptible
> lines, or speech too short to interrupt) and merged with further held turns; the held turn is delivered when the
> reply ends, or merged in front of the caller's turn still open at that moment. `PipelineObserver.on_user_turn`
> fires when a turn is handed to the Brain (and for turns arriving while the call is closing); user and agent turns
> keep sharing one index counter, and false barge-ins consume no index. Telephony actions (§7 step 5, "continue to
> completion") are never cancelled by a reply cancel: `ActionHandler.handle` runs in its own task awaited through
> `asyncio.shield`, so a barge-in, the concurrency rule or the watchdog cancels the reply right away (the inbound
> path never waits for an action) and only the outputs after the action are lost; the reply loop waits for that
> action before it starts the next reply, and its failure ends the pipeline with `error`. The end of the pipeline
> cancels an action still in flight (`pipeline_action_abandoned`). A turn cannot cancel `EndCall`: it sets closing
> first, and turns arriving while closing are never answered.

> Decision (2026-11-02) P05-T11a: in-call persistence. Each attached call's pipeline observer is
> `va_media.turns.TurnRecorder` (built by `CallPersistence.recorder` when the runtime has persistence). Rows:
> `on_user_turn` → a `user` row (text as handed to the Brain, keypad turns as `"[DTMF: …]"`, `language_code`);
> a turn with `supersedes` first deletes the superseded `user` row (its text is the front of the new turn) and
> only the new turn gets a `call.turn`; `on_agent_turn` → an `assistant` row (`text` = planned original,
> `text_spoken`, `interrupted`); `on_latency` → the measured `TurnLatency` values merged into the `user` row of
> `TurnReport.turn_index` (`latency = latency || new`, a no-op when that row is gone). `start_ms`/`end_ms` =
> pipeline ms − the pipeline clock when the recorder was built + (wall now − carrier `answered_at`) at that
> moment (0 when the carrier state has no `answered_at`), rounded, at least 0. Rows are queued synchronously on
> the per-process `va_media.turn_writer.TurnWriter` (write-behind): flushed every 2 s on the injected clock, on
> call end, and at shutdown, in queue order, one tenant transaction per org, ops idempotent
> (`va_db.repositories.CallTurnRepo.upsert_many` = `ON CONFLICT (call_id, turn_index, role) DO UPDATE`, latency
> merged); a failed org's ops are re-queued in order, at most 10 000 ops kept (oldest dropped,
> `media.turns_dropped`). Events: `call.turn` (message = text cut to 200 characters, data `{turn_index, role,
> text, node_id: null, interrupted}`), `call.interrupt` (data `{turn_index, played_chars, total_chars,
> played_ms, reaction_ms}`, message `"Interrupted after {played} of {total} characters"`), and every
> `TurnReport.events` item as `call.latency`, published in callback order by one chain of background tasks
> through the per-process `EventPublisher` whose `call_events` sink is the per-process `CallEventBuffer`; a
> publish failure is logged `media.event_publish_failed` (types only, never text) and never ends the call. When
> the pipeline ends, the runtime awaits the call's pending events, then flushes the call's turns
> (`TurnWriter.flush_call`) and the event buffer (failures logged; the periodic flush retries). The buffer and
> writer loops are started by `va_media.runtime.open_persistence` inside `open_runtime` and closed (pending rows
> written) after the sessions drained.

## 9. Outbound clock and playback tracking

`OutboundClock` runs every 20 ms (drift-corrected using `loop.time()` targets, not sleep accumulation):

1. Pop the next TTS frame (20 ms) if available; else idle frame.
2. Mix with background (§10.2) when enabled; idle frames carry background only (or are skipped when
   `background_track="none"` — Telnyx `send_silence_when_idle` covers the gap).
3. Encode to the leg codec and send `{"event":"media","media":{"payload": b64}}`.
4. Lead control: the clock sends at most 3 frames (60 ms) ahead of wall-clock playback position; when Telnyx has
   just been cleared, lead resets to 0.
5. After the last frame of a segment, send `{"event":"mark","mark":{"name":"seg:{turn}:{idx}"}}`.

`PlaybackTracker` per utterance: `t_first_frame_sent`, `frames_sent`, `segments[{idx, start_ms, end_ms, chars}]`.
`played_ms(t) = clamp((t − t_first_frame_sent) − LEAD_MS_ESTIMATE, 0, frames_sent × 20)` where
`LEAD_MS_ESTIMATE = 60`. Mark echoes correct the estimate: on mark `seg:k:i`, `played_ms ≥ segments[i].end_ms`.
`is_playing` true from first frame until the last mark echo (or `played_ms` reaches total + 200 ms if marks are lost).

> Decision (2026-11-01): P04-T08. `va_voice.playback.PlaybackTracker(turn, fmt, lead_ms=60)` is pure and synchronous;
> callers pass ms timestamps from their monotonic clock. The outbound clock calls `frame_sent(now_ms)` per 20 ms frame
> and `end_segment(chars)` after a segment's last frame; it returns the mark name `seg:{turn}:{idx}` to send. A segment
> needs at least one frame; `finish()` declares that no more audio follows and requires every frame to belong to a
> segment. `played_ms(t) = max(confirmed, clamp((t − t_first_frame_sent) − 60, 0, frames_sent × 20))` where
> `confirmed` is the end of the latest echoed segment; marks of another turn, unknown indices and malformed names are
> ignored. `is_playing` is false before the first frame and after `stop`; it stays true until `finish()` (an
> underrun is still "speaking"), then ends on the echo of the last segment's mark or once the unclamped estimate
> reaches total + 200 ms. `stop(at_ms)` (barge-in) freezes `played_ms` at `at_ms` and returns it; later frames and
> marks are ignored. The tracker owns the utterance's `AlignmentTimeline` (`played_text(at)`).
> `truncate_assistant_message(timeline, played_ms)` returns `text` (full planned original, stripped), `text_spoken`,
> `history_text = text_spoken + "—"` (just `"—"` when nothing was heard), `played_chars = len(text_spoken)`,
> `total_chars = len(text)` and `unspoken` (the remainder, for the false-barge-in resume).

Rate-limit backoff: on Telnyx error `100005`, the clock pauses sends for 100 ms and reduces lead to 1 frame for 5 s.

> Decision (2026-11-01): P04-T09. `va_voice.outbound` splits the clock into a pure ms schedule and the loop.
> `FramePacer` anchors at the first frame sent while Telnyx's buffer is empty (`t0`) and schedules frame `n` since
> the anchor at `t0 + max(0, n − lead) × 20` (lead 3, so a burst of up to 4 frames goes out at once and the queue at
> Telnyx never exceeds 60 ms beyond the frame playing); targets come from the anchor and the injected `Clock`'s
> monotonic time, never from summed sleeps. Once wall-clock time reaches `t0 + sent × 20` Telnyx has drained and the
> next frame re-anchors (after an underrun or a stalled loop the burst restarts instead of catching up). `reset()`
> after a clear re-anchors at once ("lead resets to 0"). A frame is late when it goes out more than 30 ms past its
> target (§21); the wait after a stretch with nothing to send is not lateness. `100005`: sends pause until
> `now + 100 ms` and the lead is 1 until `now + 5 s`. `OutboundAudio(sink, FrameMixer, clock=)` re-frames TTS audio
> per utterance (`start_utterance(PlaybackTracker)`, `push_audio`, `end_segment(chars)`, `finish_utterance()`;
> a partial frame stays in the framer across a segment boundary, so a mark can trail its clause by < 20 ms, and only
> `finish_utterance` pads), sends a segment's mark in the same tick as its last frame, carries the characters of a
> segment without whole frames into the next one, and calls `tracker.finish()` after the last mark. `clear()` is
> local: it drops queued frames/marks of every utterance and resets the pacer; the caller sends `transport.clear` and
> stops the trackers (marks of stopped trackers are never sent; a frame in flight during a clear does not count).
> Without background the loop parks on an event while nothing is queued (no sleeps, no idle frames).

> Decision (2026-11-02): outbound underruns (P05-T17e). A **platform underrun** is a speech frame sent after
> Telnyx's buffer drained (`FramePacer.drained(now)` before the send, i.e. the loop is more than the 60 ms lead
> behind the schedule) while the previous frame sent on the leg was a speech frame of the same utterance, with no
> idle/background frame, gap wait, `clear()`, or utterance finish in between: the caller hears a hole that the
> platform caused. TTS starvation (the vendor delivering audio late) is not an underrun: the loop then sends idle
> frames or parks in the gap branch, and either breaks the continuity. A `100005` pause longer than the lead does
> count (the hole is audible). `OutboundStats.underruns` counts them per leg; `OutboundAudio(on_underrun=)` /
> `Speaker(on_underrun=)` / `CallPipeline(on_underrun=)` report each one, and the media runtime increments
> `va_audio_underruns_total` from that hook. The `pipeline_ended` log carries `outbound_frames`, `late_frames`,
> `max_lateness_ms`, and `underruns`.

## 10. Audio conditioning

### 10.1 `noise_cancellation`

Decision: noise handling is split by consumer, with no added latency on the STT path.
- STT path: when `noise_cancellation=true`, Scribe is opened with `filter_background_audio=true` (`08` §3); raw audio
  is sent unmodified.
- VAD path: `SpectralGate` on 16 kHz PCM — STFT 512-point Hann, hop 256; noise profile = per-bin 20th percentile of
  magnitude over the trailing 1.5 s of non-speech windows (updated only during VAD non-speech); gain per bin
  `g = max(0.25, 1 − (1.5 × noise / max(mag, ε)))` (floor −12 dB), smoothed over time with α = 0.6; inverse STFT with
  overlap-add. Added VAD latency 16 ms (accounted in `eot_ms`). When `noise_cancellation=false` the gate is bypassed.

> Decision (2026-11-01): P04-T09. With the raw 20th percentile as the noise estimate the gate only reaches ≈ −8 dB
> on stationary Gaussian noise (Rayleigh magnitudes: the 20th percentile is ≈ 0.53 × the mean), short of the §22
> target of ≥ 10 dB. `va_voice.noise.SpectralGate` therefore scales the per-bin 20th percentile by the Rayleigh
> mean/20th-percentile ratio `sqrt(π/2) / sqrt(−2 ln 0.8)` ≈ 1.876 (measured ≈ −11.6 dB at −30 dBFS). The window is
> the periodic Hann (it sums to 1 at 50 % overlap, so unit gains reconstruct the input exactly); the profile ring
> holds the last 94 non-speech frames (1.5 s) and gains stay 1 until 16 of them (0.25 s) were seen; smoothing is
> `g = 0.6 × g_prev + 0.4 × g_raw`. `process()` accepts any chunk size and returns the samples that are final, in
> whole hops: the output stream is the input delayed by exactly 256 samples, so VAD times on the gated stream are
> 16 ms late in stream time (subtract `LATENCY_MS`). VAD decisions are fed back with `set_speech(active)`.
>
> Decision (2026-11-02): profile refresh cadence (P05-T17e). Recomputing the per-bin percentile over the 94 × 257
> ring on every hop cost ≈ 46 % of the media worker's event loop at 50 concurrent calls (py-spy). The profile is now
> computed on the first hop at which it becomes ready and then recomputed every 32 hops (512 ms) while the ring has
> new non-speech frames (`PROFILE_REFRESH_HOPS`); speech-only stretches never recompute it. The percentile is a
> partial sort (`np.partition` of the two neighbouring ranks) with `np.percentile`'s linear interpolation, so the
> values are unchanged. A trailing 1.5 s stationary-noise estimate barely moves within 512 ms, and the ≥ 10 dB
> reduction still holds. Measured on stationary noise: ≈ 2 ms per call-second (was ≈ 24 ms).
>
> Decision (2026-11-03) P05-T17e2: once per VAD window. After `T` raw samples the gate has released exactly
> `floor(T / 256) × 256` samples, so with a 512-sample VAD window a new window completes exactly when `floor(T /
> 512)` increases. `GatedVad(vad, window_samples=512)` (what `InboundPath` builds) keeps the raw samples until then
> and gates + feeds them in one call: gate output, VAD events and their times are identical to gating every 20 ms
> frame (the speech feedback only changes after a VAD feed), but the gate and the VAD run ≈ 31 instead of 50 times
> per call-second and the trailing partial window never reaches the VAD. `window_samples` defaults to one hop (the
> previous per-feed behaviour) and must be a multiple of 256. `SpectralGate.process` transforms all hops that
> become final in one call as the rows of one forward and one inverse FFT and overlap-adds them vectorised; only
> the per-bin gain update stays per hop (bit-identical output).

### 10.2 Background tracks

- Assets generated procedurally by `scripts/generate_audio_assets.py` (seeded RNG, no third-party audio) into
  `assets/audio/{office,cafe,restaurant,static}.ulaw` and `.pcm16k` — 60 s loops:
  - `static`: pink noise band-limited 300–3 400 Hz.
  - `office`: brown noise bed + randomized keyboard-click transients (5–20 ms bursts, 2–6 /s) + distant murmur
    (noise modulated by 3–6 Hz envelopes, band 200–2 000 Hz).
  - `cafe`: murmur bed + clinks (short decaying 2–5 kHz sinusoids, 0.2–1 /s).
  - `restaurant`: denser murmur (×1.5 level) + clinks + low rumble.
  Loops are cross-faded over 500 ms at the seam.
- Levels: `null` (default) → `static` at −40 dBFS; `office`/`cafe`/`restaurant` → −22 dBFS RMS; `"none"` → no track.

> Decision (2026-10-25): assets are stored at their playback level so the mixer applies unit gain: `static` −40 dBFS
> RMS, `office`/`cafe`/`restaurant` −22 dBFS RMS, `hold_music` −18 dBFS RMS, peaks soft-limited (tanh) below −1 dBFS.
> Each track is synthesized once at 16 kHz; `.pcm16k` is raw little-endian int16 mono 16 kHz, and `.ulaw` is raw
> G.711 μ-law 8 kHz (no header) derived by an FFT-domain low-pass at 3 400 Hz plus 2:1 decimation, levelled
> separately so both formats hit the target. Randomness comes only from `numpy.random.PCG64(seed).random_raw()`
> (bit-generator streams are stable across numpy versions, `Generator` methods are not) with our own float
> conversion; each track's seed is `crc32(track) ^ 20261025`. Office clicks are a Poisson process at 4 /s, clinks at
> 0.6 /s. Hold music (`assets/audio/hold_music.{ulaw,pcm16k,wav}`, see `06` §10.2) is a procedural I–vi–IV–V
> progression in C at 64 BPM (two bars per chord, so the 60 s loop holds exactly two progressions and is rendered
> circularly instead of cross-faded), with every partial below 3 400 Hz. `generate_audio_assets.py --check` is the
> drift check: committed files must be byte-identical to a regeneration.
- Mixing: decode TTS frame to int16, add `background_frame × gain`, clip to int16, encode. Agent speech is not ducked.
- Background never goes to the STT (it is outbound only).

> Decision (2026-11-01): P04-T09. `background_track` values resolve with `va_voice.background.resolve_background`:
> `null` and the org-settings value `phone_static` (`04` §7) → asset `static`, `office`/`cafe`/`restaurant` → their
> asset, `"none"` → no track (anything else is an error). `BackgroundLibrary(assets_dir).loop(track, fmt)` reads
> `<asset>.ulaw` for 8 kHz legs and `<asset>.pcm16k` for 16 kHz legs (decoded once per process and shared
> read-only; each call gets its own loop position). `FrameMixer` accepts the TTS formats `ulaw_8000` and `pcm_16000`
> and applies unit gain by default (assets carry their level); μ-law speech without background is passed through
> byte for byte, `pcm_16000` is converted to the `TELNYX_L16_BYTE_ORDER` wire order (§15). The background advances
> one frame per frame sent, speech or idle, so the bed is continuous.

> Decision (2026-11-02): call audio options (P05-T15). (1) `noise_cancellation` (call value, default `true`, org
> `noise_cancellation_default`): `va_media.runtime_config.stt_config` sets Scribe `filter_background_audio`, and
> `pipeline_config` sets `PipelineConfig.noise_cancellation`, which makes `InboundPath` wrap the leg's VAD in
> `va_voice.pipeline.GatedVad` (one `SpectralGate` per leg; VAD `speech_start`/`speech_end` drive `set_speech`;
> event times move back by `LATENCY_MS` = 16 ms, clamped at 0, so they stay on the raw timeline; a feed the gate
> releases no samples for skips the VAD). `PipelineConfig` defaults to `false` (the pipeline alone runs raw VAD);
> the runtime always maps the call's value. (2) `background_track`: one `BackgroundLibrary` per media-worker
> process over `assets/audio` relative to the working directory (`/app` in the image, which copies `assets/`;
> `VA_ASSETS_DIR` stays unread); `va_media.runtime.background_mixer` builds each leg's `FrameMixer` in the TTS
> format with the call's loop. A missing or unreadable asset (or an unsupported rate) is logged
> `media.background_unavailable` (`track`, `detail` = exception type) and the call goes on without a bed.
> (3) Option → component: `noise_cancellation` → Scribe flag + gated VAD; `background_track` → `FrameMixer`
> background; `interruptibility`/`block_interruptions`/`interruption_threshold_ms`/`ignore_button_press` →
> `PipelineConfig`; `keywords` → Scribe keyterms; `language` → Scribe `language_code` (unknown/`auto` → detection),
> TTS `language_code` and the prompt language; `voice` → TTS `provider_voice_id` + `voice_settings`; `temperature`
> → the TaskBrain dialogue request; `precall_dtmf_sequence` → `GreetingGate`. (4) FakeTelnyx's agent-speech level
> rises to frame RMS 1024 (-30 dBFS) so the default `static` bed (-40 dBFS, frame RMS ≤ 436) is not agent speech
> (specs/30 §3.5 Decision P05-T15); the -22 dBFS beds overlap speech levels, so scenario tests that wait for agent
> silence use `static` or `"none"`.

## 11. Greeting logic

1. Outbound: wait for `call.answered` (from carrier state).
2. If AMD enabled: wait for `call.amd` result up to 3 000 ms after answer while VAD/STT run.
   - `machine` → voicemail flow (§14). `human` → continue. Timeout → treat as human (classification continues and can
     still switch to voicemail flow until `voicemail.detect_timeout_ms`).
   - See the §14 Decision (2026-11-02, P05-T10c) for the AMD wait, the verdict sources and precall DTMF.
3. `precall_dtmf_sequence` → send (`06` §9), then wait 500 ms.
4. `wait_for_greeting=true` → stay silent until first user `EndOfTurn` or `greeting_timeout_ms`, then the engine runs
   with that user turn (or an empty one on timeout).
5. Else speak the opener: `first_sentence` (templated) from the phrase cache when available (pre-rendered during
   prefetch, key per `09` §9), else generated by the engine from the start node.
6. Inbound: answer → immediately step 5 (no AMD) unless `wait_for_greeting`.

> Decision (2026-11-02): greeting gate, outbound DTMF, IVR mode (P05-T10b). `CallPipeline(before_opener=...)`
> awaits the hook before anything is spoken; the media runtime passes `va_media.greeting.GreetingGate`.
> Step 1 (outbound only): Telnyx starts the stream after the answer, so the gate waits on
> `va_media.carrier_watch.CarrierWatch.answered` (the `answered` field of the carrier state snapshot, or a
> `call.answered` carrier event of the `primary` leg) for at most 2 s on the injected clock and then goes on
> (`media.answer_unconfirmed`). Step 3 (outbound only): `precall_dtmf_sequence` is split into digit groups and
> pauses (`w` = 0.5 s; `W` = 1.0 s, reachable from `press_button` only), one `send_dtmf` per group with
> `command_id("precall_dtmf", call_id, "primary", i)`, then 500 ms; a Telnyx error, or a BYO account whose key
> cannot be resolved (`media.telnyx_unavailable`), is logged and the call goes on (the digits are never
> logged). Step 4 (every direction): with `wait_for_greeting` the pipeline stays silent until the first user
> turn (speech `EndOfTurn` or a keypad sequence) or `greeting_timeout_ms` (1 000–30 000), then runs
> `Brain.start(greeting)` with that turn (`None` on timeout); the turn is the Brain's first user message (the
> task runner appends it before its `first_sentence` or generates the opener answering it), and the no-input
> timer is never armed before the opener. `press_button` runs in `va_media.actions.MediaActions` with
> `command_id("press_button", call_id, "primary", n, i)` (`n` = the call's press counter); its failures are
> logged and swallowed, other actions are logged `media.action_unsupported` until their tasks. IVR dead end
> (specs/05 §9 `ivr_dead_end`, `ended_by=system`): with `ivr_mode` the pipeline's `ivr` task
> (`va_voice.pipeline.IvrProgress`) ends the call silently (the reply in progress is cut off, no final line)
> when 120 s pass from the opener without progress; progress = a delivered user turn whose
> `text_norm.normalized` text was not delivered before in the call (repeating menus are no progress), or any
> Brain action. The opener pre-render (step 5, phrase cache) is P05-T10d.

> Decision (2026-11-02): opener pre-render (P05-T10d). `prefetch_call_context` renders the templated
> `first_sentence` once (`va_engine.opener.render_opener`: task runner only, the call's variables,
> `request_data` and built-ins at prefetch time, stripped; blank → none; a template syntax error speaks the raw
> text) and stores it in `CallContext.opener_text` (optional, context version stays 1). With
> `wait_for_greeting` nothing is pre-rendered (the opener may answer the greeting). After the context is saved,
> `va_workflows.activities.prerender.OpenerPrerender` renders, within one 8 s budget, the opener (per-call
> entry, TTL 1 h) and the voice's standard fillers (shared, 30 days) through ElevenLabs REST into the phrase
> cache (specs/09 §9). The key uses the voice the TTS socket opens with (`va_engine.opener.phrase_voice`:
> `provider_voice_id`, `ELEVENLABS_TTS_MODEL`, `voice_settings`, the fixed language row's TTS code or none),
> the leg's TTS format (`pcm_16000` with `hd_audio`, else `ulaw_8000`), and the text after the pronunciation
> guide (`cache_text`), i.e. exactly what the live path would send. Orgs with `privacy.zero_retention` or
> `hipaa_mode` get nothing rendered (REST has no `enable_logging=false`); a missing org, unreadable org
> settings, a REST failure or the budget expiring are logged (entry name, never the text) and never fail the
> activity (timeout raised to 20 s); `PrefetchResult.opener_cached` reports the outcome. The task runner
> speaks `opener_text` verbatim (so time built-ins cannot drift from the cached audio); a context without it
> (rebuilt by the media worker) renders `first_sentence` at call time. In the media worker every call gets a
> `va_media.phrases.CallPhrases` (read-only lookups, `va_tts_cache_hits_total`/`misses_total`); the pipeline
> `Speaker` plays a `SpeakStatic` whose pronounced text has an entry in the leg's TTS format from the cache:
> no TTS context is opened, the whole audio goes through the same outbound clock as one segment, the stored
> alignment fills the timeline (barge-in truncation works as for live audio; an entry without alignment
> interpolates), and a barge-in clears the far end without a TTS cancel. A miss, an empty entry, another
> format, or a failing lookup (Redis down, logged `pipeline_phrase_lookup_failed`) speaks it through the live
> socket. `SpeakText` never looks up.

## 12. Silence and no-input policy

After the agent finishes an utterance (last mark echo), start `no_input_timer = no_input_timeout_ms` (default 8 000;
org setting `call_defaults.no_input_timeout_ms`, range 3 000–30 000). On expiry with no speech:
1. Reprompt count < `max_reprompts` (default 2): engine `on_no_input()` → speaks a reprompt (default text
   "Are you still there?" — node may override with `noInputPrompt`). Increment count.
2. Otherwise speak "It seems we got disconnected. Goodbye." (org `settings.no_input_goodbye`) and end with
   `end_reason='no_input_timeout'`.
Any user speech resets the count.

## 13. DTMF input

- `ignore_button_press=true` → drop all DTMF.
- Otherwise digits are buffered: a DTMF sequence ends after 2 000 ms without a new digit or on `#`. The sequence is
  delivered to the engine as a user turn with text `"[DTMF: 1 2 3 4]"` and `dtmf="1234"` field; pathway nodes may
  map DTMF to edges (`11` §5) and variable extraction receives the raw digits.
- Barge-in: a DTMF digit during playback interrupts at levels ≥ 1.

> Decision (2026-11-01): P04-T12c. Both policies live in `va_voice.pipeline.CallPipeline` as two more tasks of its
> `TaskGroup` (`silence`, `dtmf`), backed by the pure `va_voice.pipeline.silence.NoInputTimer` and
> `va_voice.pipeline.dtmf.DtmfBuffer`. **No-input (§12):** the timer is armed (never extended while armed) only
> when the leg is idle: the reply loop waits for work (the agent's last utterance has played to its mark echo or
> grace, or the reply produced no audio), nobody is mid-turn, no turn is held, queued or committing, no digit is
> pending, no action runs and the call is not closing. It is re-armed after a discarded noise turn, an ignored
> backchannel and an empty `#`. Any VAD `speech_start`, any accepted digit and every queued job disarm it. On
> expiry (re-checked against the idle rule) fewer than `max_reprompts` reprompts since the last delivered user
> turn → `Brain.on_no_input()`; when that stream yields nothing the pipeline speaks the node's
> `no_input_prompt` (`BrainTiming`), else `config.no_input_prompt` ("Are you still there?"), interruptibly.
> Otherwise it plays `EndCall(config.no_input_goodbye, "no_input_timeout")` (the org setting default of
> specs/04 §7 supersedes the literal above). "Any user speech resets the count" means speech (or keys) that
> become a user turn delivered to the Brain; noise, ignored backchannels and false barge-ins only postpone the
> timer. `max_reprompts = 0` says goodbye at the first expiry. **DTMF (§13):** symbols `0-9 * # A-D`; anything
> else is dropped (`pipeline_dtmf_invalid`). `ignore_button_press` drops every digit before any other effect
> (`stats.ignored_dtmf`). A digit while an utterance plays first runs the barge-in sequence when the effective
> level is ≥ 1 (`InterruptRecord.reason = "dtmf"`, `speech_start_ms` = the digit's arrival, no false-barge-in
> resume: a digit is real input); at level 0 or on a non-interruptible line it does nothing to playback. The
> digit is then buffered: the sequence ends 2 000 ms after its last digit or at once on `#` (not part of the
> sequence; a `#` with nothing pending is ignored). The sequence becomes `UserTurn(text="[DTMF: 1 2 3 4]",
> dtmf="1234", start_ms=first digit, end_ms=last digit)` with its own turn index and no latency timer, and is
> routed exactly like a speech turn (dropped while closing, held behind audible playback, merged with a reply
> still generating; never a backchannel). `merge_turns` concatenates the `dtmf` parts so no digit is lost.
> Digits pending when the transport closes are dropped. Digit values are never logged (PINs, card numbers).

## 14. Voicemail flow

Triggered by Telnyx premium AMD `machine`, or by `voicemail.sensitive` LLM classification (`10` §9.4) on committed
user text within `detect_timeout_ms`, or by the engine's voicemail detector when AMD is disabled and action ≠ `ignore`
(the classifier still runs when `sensitive=true`).

1. Emit `call.amd`/`call.voicemail` (`detected`), set `answered_by='voicemail'`, cancel current utterance.
2. Action `ignore` → continue conversation.
3. Action `hangup` → hang up, `end_reason='voicemail_hangup'`, disposition `VOICEMAIL`.
4. `leave_message` / `leave_message_and_sms`: wait for `call.machine.premium.greeting.ended` (beep) or 1 200 ms of
   silence after the greeting, max 20 s; speak the templated message (TTS streamed, not cached); wait for mark echo;
   for `_and_sms` send the SMS (`21` §4) with `voicemail.sms.message`; hang up, `end_reason='voicemail_left'`.
5. If `retry` configured → `CallWorkflow` schedules the child retry call.

> Decision (2026-11-02): AMD and voicemail actions (P05-T10c). (1) `va_domain.voicemail` holds `amd_mode` (moved
> from the dial; `premium` unless `ivr_mode`, `answered_by_enabled=false`, or a non-sensitive `ignore`),
> `classifier_enabled` (outbound, `voicemail.sensitive`, not `ivr_mode`, `answered_by_enabled`) and `amd_verdict`
> (premium `human_residence`/`human_business` → human, `machine` → machine, `fax_detected` → fax; standard `human`
> → human, `machine` → machine; anything else → unknown). Whenever a voicemail action matters AMD is `premium`
> unless `ivr_mode`/`answered_by_enabled=false`, which also switch the classifier off, so the "engine detector
> when AMD is disabled" case above is unreachable. (2) `va_media.carrier_watch.CarrierWatch` exposes `amd` +
> `verdict` (the snapshot's `answered_by`, else `amd_result`, else the first primary-leg detection event;
> `on_verdict` is called once), `beep` (snapshot `beep_detected`/`greeting_ended`, or a primary
> `call.machine.premium.greeting.ended`/`call.machine.greeting.ended`); call screening is logged and treated as a
> human. (3) §11 step 2: on outbound calls with `amd_mode == "premium"` the greeting gate waits up to 3 s after the
> answered wait for the verdict (timeout → `media.amd_pending`, treated as human); precall DTMF is skipped on a
> fax and on a machine unless the action is `ignore`. (4) Outbound calls get a `va_media.voicemail.VoicemailMonitor`
> (inbound/web none): a `machine` verdict within `voicemail.detect_timeout_ms` of the answer (trigger `amd`), or a
> `classify_answer` result (specs/10 §9.4, call type `voicemail`, prompt `answer_classifier.txt`) of label
> `voicemail` with confidence ≥ 0.8 on the delivered user turns so far (trigger `classifier`; one request in flight,
> turns arriving meanwhile re-run it once with the whole transcript; `human`/`ivr` ≥ 0.8 stop the classification;
> LLM failures wait for the next turn) is a detection; only the first acts; later verdicts/results are logged
> `media.voicemail_late`; `ivr_mode` calls never act; `fax` ends the call silently with `fax_detected` (the edge
> already stored `answered_by='fax'` and emitted `call.amd`). (5) A detection first hands the call to the pipeline
> through `CallPipeline.take_over(Takeover(reason, text, beep))` (first end wins; a takeover accepted before the
> opener skips it): `hangup` → `voicemail_hangup`; `leave_message`/`leave_message_and_sms` → the templated
> `voicemail.message` (call variables, `request_data`, built-ins; blank → `voicemail_hangup`), spoken once, not
> interruptible, after the beep, or 1 200 ms without caller speech (VAD; counted from the takeover when nobody spoke),
> at most 20 s, through the live TTS socket, then `voicemail_left` after its mark echo (playback capped at 120 s);
> `ignore` → the conversation goes on. The takeover stops accepting turns (recorded, never answered) and cuts the
> reply in progress. Then, in the background, it emits `call.voicemail` (`data = {action, trigger}` + `confidence`
> for the classifier; message `Voicemail detected`) and, for the classifier, sets `answered_by='voicemail'` on the
> answered call (the edge sets it for AMD); failures are logged and never undo the action. (6) Both voicemail ends
> set `disposition_tag='VOICEMAIL'` when the call has none (`va_db.call_end.record_answered_end`). (7) The
> `_and_sms` SMS follows the P05-T10e Decision below; the retry child call follows the P05-T10f Decision below.
> Transcripts and the message text are never logged.

> Decision (2026-11-02): Voicemail SMS (P05-T10e). (1) `Takeover.after_message` (an async hook, capped by
> `after_message_timeout_s`, default 15 s, then cancelled and logged `pipeline_takeover_after_message_timeout`) runs
> once the message's mark echo arrived or its 120 s playback cap passed, then the call ends `voicemail_left`; it is
> never run for a takeover without text, nor when the call closed (caller hangup, watchdog) before the message
> finished. For `leave_message_and_sms` the `VoicemailMonitor` passes `va_media.voicemail_sms.VoicemailSms.send` as
> the hook (no database → logged `media.voicemail_sms_unavailable`, the message is still left). (2) Sender =
> `voicemail.sms.from`, else the call's `from`; it must be an `active`, not deleted number of the org with the
> `sms` capability (`va_db.voicemail_sms.sms_sender`). Send Call checks the same rule after resolution → 400
> `SMS_NOT_ENABLED` (`field = voicemail.sms.from`). The 10DLC / toll-free gating of specs/21 §2 step 1 arrives with
> the SMS phase. (3) Messaging profile: the BYO account's `messaging_profile_id` for BYO numbers, else
> `TELNYX_MESSAGING_PROFILE_ID` (now also a media-worker variable, with `EDGE_PUBLIC_HOST` for the
> `webhook_url = webhook_urls(EDGE_PUBLIC_HOST, account).messaging`, `use_profile_webhooks=false`); none → the
> message is recorded `sending_failed` (`error.code = no_messaging_profile`). (4) Instead of the Temporal
> `send_message` activity of specs/21 §2 step 6 the media worker sends directly, once: the `messages.id` is
> `uuid5(VOICEMAIL_SMS_NAMESPACE, call_id)`, inserted `queued` (`ON CONFLICT (id) DO NOTHING`) and committed before
> the Telnyx request, so a second attempt for the call finds the row and sends nothing; a failed send is not
> retried (`send_message` is not idempotent) and is stored `sending_failed` with `error = {code, detail}` (first
> Telnyx error) or `{code: <failure type>}`. Success stores `telnyx_message_id`, the recipient status when it is a
> `messages.status` value (else `queued`) and `segments = parts` (default 1). (5) The message joins the number pair's
> active conversation (specs/21 §1: `status IN ('active','human_takeover')`, `last_message_at + time_out_minutes >
> now`, most recent) or a new send-only conversation (`channel='sms'`, `runner_type='objective'`, `objective` NULL,
> `contact_id` of the call, `metadata = {source: voicemail, call_id}`); `last_message_at` moves to now. Replies are
> handled once the SMS phase lands. (6) After a successful send `call.sms_sent` is published (message `Voicemail SMS
> sent`, `data = {message_id, conversation_id, segments, source: "voicemail"}`; no numbers, no text). Billing on
> `message.sent` (specs/21 §12) stays with the SMS phase; no usage is recorded here. (7) Every failure is logged
> (`media.voicemail_sms_failed` with a `reason`: `sms_not_enabled`, `no_messaging_profile`, `telnyx_unavailable`,
> `telnyx_error`, `database`; `media.voicemail_sms_skipped` for `no_sms`/`blank_message`;
> `media.voicemail_sms_duplicate`) and never changes the call's end.

> Decision (2026-11-02): Voicemail retry (P05-T10f). (1) Trigger: the ended call's recorded `end_reason` —
> `voicemail_hangup`/`voicemail_left` always, `no_answer`/`busy` only with `retry.on_no_answer`
> (`va_domain.call_retry.retry_wanted`). `CallWorkflow` asks the `schedule_retry_call` activity after its lifecycle
> and the post-call start, for every call it found (phase `retry`, between `post_call` and `ended`); the activity
> reads the row, so voicemail ends recorded by the media worker and carrier no-answer ends recorded by the edge are
> handled alike. An activity failure is logged (`calls.retry_failed`) and never fails the workflow. (2) Attempts:
> `max_attempts` is the number of retry calls in the chain. The original call is attempt 0; each child stores its
> attempt in `calls.config.retry_attempt` (a non-configuration key next to `pricing`); a child is created only while
> the ending call's attempt is below `max_attempts`, and inherits `retry` unchanged so the chain continues.
> (3) The child is an ordinary **scheduled** call: `start_time = now + retry.wait`, `status`/`queue_status` =
> `scheduled`/`new`, `parent_call_id` = the parent, the parent's dispatch columns (numbers, runner, voice, number,
> memory, `external_id`, `test_call`, `request_data`, `metadata`), no credit hold, `workflow_id = call-<child>`,
> and `config` = the parent's stored config with `voicemail.action = retry.voicemail_action`, `voicemail.message =
> retry.voicemail_message` when set (else the parent's message), `start_time`, and `retry_attempt`. Because it is a
> scheduled call, the `dial` activity admits it at its start time (compliance, limits, credit hold; specs/24 §6
> Decision P05-T04b) and a refusal is recorded on the child as `failed`/`dispatch_blocked` with
> `call.dispatch_blocked`; the parent is never changed. (4) The child's `call.scheduled` event ("Call successfully
> scheduled.") is written in the same transaction, data `{status, scheduled_for, warnings: [], parent_call_id,
> retry_attempt}`. (5) The parent's `CallWorkflow` starts the child's `CallWorkflow` as a child workflow (same task
> queue, `ParentClosePolicy.ABANDON`, input `scheduled_for`), which sleeps until the start time;
> `WorkflowAlreadyStarted` is logged and ignored. Its result carries `retry_call_id`. (6) Idempotency: the activity
> locks the parent row (`FOR UPDATE`) and looks for a child with `parent_call_id` = parent and a `retry_attempt` key
> (warm transfers may use `parent_call_id` too); a found child is returned as `already_scheduled` with its id and
> start time, so an activity retry after a lost commit, or a replay, never creates a second child.

> Decision (2026-11-02): the takeover seam also serves the inbound maintenance message: a call whose context carries
> `maintenance_message` is taken over before the pipeline runs and ends `inbound_rejected` (specs/16 §4 Decision
> P05-T12d).

## 15. `hd_audio` mode

- Dial/answer with `stream_codec="L16"`, `stream_bidirectional_codec="L16"`, `stream_bidirectional_sampling_rate=16000`.
- Wire byte order is controlled by `TELNYX_L16_BYTE_ORDER` (default `big`, per RFC 3551 L16). Inbound: convert to
  native int16; outbound: convert ElevenLabs `pcm_16000` (little-endian) to the wire order.
- Scribe `audio_format=pcm_16000` receives little-endian PCM (converted); TTS `output_format=pcm_16000`.
- VAD uses the 16 kHz stream directly (no resample).
- Live smoke test `test_hd_audio_byte_order` plays a 1 kHz tone and asserts STT/energy sanity in both directions.

> Decision (2026-11-02): the transport maps an L16/16000 stream to `L16_16000` and passes the wire bytes through
> untouched; byte-order conversion stays in the pipeline (`PipelineConfig.l16_byte_order`, `FrameMixer(wire_byte_order=)`)
> (specs/06 §8 Decision P05-T08).

## 16. Engine interface (contract with `va_engine`)

```python
class ConversationEngine(Protocol):
    async def start(self, ctx: CallContext) -> AsyncIterator[EngineOutput]: ...          # opener
    async def on_user_turn(self, turn: UserTurn) -> AsyncIterator[EngineOutput]: ...
    async def on_no_input(self) -> AsyncIterator[EngineOutput]: ...
    async def on_dtmf(self, digits: str) -> AsyncIterator[EngineOutput]: ...
    async def on_interrupted(self, spoken_text: str) -> None: ...
    async def on_control(self, msg: ControlMessage) -> AsyncIterator[EngineOutput]: ...
    async def on_carrier_event(self, ev: CarrierEvent) -> AsyncIterator[EngineOutput]: ...
    def node_timing(self) -> NodeTiming: ...   # resumption_speed, interruptibility, no_input_prompt
    def snapshot(self) -> EngineState: ...
```

> Decision (2026-11-01): P04-T12a. `va_voice.pipeline.CallPipeline` is the transport-agnostic turn loop of one leg;
> va_media's `CallSession` (phase 05) owns the sockets (Telnyx WS, STT/TTS open/close), the Redis control channel,
> ownership/heartbeat and persistence, and adapts `ConversationEngine` to the narrower `Brain` protocol (`start`,
> `on_user_turn`, `on_no_input`, `on_interrupted`, `timing`). DTMF reaches the Brain as a `UserTurn` with `dtmf` set
> (§13), so the Brain has no `on_dtmf`. The pipeline never imports va_elevenlabs/va_telnyx: `Transport`,
> `SttStream`, `TtsStream`, `VoiceActivityDetector` are structural protocols with read-only members that the
> vendor clients and `SileroVad` satisfy unchanged. Brain outputs are `SpeakText(tokens, interruptible,
> llm_request_ms)`, `SpeakStatic(text, interruptible)`, `EndCall(final_text, reason)` and opaque `BrainAction`
> objects handed to an injected `ActionHandler` (transfer, SMS, DTMF, hold, ...; without a handler they are logged
> `pipeline_action_unhandled`). `run()` returns `PipelineResult(end_reason, turns, stats, error)` and never hangs
> up, persists or publishes; user/agent turn records and latency reports go to a synchronous `PipelineObserver`
> that phase 05 maps to write-behind persistence, `call.turn` and `call.latency`. One timeline: pipeline ms =
> `clock.monotonic() × 1000`; VAD stream time maps onto it with `origin = arrival of the first in-order frame − 20 ms`,
> STT partials take their arrival time. The tasks of §1 that belong to the pipeline run in one `TaskGroup`
> (`inbound`, `stt`, `tts`, `outbound`, `endpoint`, `replies`, `watchdog`; since P04-T12c also `silence`, `dtmf`); the first end reason wins, any task
> failure ends with `error` (`pipeline_failed`), and the end of a vendor stream while running is an error. 16 kHz
> legs (L16 in `TELNYX_L16_BYTE_ORDER`, or little-endian `pcm_16000` for the widget) send little-endian PCM to the
> STT, feed the VAD without resampling and take `pcm_16000` TTS audio. Each utterance is one TTS context
> (`turn-{index}`; user and agent turns share one index counter, which is also the mark's turn number), one
> `send(..., flush=True)` per clause and `flush` at the end, and **one outbound segment**: ElevenLabs reports no
> per-clause audio boundary, so the utterance's mark follows its last frame (`OutboundAudio.finish_utterance(chars)`)
> and `played_ms` between start and that mark comes from the estimate. Playback completes on the mark echo or at
> `t_first + 60 + total + 200 ms` without it. A cancelled or failed utterance stops its tracker, clears the outbound
> queue locally, cancels its TTS context (unless `is_final` already arrived) and is recorded as interrupted when any
> audio went out. Brain timing (`resumption_speed`, `collecting_number`) is re-applied to the endpointer at start and
> after every reply. The opener is `brain.start()` at once; greeting waits (§11 steps 1–4, AMD) are `CallSession`'s
> concern. `EndCall` stops accepting outputs, plays `final_text` to completion bounded by 8 s, then ends with its
> reason. The watchdog speaks `max_duration_message` at `max_duration − 10 s` (cutting off any running reply with
> `transport.clear`; skipped once an `EndCall` is ending the call) and ends with `max_duration` at `max_duration`.
> Spoken defaults equal the specs/04 §7 org settings defaults (`max_duration_message` = "I'm sorry, we've reached the
> time limit for this call. Goodbye.", `no_input_goodbye`); the specs/05 `max_duration` row's wording is superseded
> by that org setting.
>
> Decision (2026-11-03) P05-T17e2: the stream origin is the smallest `arrival − 20 × k` over the frames read so
> far (frame `k` ends at stream time `20 × k`; `k` counts every frame fed, gap fill included). For the first frame
> this is the old rule; afterwards it can only move earlier. Frames that queued while a call attached (context
> load, subscriptions, the opener) used to be read in a burst, and anchoring the origin on the first of them moved
> every VAD time, end-of-turn deadline and `user_speech_end` of that call later by the backlog: at 50 simultaneous
> attaches 46–338 ms per call, constant for the whole call. The estimate is never earlier than the true send time
> plus the smallest transit (a held or late frame only yields a larger candidate). Measured: 50-call overhead
> p50 82–87 ms (was 140–211 ms with the batched VAD alone).

> Decision (2026-11-02): media call runtime (P05-T10a). The media lifespan builds the per-process collaborators
> once in `va_media.runtime.open_runtime` (KMS envelope + `OrgProviderStore` behind one `LlmGateway` on one HTTP
> client, prewarmed at startup with a logged-never-raised failure; `OrgSettingsReader`; the vendored token counter;
> the Silero model, loaded only after the readiness check verified the file — a load failure fails readiness —; one
> `VadExecutor` of `MEDIA_VAD_THREADS`) and uses the yielded `CallRuntime` as the session body of every attached
> call (an injected body replaces it in tests). Per call `CallRuntime` (1) refuses runners other than `task` (the
> pathway/persona/agent Brains arrive in phases 07/13), a process without a VAD model, and an org that no longer
> exists, without opening any vendor socket; (2) wraps the socket in `TelnyxMediaTransport` and maps the context
> with the pure `va_media.runtime_config`: `max_duration_s = max_duration_min × 60`, the warning at
> `min(10 s, max_duration_s / 2)`, the org's `max_duration_message`/`no_input_goodbye`, the call's interruption,
> IVR, no-input, DTMF and pronunciation settings; STT `ulaw_8000` (16 kHz legs `pcm_16000`) with the specs/08 §5
> row's `language_code` (an unknown value falls back to detection) and `keywords` as keyterms; TTS voice
> `provider_voice_id` + `voice_settings` + `ELEVENLABS_TTS_MODEL`, `language_code` pinned only for fixed language
> rows; (3) opens STT and TTS concurrently, both closed on every path; an open failure (`SttError`, `TtsError`,
> `OSError`) ends the call with `end_reason='error'` before any audio; (4) runs `CallPipeline` with a per-call
> `SileroVad` and `TaskBrain` (no tool executor until phase 08, `sms_capable=false` until messaging is wired; the
> default observer and action handler until P05-T11/T13). Vendor zero retention (`enable_logging=false`) applies
> when the org sets `privacy.zero_retention` or `hipaa_mode` **and** `ELEVENLABS_ZERO_RETENTION_AVAILABLE` is true;
> a wanted but unavailable option is logged `media.zero_retention_unavailable` and the call proceeds (the
> platform account capability is a deployment setting). The pipeline end maps onto `SessionEnd`: `transport_closed` →
> `stream_closed`, the pipeline's generic `agent_hangup` → `agent_end_call`, every specs/05 §9 reason → that reason
> with its `ended_by` (`va_db.call_states.END_REASONS`), anything else → `error` (logged). Turn metrics use
> `org_tier="unknown"` until the call context carries the plan id. Hangup, persistence and events stay with the
> end-of-call routine of P05-T11.

> Decision (2026-11-02): runtime lines and transfer callbacks (P05-T13). `CallPipeline.say(text) -> bool` is a
> sync seam that queues `text` as an interruptible static agent line after the current reply and its action
> finished (`False` for blank text or a closing pipeline); the future control `say` message reuses it.
> `MediaActions(transfer=ColdTransfer)` runs `transfer_call`; the runtime binds its outcome callbacks once the
> pipeline exists (`MediaActions.bind`): `bridged` → `pipeline.take_over(Takeover("transferred"))` (no message;
> the action returns right after and the pipeline end cancels nothing else), a failure →
> `TaskBrain.on_transfer_failed(msg)` + `pipeline.say(msg)` with the org's `transfer_failed_message`,
> `not_active` → nothing. Rules: specs/06 §10.1 Decision P05-T13.

## 17. Control channel messages (`call:{id}:control`)

```json
{"id": "<uuid>", "type": "stop", "reason": "api"}
{"id": "<uuid>", "type": "say", "text": "One moment please."}
{"id": "<uuid>", "type": "inject_context", "text": "Order #123 shipped today."}
{"id": "<uuid>", "type": "transfer", "phone_number": "+14155550100", "mode": "cold"}
{"id": "<uuid>", "type": "takeover", "phone_number": "+14155550100"}
{"id": "<uuid>", "type": "credits_exhausted"}
{"id": "<uuid>", "type": "drain_notice"}
```

`takeover` = cold transfer without a spoken line. `credits_exhausted` = speak `max_duration_message` variant and end
(`24` §6). The owner writes `SET call:{id}:control_ack:{id} 1 EX 30` after handling. Carrier channel
(`call:{id}:carrier`) carries `{"type": "<telnyx event_type>", "leg": "...", "payload": {...}}`.

> Decision (2026-11-01): the carrier channel message is `va_domain.carrier.CarrierEvent` (P05-T02): the three fields
> above plus `call_id`, `org_id`, `telnyx_event_id` (dedupe key), and `occurred_at` (specs/06 §5 Decision P05-T02).

> Decision (2026-11-02): the control messages are the `va_domain.control` models (`parse_control`,
> `control_channel`, `control_ack_key`); handling and acknowledgement rules: §3 Decision P05-T07b.

## 18. End of call routine

1. Stop accepting engine outputs; if the end was agent-initiated, finish the final line (wait mark echo, max 8 s).
2. Telnyx `hangup` on any live legs we own (ignore `CallAlreadyEnded`).
3. Close STT/TTS sockets (`close_socket`), cancel tasks.
4. Flush turns and events (Postgres write-behind), update `calls` (`ended_at`, `connected_seconds` from
   answered→hangup carrier timestamps, `end_reason`, `ended_by`, `variables`), release concurrency slot
   (`ZREM`), release ownership (`DEL` only if value == POD_ID via Lua), delete heartbeat.
5. Signal `CallWorkflow.signal_pipeline_finished(summary)`.
6. Emit `call.ended`.

`duration_watchdog`: at `max_duration − 10 s` speaks `max_duration_message` (interruptibility 0); at `max_duration`
ends with `end_reason='max_duration'`.

> Decision (2026-11-02): media end-of-call routine (P05-T11b). `va_media.call_end.CallEnd` is the `SessionEnder`
> of every `CallSession` whose body is the call runtime (`CallRuntime.end_routine`, built by `open_runtime`; a
> test body without `end_routine` runs no routine). It runs once after the session race and before the session
> cleanup, so the ownership lock and the heartbeat are still held (the edge sees an owner and leaves the call row
> to the worker); `end` is `None` when the body raised; an exception of the routine is logged
> `media.end_routine_failed` and never skips the cleanup (heartbeat delete, lock release, socket close). Steps:
> 1. Resolve: a raised body → `error` ("The call pipeline failed."), record + hang up. `worker_lost` (ownership
>    loss, drain timeout) → no record, no hangup (the workflow does both, specs/01 §2.1 Decision). A closed media
>    stream (`stream_closed`) re-reads `call:{<id>}:carrier_state` (the edge `HSET`s before it `PUBLISH`es, so a
>    hangup the carrier watch already consumed is in the hash as `primary_hangup=1`), else reads the carrier feed
>    (now its only reader) for the primary leg's `call.hangup` for up to 3 s of the injected clock: hung up →
>    `max_duration` when `hangup_cause='time_limit'`, `transferred` when the row is `transferred`, else
>    `user_hangup`; no hangup; `ended_at` = the primary leg's `ended_at` (else now). Nothing in 3 s → `error`
>    ("The media stream closed while the call was up.") and a hangup. Every other end keeps its reason (an
>    unknown one is `error`), `ended_at` = now, and hangs up.
> 2. Record (one tenant transaction): only an answered call (`in_progress`/`transferring`/`transferred`) is moved
>    to `completed` through the shared `va_db.call_end.record_answered_end` (also used by the `end_call`
>    activity): a stored `end_reason`/`error_message` wins, `ended_by` from specs/05 §9, connected/transfer
>    seconds from `call_durations` with the carrier state's `transfer_started_at`. A terminal call is left
>    alone; an unanswered one is left to the workflow's cleanup. A lost race (`CallTransitionError`) is logged
>    `media.end_unrecorded`; database errors `media.end_record_failed`.
> 3. Hang up the primary leg after the record (the hangup webhook then finds `end_reason` set) with
>    `command_id("hangup", call_id, "primary")` (the workflow's id: Telnyx dedupes) and our `client_state`;
>    already ended is fine; Telnyx/account failures are logged `media.hangup_failed`.
> 4. `call.ended` (`{end_reason, ended_by}`, `va_events.call_ended_event`) only when step 2 wrote the row, then
>    the call's `call_events` rows are flushed. 5. The concurrency slot is released (`ZREM`). 6.
>    `signal_pipeline_finished(PipelineFinished(end_reason, ended_by))` (the recorded reason, else the resolved
>    one) through `TemporalPipelineSignaller` (a lazily connected client, not a readiness check: specs/07 §2); a
>    missing workflow is logged `media.workflow_missing`, any other failure `media.signal_failed`, never raised.
> Each step logs its own failure and the next step still runs. This order (record, hang up, `call.ended`, slot,
> signal) replaces the list above for steps 2-6; sockets are closed by the runtime before the routine runs.

> Decision (2026-11-02): a bridged cold transfer ends the session with `transferred` (P05-T13). Step 1 resolves it
> to: no record, no hangup, no `call.ended`, no slot release (caller and target are still talking; the edge
> records `completed`/`transferred` on the primary hangup and `CallWorkflow` releases the slot). Step 3 sends
> `streaming_stop` on the primary leg instead (`command_id("streaming_stop", call_id, "primary")`, our
> `client_state`; failures logged `media.streaming_stop_failed`); the event rows are flushed and
> `signal_pipeline_finished(transferred, system)` is sent as usual.

## 19. Ownership, heartbeat, drain, failures

- Renewal every 5 s via Lua `if GET == POD_ID then PEXPIRE 15000`. Failure to renew (key lost) → end immediately with
  `end_reason='worker_lost'` without hangup race (workflow issues hangup).
- Heartbeat `SET call:{id}:hb <ts> EX 10` every 3 s.
  > Decision (2026-11-02): the heartbeat is watched by the `CallWorkflow` activity `watch_call_heartbeat`
  > (`call:{<id>}:hb` absent 20 s → `end_reason='worker_crash'` + hangup); see specs/01 §2.1 Decision P05-T06c.
- Drain on SIGTERM: add pod to `media:pods:draining`, readiness false, publish `drain_notice` to sessions (engine
  logs only), wait for sessions to end or `MEDIA_DRAIN_TIMEOUT_S`, then end remaining with `end_reason='worker_lost'`.
  > Decision (2026-11-02): the drain notice is delivered locally to each session's control listener (not
  > published to Redis); the timeout and the 10 s cleanup grace run on the injected clock (§3 Decision P05-T07b).
- STT socket failure: reconnect (50/100/200 ms, 3 attempts) buffering ≤ 2 s audio; resend buffered audio; on final
  failure → error path.
- TTS socket failure: reconnect, re-init context, resend the current segment text from the unsent position.
- LLM failure: gateway fallback model (`10` §6); total failure → speak `error_phrase` from cache → transfer to
  `fallback_number`/`transfer_phone_number` if configured, else end with `end_reason='error'`.
- Telnyx WS closed unexpectedly while call is up: wait 3 s for reconnect (Telnyx may reconnect); else hang up.

## 20. Latency metrics (exact definitions; persisted in `call_turns.latency` and ClickHouse)

| Metric | Start | End |
|---|---|---|
| `eot_ms` | end of last voiced VAD window of the user turn | EndOfTurn decision |
| `stt_final_ms` | `commit` sent to Scribe | `committed_transcript` received |
| `route_ms` | engine router call sent | router result parsed |
| `tool_ms` | tool HTTP request sent | response received |
| `llm_ttft_ms` | dialogue LLM request sent | first non-empty content delta |
| `tts_ttfa_ms` | first text sent to TTS context | first audio chunk for that context |
| `e2e_ms` | end of last voiced VAD window | first agent audio frame of the reply sent to Telnyx |
| `reaction_ms` (interrupts) | user speech start | `clear` sent |

OpenTelemetry: one span per turn `turn` with child spans `eot`, `stt_final`, `route`, `llm`, `tts_first_audio`,
`tool:{name}`; attributes `call_id`, `org_id`, `turn_index`, `node_id`. Prometheus histograms
`va_turn_e2e_ms`, `va_llm_ttft_ms`, `va_tts_ttfa_ms`, `va_stt_final_ms`, `va_eot_ms` (buckets 50…3 000 ms).

> Decision (2026-11-01) P04-T10: the tracer is `va_voice.latency`. The pipeline records `TurnMark`s on the
> monotonic ms timeline into a `TurnTimer` (first value wins, except `user_speech_end`, where the last voiced
> window wins because resumed speech moves it). `LatencyTracer.finish` computes `TurnLatency` (whole ms, rounded
> half up; a metric is unmeasured when a mark is missing, or when its end precedes its start, which is logged as
> `turn_latency_misordered`; `tool_ms` = sum of the turn's tool durations), observes metrics, emits spans, and
> returns the `call.latency` events. (1) Histogram names follow the specs/27-observability.md §3.3 catalog:
> `va_turn_eot_ms`, not `va_eot_ms`. (2) `TurnMetrics` observes only `va_turn_e2e_ms{org_tier}`, `va_turn_eot_ms`,
> `va_barge_in_total{honored}`, and `va_barge_in_reaction_ms` (honored barge-ins only). The vendor clients already
> observe `va_stt_final_ms`, `va_tts_ttfa_ms`, and `va_llm_ttft_ms` with their own labels, so the tracer does not
> observe them a second time. (3) `stt_final_ms` starts at the `stt_commit` mark. The commit is sent at the EOT
> decision, so this is the same instant as specs/27 §4's "EOT decision". (4) Spans are emitted when the turn
> finishes, with explicit timestamps (monotonic ms mapped to epoch ns through an anchor taken when the
> `TurnTracer` is built). The span names are those of the specs/27 §2.1 catalog: `turn`, `turn.eot`,
> `turn.stt_final`, `turn.route`, `turn.llm`, `turn.tts_first_audio`, `turn.tool <name>`. `turn` spans from the
> earliest to the latest recorded instant, and a child span exists only for a measured metric. Each span carries
> its metric attribute (`va.eot_ms`, ...). Attributes that only the caller knows (`va.chars`,
> `va.silence_threshold_ms`, `va.route.to`, ...) are attached with `TurnTimer.annotate(span, {...})`. (5) The summary
> event `"E2E: {n}ms"` (data = the measured `call_turns.latency` keys + `turn_index`) exists only when `e2e_ms` was
> measured, so a turn interrupted before its first audio has none. It is followed by `"STT: {n}ms"`, `"LLM: {n}ms"`,
> and `"TTS: {n}ms"`, each only when that stage was measured.

> Decision (2026-11-02): latency of tool-call and end-call replies (P05-T17b). (1) A dialogue round whose stream
> carries no content (the model answered with tool calls only) has no "first non-empty content delta"; its
> `llm_ttft_ms` ends when that stream ends, the moment the model's answer (the tool calls) has arrived. The first
> mark wins, so a text reply keeps the time of its first delta, and a turn whose first round called a tool keeps
> that round's time. (2) An `EndCall` output with `final_text` that answers a user turn is that turn's reply: the
> final line is spoken on the turn's timer, so `tts_ttfa_ms` and `e2e_ms` are measured to the goodbye and the
> turn gets its `"E2E: {n}ms"` event (before, an `end_call` turn had no summary event and its LLM time read as
> platform overhead in the e2e harness).

## 21. Performance requirements and CPU budget

Per call at 2 vCPU pod: VAD ≈ 0.3 ms / 32 ms window (≈ 1 % core), codec + mixing ≈ 0.2 %, JSON/base64 for 50 msgs/s
in + 50 out ≈ 0.6 %, STT/TTS WS ≈ 0.5 %. Budget ≤ 3 % of one core per call → 60 calls ≈ 1.8 cores.
Load test asserts: no outbound frame scheduled later than 30 ms past its target (p99) at 50 concurrent simulated
calls; event loop lag p99 ≤ 10 ms (`va_event_loop_lag_ms`).

> Decision (2026-11-01) P04-T13: the phase 04 pipeline benchmark is `packages/va_voice/tests/bench/`
> (`uv run pytest -m bench packages/va_voice`; bench tests are deselected unless the `-m` expression mentions
> `bench`, so `make test` never runs timing asserts). Platform overhead per turn =
> `e2e_ms − eot_ms − (stt_final_ms + llm_ttft_ms + tts_ttfa_ms)` from the turn's `TurnLatency` (endpointing
> silence is policy, not overhead; unmeasured vendor terms count as 0). With zero-latency fakes the vendor terms
> are the fakes' in-process time, so the gross `e2e_ms − eot_ms` is gated too. Runs: (a) 200 turns of one harness
> call on the accelerated clock (scheduling cost in virtual ms), (b) 30 turns on the wall clock
> (`va_sim.clock.RealtimeClock`, real CPU and event-loop cost), both p50 ≤ 30 ms; (c) 50 harness pipelines × 3
> turns on one event loop on the wall clock: every turn delivered and zero outbound frames more than 30 ms past
> their target (`OutboundStats.late_frames`); event-loop lag (10 ms sleep overshoot) is reported, not asserted
> (the p99 assert belongs to the phase 05 load test). Percentiles are nearest-rank. A complete, non-xdist run
> rewrites `packages/va_voice/BENCHMARKS.md` (committed; never edited by hand).
>
> Decision (2026-11-03) P05-T17e2: single-pod capacity (specs/30 §6). The media worker (and the simulator runner)
> run on uvloop as §1 states (`serve(..., loop_factory=uvloop.new_event_loop)`; the other services keep the asyncio
> loop). `va_media.main.tune_gc()` runs `gc.collect()`, `gc.freeze()` and `gc.set_threshold(50_000, 20, 20)` before
> serving: startup objects never get walked again and per-frame garbage (freed by reference counting) no longer
> triggers a gen-0 pass every 700 allocations. VAD inference does use the `MEDIA_VAD_THREADS` pool (§5.3 Decisions
> P04-T03 and P05-T17e2), superseding §1's "runs inline". Measured with `make e2e-capacity` on the 8 vCPU dev host:
> 50 calls, none dropped, 50 attached at once, 0 underruns, overhead p50/p95 82–87/110–137 ms, loop lag p99
> ≤ 10 ms; 10 calls overhead p50 76 ms. Profile (py-spy `--gil`, 50 calls): the event loop holds the GIL ≈ 60 % of
> the time (was ≈ 75 % before uvloop), inbound path ≈ 20 %, gate ≈ 7 %, ONNX ≈ 4 % on the pool threads.

## 22. Tests required

- Codec round trips and vectors; Framer padding; L16 byte order conversion.
- ReorderBuffer: out-of-order, gaps, duplicates (hypothesis property tests).
- VAD wrapper: shape/state handling with the real ONNX model on fixture WAVs (speech, silence, noise, DTMF);
  hysteresis and energy floor.
- Endpointer: table-driven over resumption speeds, adaptive pauses, completeness multipliers, ivr floor, fixed
  `interruption_threshold`.
- Interruptibility matrix: every level × (backchannel / short / long speech) scenario; block_interruptions; DTMF.
- Barge-in sequence ordering and timing (fake clock), truncation to spoken words with alignment fixtures, false
  barge-in resume.
- OutboundClock drift and lead control; marks; background mixing levels (RMS within ±1 dB).
- SpectralGate reduces stationary noise ≥ 10 dB on fixture while VAD speech recall ≥ 95 %.
- Greeting, voicemail (each action), no-input reprompts and hangup, max_duration warning, precall DTMF timing.
- Control messages; ownership loss; drain; STT/TTS reconnect; LLM fallback and error phrase.
- E2E simulator: scripted conversations measuring platform overhead (`e2e_ms − (sim STT + LLM + TTS latencies)`)
  ≤ 150 ms p50, ≤ 250 ms p95.

## 23. Acceptance criteria

1. Simulator e2e suite passes all scenarios in `30-testing-and-evals.md` §5 that involve audio.
2. Barge-in reaction p95 ≤ 120 ms in simulator runs.
3. 50 concurrent simulated calls per pod with no frame later than 30 ms (p99) and zero dropped sessions.
4. Every turn persists the latency fields defined in §20.
