# Phase 04 — Real-Time Media Pipeline Core

- [x] P04-T01 Audio codecs, framing, resampling — va_voice.audio codec/formats/framing/resample/levels; 71 tests, 100 % coverage, bit-identical with sim codec; make test 6589 passed / 7 skipped, lint/typecheck/placeholders pass, jscpd 0 clones
  - Spec refs: specs/07-media-pipeline.md (audio framing, codecs, hd_audio L16 mode)
  - Depends on: P00-T05
  - Deliverables: `va_voice.audio` (μ-law encode/decode via numpy lookup tables, PCM16 helpers, L16 big-endian conversions, soxr resample 8k↔16k, frame splitter/joiner, RMS/dBFS)
  - Acceptance: round-trip error bounds; golden vectors for μ-law (ITU-T G.711 table spot checks); resample length correctness.

- [x] P04-T02 Reorder buffer — va_voice.reorder.ReorderBuffer (count-based window 5, silence fill, resync > 50, drain) + CatalogReorderMetrics, va_audio_gap_frames_total in specs/27 §3.3 + catalog; 19 tests incl. 3 hypothesis properties, va_voice 100 % coverage; make test 6610 passed / 7 skipped, lint/typecheck/placeholders/jscpd clean
  - Spec refs: specs/07-media-pipeline.md (reorder buffer); specs/06-telephony-telnyx.md (media `chunk` ordering)
  - Depends on: P04-T01
  - Deliverables: `ReorderBuffer(window_frames=5)` emitting in-order frames, gap fill with silence after timeout, duplicate drop, metrics counters
  - Acceptance: hypothesis tests over random permutations within window.

- [x] P04-T03 Silero VAD wrapper — va_voice.vad_model.SileroModel (manifest-verified ORT session) + va_voice.vad.SileroVad/VadDecider/VadExecutor (MEDIA_VAD_THREADS); fixture WAVs speech/silence/noise/DTMF; test_synth_detected_by_silero 20 utterances ≥ 90 %; make test 6653 passed / 7 skipped (98 %), vad*.py 100 %, lint/typecheck/placeholders pass, jscpd 0
  - Spec refs: specs/07-media-pipeline.md (Silero VAD)
  - Depends on: P04-T01, P00-T10
  - Deliverables: `SileroVad` (onnxruntime session from `models/silero_vad.onnx`, state tensors, 512-sample windows at 16 kHz, thresholds, thread-pool execution), `VadEvent` stream (speech_start, speech_end, probability)
  - Acceptance: fixture WAVs (speech/silence generated via TTS fixture files committed under tests/fixtures) produce expected segments; manifest verification on load; `test_synth_detected_by_silero` (specs/30 §3.1): the production `SileroVad` with production thresholds classifies ≥ 90 % of 32 ms windows of the voiced body of 20 random `va_sim.audio.synthesize_utterance` utterances as speech (deferred here from P03-T03a).

- [x] P04-T04 Adaptive endpointer — va_voice.endpointing.Endpointer (pure ms timeline, adaptive p90, completeness multipliers, ivr floor, fixed threshold, late-transcript/discard cap) + va_voice.text_norm; specs/07 §6 Decision; make test 6759 passed / 7 skipped (98.47 %, new modules 100 %); lint, typecheck, placeholders pass
  - Spec refs: specs/07-media-pipeline.md (endpointing algorithm, resumption_speed)
  - Depends on: P04-T03
  - Deliverables: `Endpointer` combining VAD silence, partial-transcript completeness heuristic, adaptive per-caller timeout, per-node resumption speed, max-wait cap; emits `EndOfTurn(at, reason)`
  - Acceptance: table-driven tests for every rule and the adaptation algorithm with fake clock.

- [x] P04-T05 Interruptibility policy and backchannels — va_voice.interrupt (InterruptPolicy, BackchannelClassifier, effective_level, DTMF rule), 134 tests incl. spec-parsed backchannel set + level matrix; make test 6893 passed / 7 skipped (98.48 %, interrupt.py 100 %), lint/typecheck/placeholders clean
  - Spec refs: specs/07-media-pipeline.md (interruptibility levels 0–4, backchannel list, block_interruptions)
  - Depends on: P04-T03
  - Deliverables: `InterruptPolicy.should_interrupt(speech_ms, words, text, level, agent_speaking)` and `BackchannelClassifier`
  - Acceptance: matrix test covering levels × inputs exactly as specified.

- [x] P04-T06 Clause chunker for streaming TTS — va_voice.text.chunker ClauseChunker + markdown normalize_text/MarkdownStripper; golden + hypothesis (concat, ≤160, numbers, split invariance); make test 7030 passed / 7 skipped (98.49 %, text/ 100 %); lint/typecheck/placeholders clean
  - Spec refs: specs/09-tts-elevenlabs.md (token → clause chunker algorithm)
  - Depends on: P00-T05
  - Deliverables: `ClauseChunker.feed(delta) -> list[str]`, `flush()`, abbreviation and number handling, min words / max chars rules
  - Acceptance: golden tests from the spec examples plus hypothesis test that concatenated chunks equal input.

- [x] P04-T07 Pronunciation guide application — PronunciationApplier (one combined regex, longest-first, scoped (?i:), spaced spell_out) + OffsetMap floor/ceil; 31 tests incl. hypothesis naive-reference equivalence; make test 7061 passed / 7 skipped (98.5 %, pronunciation.py 100 %); lint/typecheck/placeholders/spec refs pass
  - Spec refs: specs/09-tts-elevenlabs.md (pronunciation_guide application)
  - Depends on: P00-T05
  - Deliverables: `PronunciationApplier` (case_sensitive, spaced, word-boundary regex, precompiled per call)
  - Acceptance: tests for each flag and overlapping rules ordering.

- [x] P04-T08 Playback tracker and truncation — va_voice.playback (PlaybackTracker, marks, truncate_assistant_message) + text.alignment (AlignmentTimeline, gap resync); make test 7121 passed / 7 skipped, 98.51 %, new modules 100 %; lint/typecheck/placeholders pass
  - Spec refs: specs/07-media-pipeline.md (playback tracker, barge-in truncation); specs/09-tts-elevenlabs.md (alignment handling)
  - Depends on: P04-T01
  - Deliverables: `PlaybackTracker` (absolute alignment timeline per context, frames-sent clock, Telnyx mark correlation, `played_text(at)`), `truncate_assistant_message`
  - Acceptance: tests with synthetic alignments verify truncation at word boundaries for interrupts at arbitrary times.

- [x] P04-T09 Outbound pacer and background mixer — va_voice.outbound (FramePacer drift-free anchor schedule, lead 3, throttle; FrameMixer unit-gain mix + μ-law passthrough + L16 byte order; OutboundAudio marks after last frame, clear with generation guard), va_voice.background (BackgroundLibrary per-rate assets, levels ±1 dB), va_voice.noise SpectralGate (−11.6 dB, gated Silero recall 100 %); specs/07 §9/§10.1/§10.2 Decisions; make test 7199 passed / 7 skipped (98.54 %, new modules 100 %), lint/typecheck/placeholders/spec refs 0, jscpd 0, radon A
  - Spec refs: specs/07-media-pipeline.md (outbound pacing, background track mixing, noise_cancellation handling)
  - Depends on: P04-T01, P00-T10
  - Deliverables: `OutboundAudio` (20 ms pacing with lead buffer, clear(), background loop mixing at configured level, pure passthrough when background is `none`), noise handling module as specified
  - Acceptance: timing tests with fake clock; mixed output level within ±1 dB of target.

- [x] P04-T10 Latency tracer and turn metrics — va_voice.latency (TurnTimer marks, TurnLatency, TurnMetrics e2e/eot/barge-in without double counting vendor histograms, retroactive turn span tree, call.latency events); test_latency 25 tests, latency.py 100 %; make test 7224 passed / 7 skipped (98.55 %), lint/typecheck/placeholders/spec refs 0, jscpd 0 clones, radon A (2.26)
  - Spec refs: specs/07-media-pipeline.md (latency tracing spans, metric definitions); specs/27-observability.md (per-turn spans)
  - Depends on: P00-T08
  - Deliverables: `TurnTimer` marks (user_speech_end, eot, stt_final, llm_first_token, tts_first_audio, first_audio_out), computed metrics dict, OTel span emission, Prometheus histograms
  - Acceptance: metric arithmetic tests; spans exported to an in-memory exporter in tests.

- [x] P04-T11 Phrase cache — va_voice.phrase_cache (Redis tts:cache key/value per specs/09 §9 Decision, fail-open, single flight, prefetch, hit/miss metrics); 55 tests incl. FakeElevenLabs; make test 7279 passed / 7 skipped, phrase_cache 100 %, lint/typecheck/placeholders/spec refs 0, jscpd 0, radon A (2.41)
  - Spec refs: specs/09-tts-elevenlabs.md (phrase cache); specs/03-data-model.md (Redis keyspace `tts:cache:*`)
  - Depends on: P03-T12
  - Deliverables: `PhraseCache.get_or_render(voice, text, settings, format)` using REST convert, Redis storage, size limits
  - Acceptance: cache hit/miss tests against FakeElevenLabs; key includes voice settings hash.

- [x] P04-T12a Pipeline core: protocols, harness, normal turn — va_voice.pipeline (protocols, models, config, inbound, speaker, pipeline, waits) + AcceleratedClock harness; 60 pipeline tests; make test 7341 passed / 7 skipped (98.58 %, pipeline 99-100 %), lint/typecheck/placeholders/spec refs 0, jscpd 0, radon avg A
  - Spec refs: specs/07-media-pipeline.md §1, §5.2, §6, §8, §9, §11 (opener), §16, §18 (`duration_watchdog`, end routine), §20
  - Depends on: P04-T02, P04-T04, P04-T06, P04-T07, P04-T08, P04-T09, P04-T10
  - Deliverables: `va_voice.pipeline` subpackage: `Transport`, `SttStream`, `TtsStream`, `Brain`, `VoiceActivityDetector` protocols; Brain outputs (`SpeakText`, `SpeakStatic`, `EndCall`, passthrough actions); `CallPipeline.run()` (inbound reorder + fan-out to VAD/STT, endpointing → STT commit → Brain turn → chunker → pronunciation → TTS → outbound with marks and playback completion; opener; max-duration warning and end; latency tracer per turn; cancellation and cleanup returning a `PipelineResult`); deterministic in-memory harness (`tests/support/pipeline_harness.py`) with fake transport/STT/TTS/VAD/Brain on the sim `AcceleratedClock`
  - Acceptance: harness tests: opener spoken, normal turn (user speech → EOT → committed text to Brain → reply audio paced out with marks, turn record + `call.latency` events with measured e2e), max-duration warning then end, `EndCall` final line then end, transport close ends the pipeline and cancels every task, a crashing component ends with `error`.

- [x] P04-T12b Pipeline barge-in and interruption policy — barge-in sequence clear→tts.cancel→llm.cancel→brain.interrupted (≤120 ms), backchannel/held/merge routing, false-barge-in resume, actions shielded from reply cancel; 7375 passed, cov 98.55 %, lint/typecheck/placeholders pass
  - Spec refs: specs/07-media-pipeline.md §7 (levels, barge-in sequence, false barge-in), §8 (concurrency rule), §9 (truncation)
  - Depends on: P04-T05, P04-T12a
  - Deliverables: barge-in in `CallPipeline` (InterruptPolicy on live VAD/partials while playing; transport.clear → outbound clear → TTS cancel → Brain stream cancel → truncate history; `Brain.on_interrupted`; interrupt events with reaction_ms; pre-audio new user turn cancels generation and concatenates text; false barge-in resume "Sorry — " once per turn)
  - Acceptance: in-memory harness tests: barge-in ordering recorded with timestamps and ≤ 120 ms reaction, backchannel ignored at level 2, interrupt blocked at level 0 (speech appended to next turn), level 4 any speech, non-interruptible SpeakStatic, truncated history ends with "—", pre-audio concat, false barge-in resume.

- [ ] P04-T12c Pipeline silence policy and DTMF input
  - Spec refs: specs/07-media-pipeline.md §12, §13
  - Depends on: P04-T12b
  - Deliverables: no-input timer after playback completes (reprompts via `Brain.on_no_input`, goodbye + `no_input_timeout` end, reset on speech); DTMF buffering (2 000 ms / `#`), `ignore_button_press`, DTMF user turn `"[DTMF: 1 2 3 4]"` with `dtmf`, DTMF barge-in at levels ≥ 1
  - Acceptance: in-memory harness tests: silence reprompt then hangup, speech resets the reprompt count, DTMF delivered to Brain (timeout and `#` terminator), ignored DTMF, DTMF interrupts playback at level 1 but not 0.

- [ ] P04-T13 Pipeline benchmark
  - Spec refs: specs/07-media-pipeline.md (performance requirements, CPU budget)
  - Depends on: P04-T12c
  - Deliverables: `packages/va_voice/tests/bench/test_pipeline_bench.py` (marker `bench`), `packages/va_voice/BENCHMARKS.md` generated from results
  - Acceptance: p50 platform overhead ≤ 30 ms per turn with zero-latency fakes; 50 concurrent harness pipelines on one event loop without frame deadline misses.

## Phase exit gate
`make check` passes; `uv run pytest -m bench packages/va_voice` meets thresholds.
