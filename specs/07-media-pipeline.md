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
8. Start greeting logic (§11).

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

## 5. Inbound path

### 5.1 Reorder buffer

Telnyx does not guarantee order. `ReorderBuffer(window=5)` keyed on `int(media.chunk)`: emits in order; if the next
expected chunk is missing while 5 later chunks are buffered (≈ 100 ms), it inserts one silence frame for the gap and
continues (counts `media_gap_frames_total`). Duplicates (chunk ≤ last emitted) are dropped. Only `track` inbound
frames are processed on primary legs.

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

Rate-limit backoff: on Telnyx error `100005`, the clock pauses sends for 100 ms and reduces lead to 1 frame for 5 s.

## 10. Audio conditioning

### 10.1 `noise_cancellation`

Decision: noise handling is split by consumer, with no added latency on the STT path.
- STT path: when `noise_cancellation=true`, Scribe is opened with `filter_background_audio=true` (`08` §3); raw audio
  is sent unmodified.
- VAD path: `SpectralGate` on 16 kHz PCM — STFT 512-point Hann, hop 256; noise profile = per-bin 20th percentile of
  magnitude over the trailing 1.5 s of non-speech windows (updated only during VAD non-speech); gain per bin
  `g = max(0.25, 1 − (1.5 × noise / max(mag, ε)))` (floor −12 dB), smoothed over time with α = 0.6; inverse STFT with
  overlap-add. Added VAD latency 16 ms (accounted in `eot_ms`). When `noise_cancellation=false` the gate is bypassed.

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

## 11. Greeting logic

1. Outbound: wait for `call.answered` (from carrier state).
2. If AMD enabled: wait for `call.amd` result up to 3 000 ms after answer while VAD/STT run.
   - `machine` → voicemail flow (§14). `human` → continue. Timeout → treat as human (classification continues and can
     still switch to voicemail flow until `voicemail.detect_timeout_ms`).
3. `precall_dtmf_sequence` → send (`06` §9), then wait 500 ms.
4. `wait_for_greeting=true` → stay silent until first user `EndOfTurn` or `greeting_timeout_ms`, then the engine runs
   with that user turn (or an empty one on timeout).
5. Else speak the opener: `first_sentence` (templated) from the phrase cache when available (pre-rendered during
   prefetch, key per `09` §9), else generated by the engine from the start node.
6. Inbound: answer → immediately step 5 (no AMD) unless `wait_for_greeting`.

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

## 15. `hd_audio` mode

- Dial/answer with `stream_codec="L16"`, `stream_bidirectional_codec="L16"`, `stream_bidirectional_sampling_rate=16000`.
- Wire byte order is controlled by `TELNYX_L16_BYTE_ORDER` (default `big`, per RFC 3551 L16). Inbound: convert to
  native int16; outbound: convert ElevenLabs `pcm_16000` (little-endian) to the wire order.
- Scribe `audio_format=pcm_16000` receives little-endian PCM (converted); TTS `output_format=pcm_16000`.
- VAD uses the 16 kHz stream directly (no resample).
- Live smoke test `test_hd_audio_byte_order` plays a 1 kHz tone and asserts STT/energy sanity in both directions.

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

## 19. Ownership, heartbeat, drain, failures

- Renewal every 5 s via Lua `if GET == POD_ID then PEXPIRE 15000`. Failure to renew (key lost) → end immediately with
  `end_reason='worker_lost'` without hangup race (workflow issues hangup).
- Heartbeat `SET call:{id}:hb <ts> EX 10` every 3 s.
- Drain on SIGTERM: add pod to `media:pods:draining`, readiness false, publish `drain_notice` to sessions (engine
  logs only), wait for sessions to end or `MEDIA_DRAIN_TIMEOUT_S`, then end remaining with `end_reason='worker_lost'`.
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

## 21. Performance requirements and CPU budget

Per call at 2 vCPU pod: VAD ≈ 0.3 ms / 32 ms window (≈ 1 % core), codec + mixing ≈ 0.2 %, JSON/base64 for 50 msgs/s
in + 50 out ≈ 0.6 %, STT/TTS WS ≈ 0.5 %. Budget ≤ 3 % of one core per call → 60 calls ≈ 1.8 cores.
Load test asserts: no outbound frame scheduled later than 30 ms past its target (p99) at 50 concurrent simulated
calls; event loop lag p99 ≤ 10 ms (`va_event_loop_lag_ms`).

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
