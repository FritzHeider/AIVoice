# 08 — Speech-to-Text with ElevenLabs Scribe

Package: `packages/va_elevenlabs/src/va_elevenlabs/stt_realtime.py` (streaming), `stt_batch.py` (post-call),
interface in `packages/va_voice/src/va_voice/stt.py`.

## 1. Provider interface

```python
class SttConfig(BaseModel):
    audio_format: Literal["ulaw_8000", "pcm_16000"]
    sample_rate: int                       # 8000 or 16000
    language_code: str | None              # ISO 639-1/639-3; None = auto-detect
    include_language_detection: bool
    keyterms: list[str]
    filter_background_audio: bool
    entity_detection: list[str]            # [] or ["pii","pci","phi"] when redaction enabled
    previous_text: str | None              # sent on the first chunk only

class SttEvent(BaseModel):
    kind: Literal["partial", "committed", "error", "warning"]
    text: str = ""
    words: list[SttWord] = []              # committed only: text, start_s, end_s, logprob
    language_code: str | None = None
    entities: list[SttEntity] = []         # text, entity_type, start_char, end_char
    received_at: float                     # loop.time()
    error_type: str | None = None

class StreamingStt(Protocol):
    async def open(self, cfg: SttConfig) -> None: ...
    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None: ...
    async def commit(self) -> None: ...
    def events(self) -> AsyncIterator[SttEvent]: ...
    async def close(self) -> None: ...
```

`ElevenLabsRealtimeStt` is the only implementation in v1; the simulator implements the same wire protocol.

## 2. Connection

- URL: `{ELEVENLABS_WS_BASE}/v1/speech-to-text/realtime?{query}`; production base `wss://api.elevenlabs.io`
  (orgs with `data_region='eu'` use `wss://api.eu.residency.elevenlabs.io`; selected by `va_elevenlabs.regions`).
- Header: `xi-api-key: {ELEVENLABS_API_KEY}`.
- `websockets.connect(..., open_timeout=2, ping_interval=None, max_size=2**22, compression=None)` — keepalive is
  handled by the server parameter `keepalive_interval_ms` and our continuous audio.
- One session per call leg, opened during attach (`07` §3) in parallel with TTS.

## 3. Query parameters (exact)

| Param | Value |
|---|---|
| `model_id` | `ELEVENLABS_STT_MODEL` (`scribe_v2_realtime`) |
| `audio_format` | `ulaw_8000` (default) / `pcm_16000` (hd_audio, widget) |
| `commit_strategy` | `manual` (our endpointer decides; `07` §6) |
| `include_timestamps` | `true` |
| `include_language_detection` | `true` when language_code is null, else `false` |
| `language_code` | from §5 mapping (omitted when null) |
| `keyterms` | repeated query param, one per term (`keyterms=Blandy&keyterms=Acme+Corp`) |
| `filter_background_audio` | `true` when call `noise_cancellation=true` |
| `entity_detection` | `pii` repeated with `pci` when org setting `privacy.redact_transcripts=true`; omitted otherwise |
| `keepalive_interval_ms` | `5000` |
| `enable_logging` | `false` when org setting `privacy.zero_retention=true` (enterprise ElevenLabs only), else omitted |
| `no_verbatim` | `false` (we keep disfluencies for endpointing) |

The live smoke test `test_scribe_keyterms_query_encoding` asserts the server accepts repeated `keyterms` params
(it echoes them in `session_started.config.keyterms`); if the server reports a different encoding the client switches
to the JSON-array form `keyterms=["a","b"]` controlled by `ELEVENLABS_STT_KEYTERMS_ENCODING` (`repeated` | `json`).

## 4. Wire protocol

Client → server:

```json
{"message_type": "input_audio_chunk", "audio_base_64": "<base64>", "commit": false, "sample_rate": 8000, "previous_text": "Hi, this is Karen from Acme."}
```

- `previous_text` is included **only on the first chunk** (sending it later is an error). Value: the agent's opener
  text when known at attach time (helps recognition of the reply), else omitted.
- Audio batching: the `stt_sender` accumulates 5 frames (100 ms) per message. On `EndOfTurn` it immediately sends the
  buffered frames with `"commit": true`; if the buffer is empty it sends one 20 ms codec-silence frame (`0xFF`×160 for
  μ-law, zeros×640 for PCM16) with `"commit": true`.
- Minimum interval between commits: 250 ms (protects against `commit_throttled`); a second EOT inside the window is
  merged into the pending commit.
- Audio is sent continuously during the whole call, including while the agent speaks (barge-in needs partials).

Server → client handling:

| `message_type` | Action |
|---|---|
| `session_started` | store `session_id`, verify echoed config (`audio_format`, `sample_rate`, `commit_strategy=manual`); mismatch → error path |
| `partial_transcript` | emit `SttEvent(partial)`; endpointer and interruptibility consume it |
| `committed_transcript` | ignored when timestamps are enabled (the `_with_timestamps` variant follows); used as fallback if the timestamp variant has not arrived within 300 ms |
| `committed_transcript_with_timestamps` | emit `SttEvent(committed)` with `words` (type `word` only; `spacing` dropped), `language_code` |
| `committed_transcript_entities` | attach entities to the most recent committed event (by text equality) and emit an update; used by redaction (`28-security.md` §5) |
| `edited_transcript` | ignored (we do not send `transcript_edit`) |
| `warning` | log + `call.error` event at level `warning` with code `stt_warning` |
| `error` | log; reconnect path |
| `auth_error` | fatal for the call; alert `stt_auth_error` (platform misconfiguration) |
| `quota_exceeded` | fatal; alert; call error path |
| `commit_throttled` | delay next commit by 250 ms and resend commit |
| `unaccepted_terms` | fatal; alert |
| `rate_limited` | reconnect with backoff 200/400/800 ms |
| `queue_overflow` | reduce batch to 200 ms messages for the rest of the session; log |
| `resource_exhausted` | reconnect with backoff |
| `session_time_limit_exceeded` | seamless rotation (§7) |
| `input_error` | log at error; continue (our bug → alert) |
| `invalid_request` | fatal for the session; reconnect once with a minimal config (no keyterms, no entity detection) |
| `chunk_size_exceeded` | split messages to 50 ms; continue |
| `insufficient_audio_activity` | ignore (commit on silence) |
| `transcriber_error` | reconnect with backoff |

## 5. Language mapping (Bland codes → Scribe + TTS)

`language` param values accepted and their mapping (`va_elevenlabs.languages.LANGUAGE_MAP`):

| Bland `language` | Scribe `language_code` | Detection | TTS `language_code` (flash v2.5) |
|---|---|---|---|
| `babel-en` (default), `en`, `en-US`, `en-GB`, `en-AU`, `en-NZ`, `en-IN` | `en` | off | `en` |
| `babel` | omitted | on | detected language if supported by TTS, else `en` |
| `auto` | omitted | on | detected ∈ {`en`,`es`} else `en` |
| `fluent` | omitted | on | detected ∈ {`en`,`es`,`de`,`fr`,`pt`,`it`} else `en` |
| `es`, `es-419`, `es-ES`, `es-MX`, `babel-es` | `es` | off | `es` |
| `fr`, `fr-FR`, `fr-CA`, `babel-fr` | `fr` | off | `fr` |
| `de`, `de-DE`, `babel-de` | `de` | off | `de` |
| `it`, `babel-it` | `it` | off | `it` |
| `pt`, `pt-BR`, `pt-PT`, `babel-pt` | `pt` | off | `pt` |
| `nl`, `babel-nl` | `nl` | off | `nl` |
| `ja`, `babel-ja` | `ja` | off | `ja` |
| `zh`, `zh-CN`, `zh-Hans`, `zh-TW`, `zh-Hant`, `babel-zh` | `zh` | off | `zh` |
| `ko`, `babel-ko` | `ko` | off | `ko` |
| `hi`, `hi-Latn`, `babel-hi` | `hi` | off | `hi` |
| `ru`, `babel-ru` | `ru` | off | `ru` |
| `pl`, `babel-pl` | `pl` | off | `pl` |
| `tr`, `babel-tr` | `tr` | off | `tr` |
| `sv`, `babel-sv` | `sv` | off | `sv` |
| `da`, `babel-da` | `da` | off | `da` |
| `no`, `nb`, `babel-no` | `no` | off | `no` |
| `fi`, `babel-fi` | `fi` | off | `fi` |
| `uk`, `babel-uk` | `uk` | off | `uk` |
| `cs`, `babel-cs` | `cs` | off | `cs` |
| `el`, `babel-el` | `el` | off | `el` |
| `ro`, `babel-ro` | `ro` | off | `ro` |
| `hu`, `babel-hu` | `hu` | off | `hu` |
| `id`, `babel-id` | `id` | off | `id` |
| `ms`, `babel-ms` | `ms` | off | `ms` |
| `vi`, `babel-vi` | `vi` | off | `vi` |
| `th`, `babel-th` | `th` | off | `en` (TTS unsupported → English voice; org warned at config time) |
| `ar`, `babel-ar` | `ar` | off | `ar` |
| `he`, `babel-he` | `he` | off | `en` (warned) |
| `ta`, `babel-ta` | `ta` | off | `ta` |
| `bg`, `babel-bg` | `bg` | off | `bg` |
| `hr`, `babel-hr` | `hr` | off | `hr` |
| `sk`, `babel-sk` | `sk` | off | `sk` |
| `fil`, `tl`, `babel-fil` | `fil` | off | `fil` |
| `ca`, `babel-ca` | `ca` | off | `en` (warned) |

Unknown values → `400 INVALID_INPUT` field `language`. The TTS-supported set is `va_elevenlabs.languages.TTS_SUPPORTED`
(the 32 flash v2.5 languages); a contract test fetches `GET /v1/models` in the live suite and asserts our set ⊆ the
model's `languages[].language_id`.

When detection is on, the session's working language is set from the first committed transcript's
`language_code` with `language_probability` not available in realtime → we use the first two committed utterances:
if both agree, lock; else use the latest. The engine receives `language_code` on each `UserTurn` and passes it to TTS
(`09` §3) and to the dialogue prompt ("Respond in {language_name}").

## 6. Keyterms

Sources merged in order: call `keywords`, pathway `global_config.keywords`, node-level `keywords` of the current
node (requires reconnect → only applied at call start; node keywords are unioned at call start for all nodes of the
pathway), the agent's own name and company from persona config, and the voice name.

`keywords` item format `word` or `word:boost` (boost 1–10, default 2). Scribe realtime keyterms carry no weight, so:
1. Parse and validate each term: < 50 chars, ≤ 5 words, no characters from `< > { } [ ] \`; invalid terms are dropped
   with a warning event at call start.
2. Deduplicate case-insensitively, sort by boost desc then source order.
3. Cap at 100 terms (realtime); excess dropped with warning. Terms with boost ≥ 5 are also added to the
   LLM system prompt glossary ("Important names and terms: …") to bias generation/spelling.

## 7. Session rotation and reconnection

- Proactive rotation: when session age > `ELEVENLABS_STT_SESSION_ROTATE_S` (default 1 500 s) and the user is silent
  (no speech for ≥ 1 s, agent may be speaking), open a new session in parallel, wait for `session_started`, switch the
  sender to it, then close the old one after 500 ms (drains in-flight partials). `previous_text` for the new session =
  last 300 chars of committed user + assistant text.
- Reactive (`session_time_limit_exceeded`, socket close, network error): reconnect immediately; frames produced
  during the gap are buffered (max 2 s, then oldest dropped); after `session_started` the buffer is sent in 100 ms
  messages. If a commit was pending it is re-sent. Attempts: 3 (50, 100, 200 ms); failure → call error path (`07` §19).
- Every reconnect emits `call.error` level `warning` code `stt_reconnect`.

> Decision (2026-11-01): realtime STT client (P03-T11). `va_elevenlabs.stt_realtime.ElevenLabsRealtimeStt(conn:
> SttConnection, *, clock, metrics, tracer_provider)` implements §1 (`va_voice` does not exist yet, so the
> `StreamingStt` Protocol and the models live in `va_elevenlabs.stt_models`, like `StreamingTts`); wire format in
> `stt_wire`, errors and the §4 action table in `stt_errors`, batching/replay in `stt_buffer`, commit bookkeeping
> in `stt_commits`, §5 in `languages`, §6 in `keyterms`. The `regions` module (EU base URL) is out of scope here.
> 1. `open(cfg)` connects (`xi-api-key`, `proxy=None`, `open_timeout` 2 s, `ping_interval=None`, `max_size=2**22`,
>    `compression=None`), waits for `session_started` (within `open_timeout`) and verifies the echoed
>    `audio_format`, `sample_rate`, `commit_strategy == "manual"` (mismatch → `SttProtocolError` code
>    `config_mismatch`, fatal). Connect errors and non-auth handshake statuses retry after 50/100/200 ms (then
>    `SttUnavailableError`); handshake 401/403 → `SttAuthError`. A fatal open raises and also yields the error event.
> 2. `SttConfig.include_language_detection` must equal `language_code is None` and `sample_rate` must match
>    `audio_format` (validators). `received_at` is the injected clock's `monotonic()` (= `loop.time()` in production).
> 3. `send_audio` takes whole codec frames (odd PCM16 byte counts are `ValueError`) and sends one message once
>    `batch_ms` (100) is buffered. A commit sends the buffer with `"commit": true`, or one 20 ms codec-silence
>    frame when it is empty. Every chunk carries `sample_rate`; `previous_text` only the first chunk of each session.
> 4. A commit within 250 ms of the previous one is deferred to `last + 0.25 s` (full batches keep flowing); more
>    commits inside the window merge into it. `commit_throttled` drops the newest outstanding commit and defers a
>    resend by 250 ms (same path).
> 5. Outstanding commits are a FIFO (request time, replay mark). `committed_transcript` pops the head and waits
>    300 ms for `committed_transcript_with_timestamps`, which emits `SttEvent(committed)` with `words` (type `word`
>    only) and `language_code`; on expiry the plain text is emitted with `words=[]` and the late variant is dropped.
>    `va_stt_final_ms` = commit request → committed event. `committed_transcript_entities` re-emits the most recent
>    of the last 8 committed events with equal text as `SttEvent(committed, entities=[...], is_update=True)` (new
>    field `is_update: bool = False`); no match → logged, dropped. Committed text and `add_context(text)` (agent
>    turns) form the context whose last 300 chars become a new session's `previous_text`.
> 6. Server rows: `warning` → `SttEvent(warning, error_type="stt_warning", text=error)`; `auth_error`,
>    `quota_exceeded`, `unaccepted_terms` → `SttAuthError`/`SttQuotaError`/`SttTermsError` (fatal);
>    `invalid_request` → reconnect once with a minimal config (no keyterms, no entity detection), a second one is a
>    fatal `SttProtocolError`; `rate_limited`, `resource_exhausted`, `transcriber_error` → reconnect with 200/400/800 ms
>    delays; `error`, `session_time_limit_exceeded`, and any other close or network error → reconnect with
>    50/100/200 ms; `queue_overflow` → 200 ms messages from then on; `chunk_size_exceeded` → 50 ms messages (the
>    dropped message is not resent); `input_error` → logged at error, continue. Every error row is counted in
>    `va_vendor_errors_total{vendor="elevenlabs_stt", code=<message_type>}`; the same rows during the handshake
>    are fatal (fatal rows), switch to the minimal config (`invalid_request`), or fail the attempt (others). A close
>    without an error frame: 1008 `Invalid API key`/`Missing API key` → `SttAuthError`; 1008 `invalid …` → as
>    `invalid_request`; else reconnect. Malformed frames are a fatal `SttProtocolError` (`invalid_server_frame`).
>    A fatal error yields `SttEvent(error, error_type=<code>)`, ends `events()`, and is raised by later
>    `send_audio`/`commit`.
> 7. Reconnect: the replay buffer keeps every byte sent (and produced during the gap) since the last answered
>    commit, at most `replay_max_s` (2 s, oldest dropped). After the new `session_started` it is resent in
>    `batch_ms` pieces, the first with the context as `previous_text`; when commits are outstanding they collapse
>    into one, re-sent with `commit: true` on the piece ending at the newest commit's mark (a silence frame when that
>    audio was dropped); plain transcripts still waiting for their variant are emitted first. One
>    `SttEvent(warning, error_type="stt_reconnect")` per reconnect; each attempt counts
>    `va_vendor_ws_reconnects_total`. Attempts follow the row's delays; a session that ended before producing a
>    transcript frame and within `healthy_after_s` (1 s) counts as a failed attempt of the same reconnect, so three
>    consecutive failures → `SttUnavailableError`.
> 8. Rotation: the engine reads `rotation_due` (session age ≥ `rotate_after_s`, 1 500 s) and calls `rotate()` while
>    the user is silent: a second session is opened, the sender switches to it, the old socket keeps delivering
>    events for `rotate_drain_s` (0.5 s) and is then closed (1000). A failed rotation keeps the old session and
>    yields `stt_warning`.
> 9. `audio_seconds` = caller audio accepted by `send_audio` (not replays, not silence commits; §9). Spans:
>    `stt.session` (the client's life, `va.stt.reconnects`) and `elevenlabs.stt.connect` per attempt
>    (`elevenlabs.model_id`, `elevenlabs.attempt`, `elevenlabs.rotation`).
> 10. `languages.resolve_language` is case-insensitive; detection (`babel`, `auto`, `fluent`) picks the TTS
>     language from `tts_choices` (`TTS_SUPPORTED`, {en, es}, {en, es, de, fr, pt, it}) else `en`; `LanguageLock`
>     locks when two consecutive committed languages agree. `keyterms.merge_keyterms` keeps the first spelling of a
>     case-insensitive duplicate with the highest boost; a `:suffix` is a boost only when it is an integer.

## 8. Post-call batch transcription (`stt_batch.py`)

Used by `PostCallWorkflow` when the org setting `analysis.retranscribe` is true (default true when `record=true`) to
produce a higher-accuracy transcript aligned to the recording for citations, evals, and the dashboard player.

> Decision (2026-10-29): `analysis.retranscribe` is `bool | null` with default null, meaning "on when the call is
> recorded"; true or false overrides that (specs/04 §7, Decision "OrgSettings fields (P02-T12a)").

- `POST {ELEVENLABS_API_BASE}/v1/speech-to-text` multipart:
  `model_id=ELEVENLABS_BATCH_STT_MODEL` (`scribe_v2`), `file=<raw.wav from S3>`, `use_multi_channel=true` (dual-channel
  recording: channel 0 caller, channel 1 agent — verified by `test_recording_channel_order`),
  `multichannel_output_style=combined`, `timestamps_granularity=word`, `tag_audio_events=false`,
  `language_code` (when locked), `keyterms` (same list, up to 1 000), `entity_detection=pii` and `pci` when redaction
  is enabled, `diarize=false`. Query `enable_logging=false` under zero retention.
- Mono recordings (none in v1 except `test_mode`) use `diarize=true`, `num_speakers=2`, `detect_speaker_roles=true`.
- Timeout 300 s; retry twice on 5xx/429 with 5 s/20 s backoff.
- Response words (sorted, `channel_index` 0 → `user`, 1 → `assistant`) are grouped into utterances (split on speaker
  change or gap > 700 ms) and stored as a transcript artifact
  `s3://{S3_BUCKET_RECORDINGS}/{org_id}/{yyyy}/{mm}/{call_id}/transcript_retranscribed.json` (specs/06 §11 Decision P05-T14) and referenced by `call_events` (category `call`,
  message `retranscription_ready`). The realtime `call_turns` remain the canonical conversation log; citations and
  evals prefer the retranscribed utterances when present because their timestamps align with the recording.

> Decision (2026-10-29): FakeElevenLabs serves this endpoint as specified in specs/30 §3.3 Decision "FakeElevenLabs
> batch STT (P03-T05b)". ElevenLabs rejects `entity_detection` together with `multichannel_output_style=combined`
> (and `combined` with `timestamps_granularity=none`), and the simulator answers 422 to match; when redaction needs
> entities, the client requests `separate` and merges the channels' words itself by start time.

> Decision (2026-11-01): the request options, response normalisation, and utterance grouping of this section are
> `va_elevenlabs.BatchOptions.for_recording`, `BatchTranscript`, and `group_utterances` (specs/15 §7 Decision
> "ElevenLabs REST client (P03-T12)" rules 5-6).

## 9. Metering

- `stt_seconds` usage event (cost only; price 0 in plans) per call = session audio duration sent (frames × 20 ms),
  cost = seconds × `ELEVENLABS_STT_COST_MICROS_PER_SECOND` (default 75 micros/s ≈ $0.0045/min, configurable).
- Batch transcription: `stt_seconds` with `reference_type='call_retranscription'`.

## 10. Simulator behaviour (`va_sim.elevenlabs.stt`)

- Implements the realtime protocol: `session_started` echoing config; partials every 200 ms of non-silent audio
  derived from a **script**: test scenarios register expected utterances keyed by audio fixture id (the sim caller
  embeds a 40 ms in-band marker tone sequence encoding the fixture id at utterance start) or, for generated audio,
  by a side-channel Redis key `sim:stt:{session}` containing the queued transcript texts in order.
- On `commit`, returns `committed_transcript_with_timestamps` with synthetic word timings spread across the audio.
- Configurable latency (`SIM_STT_COMMIT_LATENCY_MS`, default 60) and fault injection (`error`, `session_time_limit_exceeded`,
  disconnect after N seconds) via sim control API `POST /sim/elevenlabs/faults`.

> Decision (2026-10-29): specs/30 §3.3 (Decision P03-T05a) governs the simulator: partials every 300 ms of voiced
> audio (not 200 ms), a 180 ms FSK marker preamble (specs/30 §3.1) instead of a 40 ms tone sequence, the transcript
> script at `/sim/elevenlabs/stt/transcripts` instead of the Redis key `sim:stt:{session}`, and faults at
> `PUT /sim/elevenlabs/stt/fault` (every §4 error type plus `disconnect`) instead of `POST /sim/elevenlabs/faults`.

## 11. Tests required

- Query string construction for every option combination (snapshot).
- Message batching (100 ms), commit flush with empty buffer, commit throttling window, previous_text only first.
- Handling of every server `message_type` row in §4 (fake server).
- Language map: every row; unknown code rejection; detection locking logic.
- Keyterm parsing/validation/dedupe/cap and glossary injection.
- Rotation without losing partials (fake server with forced limit) and reactive reconnect with buffered audio replay.
- Batch request multipart fields and utterance grouping from a fixture response.
- Live smoke: `test_scribe_realtime_ulaw` (fixture μ-law WAV → expected text WER ≤ 15 %), keyterm encoding test.

## 12. Acceptance criteria

1. `stt_final_ms` p50 ≤ 150 ms against live Scribe in the live smoke suite; ≤ sim latency + 10 ms in simulator runs.
2. No audio is lost across rotations/reconnects (sim test compares transcript text).
3. All error types handled with the specified action and event.
