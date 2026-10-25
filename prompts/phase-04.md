## Phase 04 — Real-Time Media Pipeline Core (`packages/va_voice`)

### Goal
Build the transport-agnostic real-time voice pipeline primitives and the turn loop that later phases plug into a
telephony transport (phase 05) and a browser transport (phase 14): audio codecs and framing, reorder buffer, Silero VAD,
adaptive endpointing, interruptibility and backchannel handling, LLM-token→TTS clause chunking, pronunciation guide
application, playback tracking with alignment-accurate truncation, outbound pacing with background mixing, phrase cache,
latency tracing, and a pipeline orchestrator with protocol interfaces (Transport, SttStream, TtsStream, Brain) verified
by an in-memory harness with fake clocks.

### Read first
- `specs/07-media-pipeline.md` — entire file; primary contract.
- `specs/08-stt-elevenlabs.md` and `specs/09-tts-elevenlabs.md` — how STT/TTS sessions are driven (commit strategy,
  contexts, alignment, cancellation).
- `specs/10-llm-gateway.md` — streaming deltas consumed by the chunker; speculative generation contract.
- `specs/00-overview.md` §5 — latency and barge-in targets.

### In scope
All `va_voice` modules described in specs/07 plus their unit tests and a deterministic pipeline harness:
`audio` (μ-law⇄PCM tables, L16 big-endian, soxr resampling, frame utilities), `reorder`, `vad`, `endpointing`,
`interrupt` (levels 0–4, backchannel list, `block_interruptions`), `chunker`, `pronunciation`, `playback`
(alignment timeline, mark correlation, truncation), `outbound` (pacer, background mixer, gain), `phrase_cache`,
`latency` (spans and turn metrics), `pipeline` (CallPipeline orchestrator, protocols, cancellation semantics, silence
handling, DTMF input routing).

### Phase-specific constraints
- No asyncio task may block > 2 ms of CPU per 20 ms frame on the reference machine; VAD inference runs in a dedicated
  thread pool sized from settings, with results awaited asynchronously.
- All time-dependent logic takes an injected `Clock`/monotonic source so tests are deterministic.
- Barge-in sequence ordering (detect → transport.clear → cancel TTS context → cancel LLM stream → truncate history) is
  asserted by tests that record call order and timestamps; reaction ≤ 120 ms in harness time.

### Phase exit gate
`make check` passes; harness benchmark test (`pytest -m bench packages/va_voice`) reports platform overhead per turn
≤ 30 ms p50 with zero-latency fakes and records the numbers in `packages/va_voice/BENCHMARKS.md`.
