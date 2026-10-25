# 09 — Text-to-Speech with ElevenLabs

Package: `packages/va_elevenlabs/src/va_elevenlabs/tts_ws.py` (multi-context streaming), `tts_rest.py` (REST
synthesis and phrase cache), text utilities in `packages/va_voice/src/va_voice/text/` (`chunker.py`,
`pronunciation.py`, `alignment.py`). Standalone TTS product endpoints in `apps/api/src/va_api/routers/tts.py` and
`apps/media_worker/src/va_media/tts_ws_product.py`.

## 1. Provider interface

```python
class TtsVoice(BaseModel):
    provider_voice_id: str
    model_id: str                         # ELEVENLABS_TTS_MODEL (eleven_flash_v2_5)
    settings: VoiceSettings               # stability, similarity_boost, style, use_speaker_boost, speed
    language_code: str | None

class TtsAudio(BaseModel):
    context_id: str
    audio: bytes                          # decoded, in session output format
    alignment: ChunkAlignment | None      # chars, char_start_ms, char_duration_ms (relative to this chunk)
    is_final: bool = False

class StreamingTts(Protocol):
    async def open(self, output_format: Literal["ulaw_8000","pcm_16000"], voice: TtsVoice) -> None: ...
    async def start_context(self, context_id: str, voice: TtsVoice | None = None) -> None: ...
    async def send(self, context_id: str, text: str, *, flush: bool = False) -> None: ...
    async def flush(self, context_id: str) -> None: ...
    async def cancel(self, context_id: str) -> None: ...
    def audio(self) -> AsyncIterator[TtsAudio]: ...
    async def close(self) -> None: ...
```

## 2. Connection

- URL: `{ELEVENLABS_WS_BASE}/v1/text-to-speech/{provider_voice_id}/multi-stream-input?{query}` (EU orgs use the EU
  residency host).
- Header `xi-api-key: {ELEVENLABS_API_KEY}`.
- Query parameters:

| Param | Value |
|---|---|
| `model_id` | `ELEVENLABS_TTS_MODEL` = `eleven_flash_v2_5` |
| `output_format` | `ulaw_8000` (PSTN default) or `pcm_16000` (hd_audio, widget) |
| `sync_alignment` | `true` |
| `auto_mode` | `false` (we control chunking and flushing) |
| `inactivity_timeout` | `180` |
| `apply_text_normalization` | `auto` |
| `language_code` | TTS language from `08` §5 (omitted for `en`-only voices when unset) |
| `enable_ssml_parsing` | `false` |
| `enable_logging` | `false` under org zero retention, else omitted |

- **One socket per call leg**, opened at attach and kept warm for the whole call. The voice is bound to the socket
  URL; a voice change mid-call (pathway node voice override) opens a second socket for the new voice (max 2 per leg;
  least recently used is closed).
- Keepalive: at open, initialise an idle context `ka` with `{"text": " ", "context_id": "ka"}`; every 15 s while no
  other context is active send `{"text": "", "context_id": "ka"}`.

## 3. Messages (client → server), exact shapes

Initialise context (first message of each utterance):

```json
{"text": " ", "context_id": "t7-0",
 "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": true, "speed": 1.0},
 "generation_config": {"chunk_length_schedule": [50, 90, 120, 150]}}
```

Send text (every clause from the chunker; text always ends with exactly one space):

```json
{"text": "Thanks for calling Acme Dental. ", "context_id": "t7-0", "flush": true}
```

Flush (end of utterance when the last clause was sent without flush):

```json
{"context_id": "t7-0", "flush": true}
```

Close context (barge-in or done):

```json
{"context_id": "t7-0", "close_context": true}
```

Keepalive: `{"text": "", "context_id": "ka"}`. Close socket at call end: `{"close_socket": true}`.

Server → client: `{"audio": "<base64>", "contextId": "t7-0", "alignment": {...} | null, "normalizedAlignment": {...} | null}`
and `{"isFinal": true, "contextId": "t7-0"}`.

Context naming: `t{turn_index}-{n}` where `n` increments for multiple utterances within one turn (e.g. tool speech
then answer). After `isFinal`, the context is considered closed (we also send `close_context` to free server slots).
At most 3 contexts open per socket (`ka` + current + one draining).

### 3.1 `chunk_length_schedule` decision

`[50, 90, 120, 150]`. Rationale: the minimum allowed value (50) lets the server start synthesizing a short opening
clause without waiting for the 120-character default, cutting first-audio latency; because our chunker flushes at
clause boundaries (§5), the schedule mainly governs long clauses, where 90–150 keeps prosody acceptable. A/B
benchmark in the live suite (`test_tts_schedule_latency`) records `tts_ttfa_ms` for `[50,90,120,150]` vs default and
must show the chosen schedule ≤ default p50.

## 4. Voice settings resolution

`voices.settings` → per-call `voice_settings` overrides → node-level `voiceSettings` overrides (pathway node) →
clamp: stability/similarity/style to [0, 1], speed to [0.7, 1.2]. Sent on context initialisation only.

## 5. Clause chunker (`va_voice.text.chunker.ClauseChunker`)

Input: async stream of LLM text deltas. Output: clauses to send. State: `buf: str`, `emitted_count: int`.

Algorithm on each delta:
1. Append delta to `buf`; strip markdown artefacts on the fly: remove `*`, `_` when used as emphasis pairs, leading
   `#`, leading list markers (`- `, `* `, `1. ` at line start), backticks; replace newlines with a space; collapse
   whitespace.
2. Find the earliest boundary index `i` in `buf` such that:
   - **Sentence boundary**: char ∈ `.!?` followed by whitespace or end-of-stream, and not an abbreviation (token
     before the period, case-insensitive, in `{mr, mrs, ms, dr, st, jr, sr, inc, ltd, co, corp, vs, etc, e.g, i.e,
     a.m, p.m, u.s, no, ave, blvd, dept, approx, est, mt, ft, apt}`), and not a decimal/number (digit on both sides),
     and not an ellipsis inside a word; minimum words before boundary: 2 for the first clause, 3 otherwise.
   - **Clause boundary**: char ∈ `,;:` or the dash `—`/` - `, not between digits (`1,000`), with ≥ 4 words before it
     for the first clause (fast first audio) and ≥ 7 words otherwise.
   - **Length cap**: `len(buf) ≥ 160` → boundary at the last whitespace before 160.
3. If found: clause = `buf[:i+1].strip()`, `buf = buf[i+1:]`, emit clause (+ trailing space) with `flush=true`.
   Repeat step 2.
4. On end of stream: emit remaining `buf` (if non-empty) with `flush=true`.
5. Never emit a clause that is only punctuation/whitespace.

Property tests (hypothesis): concatenation of emitted clauses equals normalized input; no clause > 200 chars; numbers
`3.14`, `$1,250.50`, `10:30` never split.

## 6. Pronunciation guide (`va_voice.text.pronunciation.PronunciationApplier`)

Applied to each clause before sending (and to static/cached texts before synthesis).

For each rule `{word, pronunciation, case_sensitive, spaced}` in order (longest `word` first to avoid partial
overlaps):
1. Pattern: `(?<![\w])` + `re.escape(word)` + `(?![\w])`; flags `0` if `case_sensitive` else `re.IGNORECASE`.
2. Replacement: `pronunciation`; if `spaced=true`, the replacement is the pronunciation's characters joined by single
   spaces (used for spelling out codes, e.g. `"ABC123"` → `"A B C 1 2 3"`), and when `pronunciation` is empty the
   matched word itself is spaced.
3. Replacements are not re-matched by later rules (apply via a single combined regex with alternation and a lookup
   dict).
The original (unreplaced) clause text is what is stored in the transcript; alignment maps back via an offset map
produced by the applier (`OffsetMap.to_original(index)`), so truncation on barge-in uses original text positions.

## 7. Alignment timeline (`va_voice.text.alignment.AlignmentTimeline`)

Per utterance:
- `offset_ms` starts at 0. For each audio chunk of the context: `chunk_ms = len(audio)/8` (μ-law 8 kHz) or
  `len(audio)/32` (PCM16 16 kHz).
- If `alignment` present: for each char k: `abs_start = offset_ms + charStartTimesMs[k]`,
  `abs_end = abs_start + charDurationsMs[k]`; append to timeline (char, abs_start, abs_end) using `alignment` (not
  `normalizedAlignment`) because it refers to the text we sent.
- `offset_ms += chunk_ms`.
- Sent text characters are matched to alignment chars sequentially (whitespace-insensitive) to build
  `sent_index → time`; through the pronunciation `OffsetMap` this yields `original_index → time`.
- `text_until(played_ms)`: largest original-text prefix ending at a word boundary whose last char `abs_end ≤ played_ms`.
- If alignment is missing for a chunk, chars of the corresponding clause are linearly interpolated across the
  chunk duration.

## 8. Cancellation

`cancel(context_id)`: send `close_context`; mark context `cancelled` locally; any subsequent `audio` with that
`contextId` is dropped (counted `tts_late_audio_dropped_total`). The outbound clock queue is cleared by the caller
(`07` §7). Cancellation must not close the socket.

## 9. Phrase cache (REST)

Used for: `first_sentence` (when it contains no per-call variables after templating — or per call during prefetch),
fillers/backchannels ("Sure.", "Got it.", "One moment.", "Let me check that for you."), tool `speech`, voicemail
`error_phrase`, reprompts, `max_duration_message`, transfer lines.

- Request: `POST {ELEVENLABS_API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps?output_format=ulaw_8000`
  (or `pcm_16000`) body `{"text": text, "model_id": model, "voice_settings": settings, "language_code": lang?,
  "apply_text_normalization": "auto"}`; response `{"audio_base64", "alignment", "normalized_alignment"}`.
- Key: `tts:cache:{voice_id}:{sha256(model|format|lang|settings_json_sorted|text)}`; value JSON
  `{"audio_b64","alignment","duration_ms"}`; TTL 30 days; max value 2 MB.
- Prefetch (`prefetch_call_context`): renders templated `first_sentence` per call (TTL 1 h for per-call entries) and
  ensures the voice's standard filler set is cached (shared across calls).
- Playback of cached audio goes through the same outbound clock and alignment timeline (absolute alignment already).
- Fallback: on REST failure, the phrase is synthesized through the live socket instead.
- The live smoke test `test_tts_with_timestamps_endpoint` asserts the response shape.

## 10. Standalone TTS product endpoints

| Endpoint | Auth | Request | Response |
|---|---|---|---|
| `POST /v1/speak` | API key, scope `tts:write` | `{"text": ≤ 5 000 chars, "voice": name|uuid, "output_format": "mp3_44100_128"|"wav_16000"|"pcm_16000"|"ulaw_8000" (default mp3_44100_128), "voice_settings"?, "language"?, "pronunciation_guide"?}` | audio bytes with `Content-Type` (`audio/mpeg`, `audio/wav`, `audio/L16;rate=16000`, `audio/basic`) and header `X-Characters-Billed` |
| `POST /v1/audio/speech` (OpenAI-compatible) | API key | `{"model": "tts-1"|"tts-1-hd"|"eleven_flash_v2_5"|"eleven_multilingual_v2", "input": ≤ 4 096 chars, "voice": name|uuid, "response_format": "mp3"|"wav"|"pcm"|"opus" (default mp3), "speed": 0.7–1.2}` | audio bytes; `tts-1` → flash v2.5, `tts-1-hd` → multilingual v2; formats map to `mp3_44100_128`, `wav_24000`, `pcm_24000`, `opus_48000_64` |
| `WSS /v2/tts/ws` | `?api_key=` or `Authorization` header on upgrade | client messages: `{"type":"config","voice","output_format","voice_settings"?,"language"?}` (first), `{"type":"text","text"}` (LLM tokens, chunked server-side by §5), `{"type":"flush"}`, `{"type":"cancel"}` (replace on interruption: drops pending audio and starts a new context), `{"type":"close"}` | `{"type":"audio","audio":b64,"alignment":{...}}`, `{"type":"done"}` after flush completes, `{"type":"error","code","message"}` |

The WS product endpoint is served by `media-worker` (path `/v2/tts/ws` on `MEDIA_PUBLIC_HOST`) and keeps one upstream
ElevenLabs socket per client session ("warm session across turns").

Billing: `tts_characters` usage event per request/flush with quantity = characters sent upstream after pronunciation
replacement; unit price from plan (see shared-contract addition `plans.tts_rate_micros_per_1k_chars`). Calls do not
bill TTS characters separately (included in per-minute), but cost (`cost_micros`) is recorded with
`ELEVENLABS_TTS_COST_MICROS_PER_1K_CHARS` (default 50 000 = $0.05/1k).

## 11. Error handling and reconnection

- Socket close / error while a context is active: reconnect (50/100/200 ms), re-initialise the context with the same
  `context_id` suffixed `-r{n}`, resend the clauses not yet confirmed by audio (track per clause: sent, first audio
  received). Clauses that already produced audio are not resent; the timeline continues.
- Server error frames (JSON with `error`/`message` fields, e.g. quota, invalid voice) → `call.error` with code
  `tts_error`; invalid voice → switch to the org default voice for the rest of the call and emit warning.
- No audio for a flushed context within 3 000 ms → treat as failure → reconnect path.
- After 3 failed reconnects → error path (`07` §19).

## 12. Metrics

`tts_ttfa_ms` (first text sent → first audio chunk per context), `tts_chars_total`, `tts_reconnects_total`,
`tts_late_audio_dropped_total`, cache hit ratio `tts_cache_hits_total / tts_cache_requests_total`.

## 13. Simulator behaviour (`va_sim.elevenlabs.tts`)

Implements the multi-context protocol: for each flushed clause returns audio = a deterministic tone pattern (440 Hz
± hash of text) of duration `len(text) × 55 ms`, split into 100 ms chunks, each with alignment arrays; configurable
first-audio latency `SIM_TTS_TTFA_MS` (default 80); honors `close_context` (stops emitting); fault injection
(disconnect mid-utterance, error frame, delayed audio). REST `with-timestamps` returns the same synthesis in one body.

## 14. Tests required

- Message shapes (snapshots), context lifecycle, keepalive cadence (fake clock), max contexts.
- Chunker unit + property tests (§5); pronunciation rules incl. overlap, case sensitivity, spaced, offset mapping.
- Alignment timeline: multi-chunk absolute timing, missing alignment interpolation, `text_until` at word boundaries.
- Cancellation drops late audio; reconnection resends only unconfirmed clauses.
- Phrase cache key stability, TTL, fallback to socket.
- Product endpoints: formats, OpenAI mapping, WS protocol incl. cancel/replace, billing events.
- Live smoke: `test_tts_ws_ulaw` (non-empty μ-law audio, alignment present), `test_tts_with_timestamps_endpoint`,
  `test_tts_schedule_latency`.

## 15. Acceptance criteria

1. `tts_ttfa_ms` p50 ≤ 250 ms with live ElevenLabs Flash v2.5 from us-east (live suite).
2. Barge-in truncation stores `text_spoken` within ±1 word of the audio actually played (sim test with known timings).
3. Phrase cache hit rate ≥ 90 % for fillers in load tests.
