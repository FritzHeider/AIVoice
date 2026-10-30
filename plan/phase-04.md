# Phase 04 — Real-Time Media Pipeline Core

- [ ] P04-T01 Audio codecs, framing, resampling
  - Spec refs: specs/07-media-pipeline.md (audio framing, codecs, hd_audio L16 mode)
  - Depends on: P00-T05
  - Deliverables: `va_voice.audio` (μ-law encode/decode via numpy lookup tables, PCM16 helpers, L16 big-endian conversions, soxr resample 8k↔16k, frame splitter/joiner, RMS/dBFS)
  - Acceptance: round-trip error bounds; golden vectors for μ-law (ITU-T G.711 table spot checks); resample length correctness.

- [ ] P04-T02 Reorder buffer
  - Spec refs: specs/07-media-pipeline.md (reorder buffer); specs/06-telephony-telnyx.md (media `chunk` ordering)
  - Depends on: P04-T01
  - Deliverables: `ReorderBuffer(window_frames=5)` emitting in-order frames, gap fill with silence after timeout, duplicate drop, metrics counters
  - Acceptance: hypothesis tests over random permutations within window.

- [ ] P04-T03 Silero VAD wrapper
  - Spec refs: specs/07-media-pipeline.md (Silero VAD)
  - Depends on: P04-T01, P00-T10
  - Deliverables: `SileroVad` (onnxruntime session from `models/silero_vad.onnx`, state tensors, 512-sample windows at 16 kHz, thresholds, thread-pool execution), `VadEvent` stream (speech_start, speech_end, probability)
  - Acceptance: fixture WAVs (speech/silence generated via TTS fixture files committed under tests/fixtures) produce expected segments; manifest verification on load; `test_synth_detected_by_silero` (specs/30 §3.1): the production `SileroVad` with production thresholds classifies ≥ 90 % of 32 ms windows of the voiced body of 20 random `va_sim.audio.synthesize_utterance` utterances as speech (deferred here from P03-T03a).

- [ ] P04-T04 Adaptive endpointer
  - Spec refs: specs/07-media-pipeline.md (endpointing algorithm, resumption_speed)
  - Depends on: P04-T03
  - Deliverables: `Endpointer` combining VAD silence, partial-transcript completeness heuristic, adaptive per-caller timeout, per-node resumption speed, max-wait cap; emits `EndOfTurn(at, reason)`
  - Acceptance: table-driven tests for every rule and the adaptation algorithm with fake clock.

- [ ] P04-T05 Interruptibility policy and backchannels
  - Spec refs: specs/07-media-pipeline.md (interruptibility levels 0–4, backchannel list, block_interruptions)
  - Depends on: P04-T03
  - Deliverables: `InterruptPolicy.should_interrupt(speech_ms, words, text, level, agent_speaking)` and `BackchannelClassifier`
  - Acceptance: matrix test covering levels × inputs exactly as specified.

- [ ] P04-T06 Clause chunker for streaming TTS
  - Spec refs: specs/09-tts-elevenlabs.md (token → clause chunker algorithm)
  - Depends on: P00-T05
  - Deliverables: `ClauseChunker.feed(delta) -> list[str]`, `flush()`, abbreviation and number handling, min words / max chars rules
  - Acceptance: golden tests from the spec examples plus hypothesis test that concatenated chunks equal input.

- [ ] P04-T07 Pronunciation guide application
  - Spec refs: specs/09-tts-elevenlabs.md (pronunciation_guide application)
  - Depends on: P00-T05
  - Deliverables: `PronunciationApplier` (case_sensitive, spaced, word-boundary regex, precompiled per call)
  - Acceptance: tests for each flag and overlapping rules ordering.

- [ ] P04-T08 Playback tracker and truncation
  - Spec refs: specs/07-media-pipeline.md (playback tracker, barge-in truncation); specs/09-tts-elevenlabs.md (alignment handling)
  - Depends on: P04-T01
  - Deliverables: `PlaybackTracker` (absolute alignment timeline per context, frames-sent clock, Telnyx mark correlation, `played_text(at)`), `truncate_assistant_message`
  - Acceptance: tests with synthetic alignments verify truncation at word boundaries for interrupts at arbitrary times.

- [ ] P04-T09 Outbound pacer and background mixer
  - Spec refs: specs/07-media-pipeline.md (outbound pacing, background track mixing, noise_cancellation handling)
  - Depends on: P04-T01, P00-T10
  - Deliverables: `OutboundAudio` (20 ms pacing with lead buffer, clear(), background loop mixing at configured level, pure passthrough when background is `none`), noise handling module as specified
  - Acceptance: timing tests with fake clock; mixed output level within ±1 dB of target.

- [ ] P04-T10 Latency tracer and turn metrics
  - Spec refs: specs/07-media-pipeline.md (latency tracing spans, metric definitions); specs/27-observability.md (per-turn spans)
  - Depends on: P00-T08
  - Deliverables: `TurnTimer` marks (user_speech_end, eot, stt_final, llm_first_token, tts_first_audio, first_audio_out), computed metrics dict, OTel span emission, Prometheus histograms
  - Acceptance: metric arithmetic tests; spans exported to an in-memory exporter in tests.

- [ ] P04-T11 Phrase cache
  - Spec refs: specs/09-tts-elevenlabs.md (phrase cache); specs/03-data-model.md (Redis keyspace `tts:cache:*`)
  - Depends on: P03-T12
  - Deliverables: `PhraseCache.get_or_render(voice, text, settings, format)` using REST convert, Redis storage, size limits
  - Acceptance: cache hit/miss tests against FakeElevenLabs; key includes voice settings hash.

- [ ] P04-T12 Pipeline orchestrator and protocols
  - Spec refs: specs/07-media-pipeline.md (CallSession task group, protocols, barge-in sequence, silence/no-input handling, DTMF input, max_duration)
  - Depends on: P04-T02, P04-T04, P04-T05, P04-T06, P04-T07, P04-T08, P04-T09, P04-T10
  - Deliverables: `Transport`, `SttStream`, `TtsStream`, `Brain` protocols; `CallPipeline.run()` (inbound audio fan-out to VAD/STT, endpointing → Brain turn → chunker → TTS → outbound; barge-in; reprompt/hangup on silence; DTMF routing; max-duration timer; cancellation and cleanup)
  - Acceptance: in-memory harness tests: normal turn, barge-in ordering and ≤ 120 ms reaction, backchannel ignored at level 2, interrupt blocked at level 0, silence reprompt then hangup, DTMF delivered to Brain.

- [ ] P04-T13 Pipeline benchmark
  - Spec refs: specs/07-media-pipeline.md (performance requirements, CPU budget)
  - Depends on: P04-T12
  - Deliverables: `packages/va_voice/tests/bench/test_pipeline_bench.py` (marker `bench`), `packages/va_voice/BENCHMARKS.md` generated from results
  - Acceptance: p50 platform overhead ≤ 30 ms per turn with zero-latency fakes; 50 concurrent harness pipelines on one event loop without frame deadline misses.

## Phase exit gate
`make check` passes; `uv run pytest -m bench packages/va_voice` meets thresholds.
