# Scratchpad — Phase 04

## Plan P04-T01 (Planner, 2026-11-01)
Audio codecs, framing, resampling — specs/07 §4 (+ §15 hd_audio byte order, §5.3 VAD input format, §10.2 levels).
New package `packages/va_voice` (dist `va-voice`, import `va_voice`, first package of phase 04).

### Package scaffolding
- `packages/va_voice/pyproject.toml`: name `va-voice`, description "VoiceAgents real-time audio pipeline primitives",
  requires-python `>=3.12,<3.13`, deps `numpy>=2.3`, `soxr>=<latest>` (add with `uv add --package va-voice soxr`;
  soxr is new to uv.lock), `va-common` (workspace; later tasks need Clock/metrics) with `[tool.uv.sources]`.
  `uv_build` backend like va_domain. `src/va_voice/__init__.py` (docstring only), `py.typed`.
- Root `pyproject.toml`: add `"va-voice"` to `[dependency-groups] dev` + `va-voice = { workspace = true }`; append
  `packages/va_voice/src` to mypy `mypy_path`; `packages/va_voice/src` and `packages/va_voice/tests` to mypy `files`.
  If soxr lacks type info (check for `py.typed` in the installed wheel), add a commented `[[tool.mypy.overrides]]`
  `module = ["soxr", "soxr.*"]` `ignore_missing_imports = true` and wrap its calls in typed functions.
- `uv sync`, then `uvx pip-audit` on `uv export` must stay clean.

### Modules (`packages/va_voice/src/va_voice/audio/`, package `va_voice.audio` re-exports everything in `__all__`)
1. `codec.py` — table-first G.711 exactly as specs/07 §4 (independently written; must be bit-identical with
   `va_sim.audio.codec`, but va_voice src must NOT import va_sim):
   - `ULAW_TO_PCM16: Final[NDArray[np.int16]]` (256,), `PCM16_TO_ULAW: Final[NDArray[np.uint8]]` (65536,) indexed by
     `sample + 32768`, bias 0x84, clip 32635, inverted bits. Build at import from numpy vector ops.
   - `ULAW_SILENCE: Final = 0xFF`, `FULL_SCALE: Final = 32_767`.
   - `ulaw_decode(b: bytes) -> NDArray[np.int16]`, `ulaw_encode(x: NDArray[np.int16]) -> bytes`.
   - `l16be_to_pcm(b) -> NDArray[np.int16]` (`np.frombuffer(b, dtype='>i2').astype(np.int16)`),
     `pcm_to_l16be(x) -> bytes`; `pcm16le_to_pcm(b)` / `pcm_to_pcm16le(x)` (ElevenLabs `pcm_16000`, widget);
     `l16_to_pcm(b, byte_order: L16ByteOrder)` / `pcm_to_l16(x, byte_order)` with
     `L16ByteOrder = Literal["big", "little"]` (for `TELNYX_L16_BYTE_ORDER`, specs/07 §15).
   - `pcm_to_float32(x) -> NDArray[np.float32]` (x / 32768.0, VAD input specs/07 §5.3) and
     `float32_to_pcm(x)` (clip to [-1, 1), `np.rint(x*32768)` clipped to int16 range).
   - Odd-length L16/PCM16 input raises `ValueError("... whole samples ...")` (don't let numpy's message leak).
2. `formats.py` — `@dataclass(frozen=True, slots=True) class AudioFormat: name: str; sample_rate: int;
   sample_width: int; silence_byte: int` with properties `frame_bytes` (20 ms) and `bytes_per_ms`, method
   `duration_ms(n_bytes) -> float`. Constants `ULAW_8000` (8000, 1, 0xFF → 160 B), `L16_16000` (16000, 2, 0x00 → 640 B,
   big-endian wire), `PCM16LE_16000` (16000, 2, 0x00 → 640 B). `FRAME_MS: Final = 20`.
3. `framing.py` — `class Framer` (specs/07 §4): `__init__(self, fmt: AudioFormat)` (frame size + silence byte from
   the format); `feed(chunk: bytes) -> list[bytes]` returns zero or more exact frames (bytearray buffer, slices,
   no quadratic copying); `pending: int` (buffered bytes); `flush() -> bytes | None` pads the remainder with
   `silence_byte` to one frame (None when empty) and empties the buffer; for 2-byte formats a dangling odd byte is
   kept until the next feed (frames are always whole samples since frame_bytes is even).
   Module functions: `split_frames(data: bytes, fmt) -> tuple[list[bytes], bytes]` (frames + remainder) and
   `join_frames(frames: Iterable[bytes]) -> bytes`; `rebatch(frames, n)` not needed — Scribe batching (5 frames) is
   `join_frames` of 5 frames, done by the STT sender later.
4. `resample.py` — `class Resampler` wrapping `soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16",
   quality="LQ")` (spec line verbatim) — `__init__(self, in_rate: int = 8000, out_rate: int = 16000, *, quality:
   Literal["QQ","LQ","MQ","HQ","VHQ"] = "LQ")`; `process(x: NDArray[np.int16]) -> NDArray[np.int16]` (streaming,
   stateful, may return fewer samples early because of filter delay); `flush() -> NDArray[np.int16]`
   (`resample_chunk(empty, last=True)`; afterwards `process` raises `RuntimeError("resampler already flushed")`).
   Plus `resample(x, in_rate, out_rate) -> NDArray[np.int16]` one-shot (`soxr.resample`) for non-streaming uses.
5. `levels.py` — `rms(x: NDArray[np.int16] | NDArray[np.float32]) -> float` (int16 in sample units, computed in
   float64 to avoid overflow), `rms_dbfs(x) -> float` = `20·log10(rms/32767)` for int16 (full scale 32 767, same as
   `scripts/generate_audio_assets.py` `to_int16` and `va_sim.audio.dbfs_peak`, so the ±1 dB mixer test later uses
   one convention), returns `-math.inf` for silence/empty; `dbfs_to_amplitude(dbfs) -> float` (32767·10^(dbfs/20));
   `gain_for_db(db) -> float` (10^(db/20)); `apply_gain(x: int16, gain: float) -> int16` (float32 math, rint, clip).

### Tests (`packages/va_voice/tests/audio/`; no conftest needed)
- `test_codec.py`:
  - `test_known_vectors` parametrized: encode 0→0xFF, 32767→0x80, −32768→0x00 (spec), plus G.711 spot checks
    (−1→0x7F, 1→0xFE, 32635→0x80, 8159→0x80+? derive from formula via independent `g711_decode`); decode 0xFF→0,
    0x80→32124, 0x00→−32124, 0x7F→0.
  - `test_decode_table_matches_g711_formula` (per-code independent formula `((2m+33) << (e+2)) − 132`).
  - `test_round_trip_within_one_quantization_step_for_all_65536_inputs` (|decode(encode(x)) − min(x,±32635)| ≤
    step size of x's segment, computed independently).
  - `test_every_code_is_fixed_point_of_decode_encode` (except 0x7F→0xFF since both are zero: assert decode equality).
  - `test_encode_clips_beyond_32635`, `test_encode_is_monotonic_in_magnitude`.
  - `test_tables_are_bit_identical_with_simulator_codec` — imports `va_sim.audio.codec` tables (dev dep, allowed in
    tests) and `np.array_equal` both tables (AGENTS P03-T03a rule).
  - `test_l16_big_endian_byte_order` (`pcm_to_l16be(np.array([1, -2]))` == b"\x00\x01\xff\xfe"), LE likewise,
    `l16_to_pcm`/`pcm_to_l16` for both orders, round trip all 65 536 samples, odd length → ValueError.
  - `test_float32_conversion` (−32768 → −1.0, 0 → 0.0, round trip exact for all int16; clip of 1.5 → 32767).
  - hypothesis `test_ulaw_encode_decode_idempotent_after_first_pass` (encode(decode(encode(x))) == encode(x)).
- `test_formats.py`: frame_bytes 160/640/640, duration_ms of 800 bytes μ-law = 100 ms, of 3200 bytes L16 = 100 ms.
- `test_framing.py`: exact frames from 160-byte input; arbitrary chunk sizes (hypothesis: random partition of a
  random byte string ⇒ concatenated frames + flush remainder (unpadded prefix) == input, every frame exactly
  frame_bytes); `flush` pads μ-law with 0xFF and L16 with 0x00 and returns None when empty; `pending` tracking;
  feed after flush starts fresh; odd byte retained for 2-byte formats; `split_frames`/`join_frames` inverse.
- `test_resample.py`: streaming 8k→16k of 1 s in 20 ms chunks (160 samples): total output after `flush()` is
  within ±1 of 16 000 samples (soxr exact behaviour: verify and assert the tightest true bound); steady state each
  chunk yields ~320 samples after the warm-up; a 1 kHz tone keeps its frequency (FFT peak bin) and RMS within
  0.5 dB (skip edges); chunking invariance: output of one big chunk vs 20 ms chunks equal (or within 1 LSB) after
  flush; `process` after `flush` → RuntimeError; one-shot `resample` length `n*out/in` ±1; 16k→8k also works.
- `test_levels.py`: rms of full-scale square = 32767·… ; `rms_dbfs` of sine at amplitude A = 20log10(A/√2/32767)
  within 0.01 dB; silence → −inf; empty → −inf; `apply_gain` +6 dB doubles with clipping at int16 bounds;
  `dbfs_to_amplitude(-6.0206)` ≈ 16383.5; consistency check: `rms_dbfs` of `assets/audio/static.pcm16k` ≈ −40 dB
  (±0.5) and `office.ulaw` decoded ≈ −22 dB (±0.5) (locate repo root via `Path(__file__).parents[...]`).

### Docs
- specs/07 §4: add `> Decision (2026-11-01):` — module layout (`va_voice.audio.codec|formats|framing|resample|levels`,
  all re-exported from `va_voice.audio`), dBFS convention (full scale 32 767 RMS, −inf for silence), `Framer` keeps
  odd bytes, `Resampler.flush()`; LE helpers for ElevenLabs/widget; float32 = x/32768.
- AGENTS.md: fix the P03-T03a line `va_voice.codec` → `va_voice.audio.codec`; add a P04-T01 bullet (package
  scaffolding done, soxr typing status, dBFS convention, bit-identity test with sim).

### Gates
`make fmt`, `make lint`, `make typecheck`, `make placeholders`, `uv run pytest packages/va_voice -q --cov=packages/va_voice`
(≥ 85 %, aim 100 %), `make test`, `uv run python scripts/check_spec_refs.py`, jscpd (`npx --yes jscpd@4 --silent`) —
the va_voice codec intentionally mirrors va_sim's; write it differently enough (e.g. build the encode table via
`np.searchsorted` over segment boundaries / `np.log2`) so jscpd finds 0 clones, or add jscpd:ignore markers with reason;
radon, pip-audit (new soxr dep), pnpm audit.

### Risks
- soxr wheel availability for linux aarch64/x86_64 py3.12 (exists) and typing (override if no py.typed).
- jscpd duplicate with va_sim codec tests: write va_voice tests with different structure; reuse is forbidden by design.
- soxr streaming delay: early `process` calls return fewer samples — tests must assert totals after flush, not per chunk.

## Build P04-T01 (Builder, 2026-11-01)
Implemented as planned: packages/va_voice (dist va-voice, deps numpy, soxr 1.1.0, va-common), va_voice.audio
codec/formats/framing/resample/levels + 71 tests (100 % coverage). Codec built differently from va_sim (searchsorted
segments) and asserted bit-identical. Golden vectors merged into one (sample, code, decoded) table to avoid a jscpd clone
with va_sim tests. soxr: no py.typed -> mypy override. LQ stream emits nothing for first ~3 chunks; totals exact after
flush; chunking differs ≤2 LSB. specs/07 §4 Decision + AGENTS.md bullet added.
Gates: fmt ok, lint pass, typecheck pass (825 files), placeholders 0, spec refs 0, make test 6589 passed / 7 skipped,
jscpd 0 clones, radon A (1.325), pip-audit clean, pnpm audit 3 moderate (no high).

## Review P04-T01 (Reviewer, 2026-11-01)
Approved. Re-ran: lint pass, typecheck pass (825 files), placeholders 0, make test 6589 passed / 7 skipped (98.45 %),
va_voice 71 passed 100 % coverage, jscpd 0 clones, spec refs 0. Diff matches specs/07 §4 (tables, L16 BE, soxr LQ
stream, Framer padding) + Decision note; no findings.

## Plan P04-T02 (Planner, 2026-11-01)
Reorder buffer — specs/07 §5.1 (+ specs/06 media `chunk`, specs/27 §3.3 `va_audio_reorder_total`). Transport-agnostic:
va_voice must NOT import va_telnyx (callers pass `int(media.chunk)` + payload). Telnyx chunks start at 1 (FakeTelnyx
`MediaSession._chunk` 0 → +1 before send; jitter mode sends 2, 1, 4, 3 ...), so expected starts at 1 by default.

### Files
1. `packages/va_voice/src/va_voice/reorder.py` (pure, sync, no asyncio — called from the media read loop):
   - `@dataclass(frozen=True, slots=True) class OrderedFrame: chunk: int; payload: bytes; filled: bool`
     (`filled=True` = synthetic silence for a missing chunk).
   - `@dataclass(slots=True) class ReorderStats: received: int = 0; emitted: int = 0; reordered: int = 0;
     gap_frames: int = 0; duplicates: int = 0; resyncs: int = 0` (always kept, read by tests/pipeline).
   - `class ReorderObserver(Protocol)`: `reordered(self, count: int) -> None`, `gap_filled(self, count: int) -> None`,
     `duplicate(self, count: int) -> None`. `NULL_OBSERVER` no-op implementation (bodies `return None`).
   - `class ReorderBuffer`:
     `__init__(self, fmt: AudioFormat = ULAW_8000, *, window: int = 5, first_chunk: int = 1, max_gap: int = 50,
     observer: ReorderObserver = NULL_OBSERVER)`; `window >= 1`, `max_gap >= window` else `ValueError`.
     Silence frame = `bytes([fmt.silence_byte]) * fmt.frame_bytes` built once.
     `push(chunk: int, payload: bytes) -> list[OrderedFrame]`:
       - `chunk < expected` or already buffered → duplicate (dropped, stats + observer).
       - `chunk == expected` → emit it, then emit consecutive buffered successors.
       - `chunk > expected` → hold it (dict `chunk → payload`), stats.reordered += 1, observer.reordered(1).
       - then while `len(held) >= window` and `expected` missing: if `min(held) - expected > max_gap` → resync
         (expected = min(held), no fill, stats.resyncs += 1; protects against chunk-number jumps producing thousands of
         silence frames) else emit ONE silence `OrderedFrame(expected, silence, filled=True)`, gap_frames += 1,
         observer.gap_filled(1), expected += 1; after each step emit consecutive held successors. (Multiple missing
         chunks → one silence per missing chunk, each only while ≥ window frames are held: exactly spec wording.)
     `drain() -> list[OrderedFrame]`: end of stream (`stop`): emits all held frames in order, filling interior gaps
       with silence (resync rule applies to gaps > max_gap); buffer empty afterwards, `expected` = last + 1.
     Properties: `expected: int` (next chunk to emit), `held: int` (buffered count), `stats: ReorderStats`.
     Use a dict + `min()` only when needed (window is 5 → O(window)); no heap needed.
   - Timeout: spec is count-based (Telnyx streams frames continuously, silence included), so no wall-clock timer.
     The plan line "gap fill with silence after timeout" ≈ the window (5 later frames ≈ 100 ms) — record in Decision.
2. `packages/va_voice/src/va_voice/reorder_metrics.py`: `class CatalogReorderMetrics` (implements ReorderObserver
   over a media-worker `va_common.metrics.ServiceMetrics`): `va_audio_reorder_total.inc(count)`,
   `va_audio_gap_frames_total.inc(count)`; duplicates go to stats only (no metric) — `duplicate` returns None.
3. `packages/va_common/src/va_common/metrics.py`: add `_c("va_audio_gap_frames_total", "Silence frames inserted for
   missing inbound audio chunks")` right after `va_audio_reorder_total` in MEDIA_WORKER.
4. `specs/27-observability.md` §3.3: add row `| \`va_audio_gap_frames_total\` | counter | — |` after the reorder row.
   `packages/va_common/tests/test_metrics.py`: 57 → 58. Also bump any other place asserting the media-worker
   catalog size (grep "57 spec rows" in AGENTS.md P03-T10 line → update to 58).
5. `specs/07-media-pipeline.md` §5.1: `> Decision (2026-11-01):` — module `va_voice.reorder.ReorderBuffer(fmt,
   window=5, first_chunk=1, max_gap=50)`; metric name is `va_audio_gap_frames_total` (specs/27 §3.3; the old
   `media_gap_frames_total` text is superseded); `va_audio_reorder_total` counts frames that arrived ahead of the
   expected chunk; duplicates and late frames (chunk ≤ last emitted, incl. frames whose slot was already filled with
   silence) are dropped and counted in stats only; gaps wider than 50 frames (1 s) resync without filling;
   `drain()` at stream stop; count-based only (no timer). Update the §5.1 text "counts `media_gap_frames_total`" →
   keep original sentence, the Decision supersedes (or edit it to the new name — editing is cleaner; do that).
6. `packages/va_voice/tests/test_reorder.py` (+ `test_reorder_metrics.py`):
   - in-order stream passes through immediately (1 frame out per push, never held).
   - jitter pattern 2,1,4,3,...: output strictly 1..n, reordered count = n/2, no gaps.
   - single missing chunk: push 1, 3,4,5,6 → nothing after 3..6 held(4); push 7 (5 held) → silence(2, filled) then
     3..7; gap_frames == 1; late arrival of 2 afterwards → dropped, duplicates == 1.
   - two missing chunks (2,3): exactly two silence frames, released one per window condition.
   - duplicates: chunk repeated while held, and chunk ≤ last emitted → dropped, stats + observer counts.
   - first chunk out of order (2 then 1) with default first_chunk=1 → 1, 2.
   - custom first_chunk; chunk < first_chunk dropped.
   - L16_16000 fmt: silence frame is 640 zero bytes; ULAW: 160 × 0xFF.
   - resync: push 1, then 500..504 → no silence flood: resyncs == 1, output 1, 500..504.
   - drain: fills interior gaps, empties buffer, subsequent push of next chunk continues; drain on empty → [].
   - constructor validation (window 0, max_gap < window → ValueError).
   - hypothesis properties: (a) any permutation where each element is displaced < window positions (generate by
     shuffling within random blocks of size ≤ window, or sorting by index + random offset in [0, window)) of chunks
     1..n plus `drain()` → output chunks exactly 1..n, no filled frames, payloads preserved; (b) random subset
     dropped + local shuffle → output after drain is contiguous first..last with filled exactly at dropped chunks
     (when drops are isolated ≤ max_gap), every payload length = frame_bytes for fills; (c) random duplicates
     injected never appear twice in output; output chunks always strictly increasing by 1 (also across pushes).
   - metrics: `CatalogReorderMetrics(build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False)))`
     wired as observer; after a jittered stream with one gap the registry values of `va_audio_reorder_total` and
     `va_audio_gap_frames_total` equal stats (read via `registry.get_sample_value`).
   - Avoid jscpd clones with existing tests: build streams with small helpers (`frames(chunks)` → payload = chunk
     number encoded in bytes so payload preservation is checkable).

### Docs
- AGENTS.md: P04-T02 bullet (ReorderBuffer API, count-based, drain at stop, gap metric, chunks start at 1); update the
  P03-T10 "57 spec rows" phrase to 58.

### Gates
`make fmt`, `make lint`, `make typecheck`, `make placeholders`, `uv run pytest packages/va_voice packages/va_common
-q`, `make test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a`
on reorder.py (keep push/drain ≤ B by extracting `_release()`), pip-audit + pnpm audit.

### Risks
- Other tests asserting media-worker metric names/count (grep `va_audio_reorder_total` and `57`) — update together.
- hypothesis permutation generator must truly keep displacement < window, else gap fills appear legitimately; drain
  covers residual held frames.

## Build P04-T02 (Builder, 2026-11-01)
Implemented as planned: `va_voice.reorder` (ReorderBuffer/OrderedFrame/ReorderStats/ReorderObserver/NULL_OBSERVER),
`va_voice.reorder_metrics.CatalogReorderMetrics`, `va_audio_gap_frames_total` in MEDIA_WORKER catalog + specs/27 §3.3
row (test_metrics 57→58, AGENTS P03-T10 phrase updated), specs/07 §5.1 metric name edited + Decision, AGENTS bullet.
Tests: packages/va_voice/tests/test_reorder.py (19 tests incl. 3 hypothesis properties: bounded jitter → no fills,
drops → silence exactly at missing chunks, injected repeats never emitted + output contiguous across pushes).
Gates: fmt ok, lint pass, mypy pass (828 files), placeholders 0, spec refs 0, make test 6610 passed / 7 skipped
(98.45 %), va_voice 100 % coverage, jscpd 0 clones, radon A (1.56), pip-audit clean, pnpm audit 3 moderate (no high).

## Review P04-T02 (Reviewer, 2026-11-01)
Approved. Re-ran: lint pass, typecheck pass (828 files), placeholders 0, make test 6610 passed / 7 skipped (98.45 %),
va_voice 92 passed 100 % coverage, jscpd 0 clones, radon A (1.65), spec refs 0. Diff matches specs/07 §5.1 (window 5,
one silence per missing chunk, duplicates ≤ last emitted dropped) + Decision; metric added to specs/27 §3.3 and catalog.

## Plan P04-T03 (Planner, 2026-11-01)
Silero VAD wrapper — specs/07 §5.3 (+ §21 CPU budget, §22 VAD tests), specs/30 §3.1 `test_synth_detected_by_silero`.
Facts: `models/silero_vad.onnx` (v5.1.2, sha in `models/MANIFEST.json`) is present locally (git-ignored, fetched by
`make setup`); onnxruntime is NOT installed yet (latest 1.31.1, cp312 wheels for linux x86_64/aarch64 + macOS arm64).
`apps/media_worker/src/va_media/vad_model.py` already verifies the manifest for the `vad` readiness check (P00-T12).
CI (`.github/workflows/ci.yml` `check` job) never runs `scripts/fetch_models.sh` → real-model tests would fail there.

### Dependencies / settings
1. `packages/va_voice/pyproject.toml`: add `onnxruntime>=1.31.1`; `uv lock`/`uv sync --all-packages`. If it has no
   `py.typed`, add a commented `[[tool.mypy.overrides]] module = ["onnxruntime", "onnxruntime.*"]
   ignore_missing_imports = true` in root pyproject (only `va_voice/vad_model.py` imports it; wrap results in typed fns).
2. New env var `MEDIA_VAD_THREADS` (media, default `2`, "VAD inference thread pool size per process") — add to the
   specs/02 §6 table directly after `MEDIA_MAX_CALLS_PER_POD`, and to `.env.example` at the same position (with trailing
   comment, `scripts/tests/test_env_example.py` rules); `MediaSettings.media_vad_threads: int = Field(default=2, ge=1)`
   (+ a settings test in apps/media_worker/tests). No compose change (default suffices).
3. CI: in `ci.yml` `check` job add a step `run: scripts/fetch_models.sh` after `uv sync` (optionally
   `actions/cache` on `models/silero_vad.onnx` keyed by `hashFiles('models/MANIFEST.json')`); update
   `scripts/tests/test_ci_workflow.py` if it pins the step list; run `uv run actionlint`.

### Files
4. `packages/va_voice/src/va_voice/vad_model.py` (moved + extended from va_media):
   - `MODEL_NAME = "silero_vad"`, `MANIFEST_NAME = "MANIFEST.json"`, `class VadModelError(RuntimeError)`.
   - `verify_model_file(models_dir: Path) -> Path` (sync; manifest entry lookup + sha256 via `hashlib.file_digest`,
     same messages as today) — moved from va_media `_manifest_entry`/`_verify`.
   - `class SileroModel`: `load(models_dir: Path) -> SileroModel` classmethod (sync — call via to_thread/executor;
     verifies the manifest FIRST, then `ort.InferenceSession(path, sess_options, providers=["CPUExecutionProvider"])`
     with `intra_op_num_threads=1`, `inter_op_num_threads=1`, `ORT_SEQUENTIAL`, log severity 3); checks input/output
     names `input,state,sr` / `output,stateN` and raises `VadModelError` otherwise.
     `infer(frame: NDArray[np.float32] (576,), state: NDArray[np.float32] (2,1,128)) -> tuple[float, NDArray]`
     (feeds `sr=np.array(16000, dtype=np.int64)`). One session per process, shared by all streams (ORT `run` is
     thread-safe); per-stream state lives in `SileroVad`.
   - `class VadModel(Protocol)`: `infer(frame, state) -> tuple[float, NDArray[np.float32]]` (fake models in tests).
   - `apps/media_worker/src/va_media/vad_model.py`: keep `VadStatus` and `async verify_vad_model(models_dir)` (=
     `await asyncio.to_thread(verify_model_file, models_dir)`), re-export `VadModelError`, `MODEL_NAME`,
     `MANIFEST_NAME` from va_voice; delete the duplicated private helpers (existing `test_vad_model.py` must still pass).
     Add `va-voice` to apps/media_worker/pyproject.toml dependencies (+ workspace source).
5. `packages/va_voice/src/va_voice/vad.py`:
   - Constants (production thresholds, exported): `SAMPLE_RATE = 16_000`, `WINDOW_SAMPLES = 512`, `CONTEXT_SAMPLES = 64`,
     `WINDOW_MS = 32.0`, `SPEECH_THRESHOLD = 0.5`, `SILENCE_THRESHOLD = 0.35`, `START_WINDOWS = 3`,
     `ENERGY_FLOOR_DBFS = -55.0`.
   - `@dataclass(frozen=True, slots=True) class VadEvent: kind: Literal["speech_start","speech_end","probability"];
     at_ms: float; probability: float; speech: bool`. `probability` events: one per window, `at_ms` = window start,
     `speech` = hysteresis decision after the energy floor. `speech_start.at_ms` = start of the first of the 3
     consecutive speech windows (`SpeechStarted(t_start)`); `speech_end.at_ms` = end of the last speech window
     (`SilenceStarted(t_last_voiced_end)`). Times are stream audio time (samples fed / 16), not wall clock → deterministic.
   - `class VadDecider` (pure, sync): `step(at_ms, probability, rms_dbfs) -> list[VadEvent]` implementing hysteresis
     (p ≥ 0.5 speech, p < 0.35 non-speech, between keeps previous, RMS < −55 dBFS forces non-speech), start rule
     (3 consecutive speech windows while not speaking → speech_start), end candidate (first non-speech window while
     speaking → speech_end, state back to not-speaking; resumption = the start rule again → a new speech_start, which
     the endpointer reads as "candidate reset"). `reset()`. Properties `speaking`, `speech` (current frame decision).
   - `class VadExecutor`: `__init__(threads: int)` → `ThreadPoolExecutor(max_workers=threads, thread_name_prefix="va-vad")`;
     `async run(fn: Callable[[], T]) -> T` (`loop.run_in_executor`); `close()` (`shutdown(wait=True, cancel_futures=True)`);
     async context manager; `from_settings(threads)` trivial — media worker passes `settings.media_vad_threads` later.
   - `class SileroVad`: `__init__(model: VadModel, executor: VadExecutor)`; `async feed(pcm16k: NDArray[np.int16]) ->
     list[VadEvent]`: append to a pending int16 buffer; take all complete 512-sample windows; ONE executor submission
     runs them sequentially (`_infer_windows`: float32 = x/32768 via `va_voice.audio` conversion, prepend 64-sample
     context, update context/state per window, compute `rms_dbfs` per window with `va_voice.audio.rms_dbfs`), then
     the decider runs on the loop (cheap). Leftover < 512 samples stays pending. Concurrent `feed` on one instance
     serialised with an `asyncio.Lock` (order preserved). `reset()` (state + context zeros, pending cleared, decider
     reset, stream time back to 0 — "reset when the call leg changes"). Property `elapsed_ms`.
     Input validation: int16 1-D else `ValueError`.
   - Re-export from `va_voice/__init__.py` only if that module already re-exports (check; keep consistent).
6. Fixtures `packages/va_voice/tests/fixtures/vad/` (committed, 16 kHz mono PCM16 WAV), produced by
   `packages/va_voice/tests/support/vad_fixtures.py` (`build_fixtures() -> dict[str, bytes]`, `main(["--write"|"--check"])`,
   deterministic: PCG64 raw draws only, sim synth with fixed seeds; tests directory `support` on pytest pythonpath +
   mypy_path per AGENTS P00-T12 pattern — check whether `packages/va_voice/tests/support` needs adding):
   - `speech.wav`: 0.5 s static (assets `static.pcm16k` slice, ≈ −40 dBFS) + body of `synthesize_pcm16(1, "hello
     there how are you", sample_rate=16000, seed=…)` with the 180 ms marker preamble sliced off + 0.8 s static +
     second body (other text/seed/f0 200 Hz) + 0.6 s static. A sidecar `speech.json` with truth segments (ms).
   - `silence.wav` (2 s digital zero), `noise.wav` (2 s of `static.pcm16k`), `dtmf.wav` (`synthesize_dtmf` digits
     "159#" at 16 kHz, 120 ms each with 80 ms gaps). Spec says TTS-generated fixtures, but tests may not call vendors and
     FakeElevenLabs TTS emits tones: record a `> Decision` that the speech fixture is the §3.1 formant synthesis.
   - `test_vad_fixtures_are_reproducible`: `build_fixtures()` == committed bytes (regenerate with
     `uv run python packages/va_voice/tests/support/vad_fixtures.py --write`; add to AGENTS).
7. Tests `packages/va_voice/tests/test_vad.py` (decider + stream with a scripted fake model) and
   `test_vad_silero.py` (real model; module-scoped fixture `SileroModel.load(REPO_ROOT / "models")` + executor(2)):
   - decider table: hysteresis band keeps previous decision; 2 speech windows → no start, 3 → start at first window
     start; end candidate at end of last speech window; resume emits a second start; energy floor overrides p=0.99;
     reset clears.
   - stream: windows split across arbitrary feed sizes (hypothesis partition of a buffer) give identical events to one
     big feed; leftover kept; context = last 64 samples of the previous window (assert the fake model saw it);
     state threaded from output to next input; inference runs off the loop thread (fake model records
     `threading.current_thread().name.startswith("va-vad")`); one executor submission per feed (counting executor);
     concurrent feeds keep order; reset zeroes state/context/time; non-int16 / 2-D input → ValueError.
   - real model: `load` rejects a sha mismatch / missing manifest entry (tmp dir copy) with `VadModelError`; input/output
     shapes (prob in [0,1], state shape (2,1,128) changes after speech); `speech.wav` → exactly 2 speech_start/speech_end
     pairs, starts within ±100 ms and ends within ±200 ms of `speech.json` truth (Builder: measure, then assert the
     tightest honest bound); `silence.wav`/`noise.wav` → no speech_start and every window `speech=False`
     (energy floor test: static ≈ −40 dBFS is above the floor so this proves the model, digital zero proves the floor);
     `dtmf.wav` → no speech_start (if Silero flags DTMF, document the measured behaviour in the Decision and assert it);
     state independence: two `SileroVad` on one model interleaved give the same events as run separately.
   - `test_synth_detected_by_silero` (specs/30 §3.1): PCG64(seed fixed) draws 20 (seed, text length 20–60 chars, f0
     100–220 Hz) tuples inside ONE test (never parametrize random values); for each: `synthesize_utterance` (μ-law
     8 kHz, production wire) → `va_voice.audio` μ-law decode → `Resampler(8000, 16000)` + flush → `SileroVad.feed`;
     windows lying fully inside the voiced body (after the 180 ms preamble, accounting resampler delay ≤ a few ms, skip
     the first 3 windows of the body for hysteresis warm-up only if needed and document it) → ≥ 90 % `speech=True`.
   - perf guard: mean real-model inference per window < 2 ms (time 300 windows with `time.perf_counter`, generous;
     mark `@pytest.mark.bench` if it proves noisy — the exit-gate bench marker exists? check pytest markers config).
8. Docs: specs/07 §5.3 `> Decision (2026-11-01)`: module layout (`va_voice.vad_model.SileroModel` shared session +
   `va_voice.vad.SileroVad` per leg + `VadExecutor` sized by `MEDIA_VAD_THREADS`), event kinds/time base, resumption =
   start rule again, energy-floor windows still run the model (state continuity), one executor hop per feed, speech
   fixture = formant synthesis. AGENTS.md P04-T03 bullet (model must be fetched: `make setup` / CI step; fixture
   regeneration command; fake-model seam).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice apps/media_worker scripts -q`, `make test`,
`uv run python scripts/check_spec_refs.py`, `uv run actionlint`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src`, pip-audit (new onnxruntime + transitive deps: flatbuffers, protobuf, coloredlogs/sympy?) +
pnpm audit.

### Risks
- onnxruntime pulls protobuf/sympy; pip-audit must stay clean. ORT may emit warnings at import (filterwarnings=error):
  set `ort.set_default_logger_severity(3)`; if a DeprecationWarning fires, add a narrow filter with a reason.
- Silero on synthetic formant speech must reach ≥ 90 %; the sim's Decision (5) tuned the envelope floor for this. If it
  fails, the fix belongs in va_sim synthesis (spec-allowed knob) — not in thresholds (production thresholds are fixed).
- Real-model latency under xdist + coverage: keep perf assertion generous or bench-marked.
- Moving verification from va_media to va_voice must keep `apps/media_worker/tests/test_vad_model.py` green.

## Build P04-T03 (Builder, 2026-11-01)
Implemented per plan with these deviations/details:
- `va_voice.vad_model`: `SileroModel.load` (verify_model_file → ORT CPU session, 1 thread, sequential, log severity 3;
  ORT load failure → VadModelError; signature check in `__init__`), `infer` validates shapes, `initial_state()`,
  `VadModel` Protocol. onnxruntime 1.31.1 (+flatbuffers) added; mypy override (no py.typed).
- `va_voice.vad`: VadDecider / VadExecutor / SileroVad as planned. `reset()` is async (takes the feed lock). The pool
  computes on copies (`_infer_windows` pure → `_Inference`), state committed only on completion, so a cancelled feed
  drops its audio safely (tested).
- `va_media.vad_model` delegates to va_voice; va-media-worker depends on va-voice. `MEDIA_VAD_THREADS` (default 2) in
  specs/02 §6 + .env.example + MediaSettings (env-example counts 107/108/LITERAL 80). CI `check` job runs
  `scripts/fetch_models.sh` after `uv sync` (test asserts step order); no cache step.
- Fixtures generated by `packages/va_voice/tests/support/vad_fixtures.py` (truth in `SPEECH_SEGMENTS_MS`, no json
  sidecar); noise = `office` asset (static is too quiet to prove much). Measured: starts +2..+12 ms, ends +56..+98 ms;
  max p silence 0.01 / office 0.09 / DTMF 0.15; synth bodies 100 % speech; ~0.1 ms/window. Perf guard = median < 2 ms
  (no bench marker yet).
- specs/07 §5.3 Decision + AGENTS bullet.
Gates: fmt ok; lint pass; typecheck pass (833 files); placeholders 0; spec refs 0; make test 6653 passed / 7 skipped
(98.46 %; va_voice vad.py + vad_model.py 100 %); jscpd 0 clones; radon A (2.03); pip-audit clean; pnpm audit 3
moderate (no high); actionlint ok.

## Review P04-T03 (Reviewer, 2026-11-01)
Approved. Re-ran: lint pass, typecheck pass, placeholders 0, make test 6653 passed / 7 skipped (98 %; vad.py and
vad_model.py 100 %), va_voice 133 passed, jscpd 0 clones. Checked against specs/07 §5.3: I/O shapes, zero initial
state/context, hysteresis 0.5/0.35, 3-window start at first window start, end candidate at last voiced window end,
-55 dBFS floor, reset per leg, thread-pool inference; MEDIA_VAD_THREADS in specs/02 §6 + .env.example + settings; CI
fetches models. Reviewer tidy-up: rewrapped the vad_model.py module docstring (broken line breaks).
§5.3 local noise gate (§10) is not part of this task (SpectralGate comes later).

## Plan P04-T04 (Planner, 2026-11-01)
Adaptive endpointer — specs/07 §6 (+ §20 `eot_ms`, specs/05 `interruption_threshold` 50–5000 / `ivr_mode`, specs/11
node `resumption_speed` 1–3 + variables). Pure, sync, deterministic: every input carries an explicit ms timestamp on
ONE caller-chosen timeline (the pipeline in P04-T12 maps VAD stream time + partial arrival times onto it); no wall
clock inside. Tests drive time with explicit `now_ms` (the "fake clock"); no sleeps.

### Files
1. `packages/va_voice/src/va_voice/text_norm.py` (shared with P04-T05 backchannels):
   - `tokens(text: str) -> tuple[str, ...]` — lowercase, `re.findall(r"[a-z0-9']+")` after mapping `-` to space?
     NO: keep hyphen handling for T05; here split on anything not `[a-z0-9']` (so "uh-huh" → ("uh","huh")).
   - `normalized(text) -> str` = `" ".join(tokens(text))`.
   - `ends_sentence(raw: str) -> bool` — `raw.rstrip()` ends with `.`, `?`, `!` (also `…`? no; exactly the 3).
   - `ends_with_digit(raw) -> bool` — last char of `raw` with trailing punctuation/whitespace stripped is 0-9.
2. `packages/va_voice/src/va_voice/endpointing.py`:
   - Constants (exported): `BASE_SILENCE_MS = {1: 300, 2: 500, 3: 800}`, `PAUSE_MIN_MS = 120`, `PAUSE_MAX_MS = 1500`,
     `PAUSE_HISTORY = 20`, `MIN_PAUSE_SAMPLES = 4`, `ADAPTIVE_MIN_MS = 250`, `ADAPTIVE_MAX_MS = 1500`,
     `TIMEOUT_MIN_MS = 250`, `TIMEOUT_MAX_MS = 2000`, `IVR_FLOOR_MS = 900`, multipliers `COMPLETE = 0.7`,
     `CONTINUATION = 1.6`, `NUMBER = 1.8`, `SHORT_ANSWERS` / `CONTINUATION_WORDS` frozensets (verbatim §6),
     `NUMBER_NAME_HINTS = ("phone","zip","number","code")`.
   - `@dataclass(frozen=True, slots=True) EndpointerSettings(resumption_speed: Literal[1,2,3] = 2,
     interruption_threshold_ms: int | None = None, ivr_mode: bool = False, collecting_number: bool = False)`;
     `__post_init__` raises `ValueError` for speed ∉ {1,2,3} or threshold outside 50–5000.
   - `collects_number(variables: Iterable[tuple[str, str]]) -> bool` — any (name, type) with type `integer` or
     lowercase name containing a hint (helper for the engine/turn manager to fill `collecting_number`).
   - `percentile(samples: Sequence[float], q: float) -> float` — linear interpolation between closest ranks
     (numpy default method, rank = q/100·(n−1)); pure Python; ValueError on empty.
   - `completeness_multiplier(raw_partial: str, *, collecting_number: bool) -> float` — order (Decision): number rule
     (ends_with_digit and collecting_number → 1.8) first, then complete (ends_sentence or normalized whole text in
     SHORT_ANSWERS → 0.7), then continuation (last token in CONTINUATION_WORDS → 1.6), else 1.0. Empty → 1.0.
   - `@dataclass(frozen=True, slots=True) EndOfTurn(at_ms: float, reason: Literal["silence","late_transcript"],
     last_voiced_end_ms: float, timeout_ms: float)` with property `eot_ms = at_ms − last_voiced_end_ms`.
     `TurnDiscarded(at_ms: float, started_ms: float, last_voiced_end_ms: float)` (noise burst, rule 6).
   - `class Endpointer(settings: EndpointerSettings = EndpointerSettings())`:
     - `configure(settings)` — node change; keeps pause samples (per-caller adaptation persists for the call).
     - `on_vad(event: VadEvent) -> None` — `speech_start`: if no turn open → open turn (`started_ms`, clear text);
       if a silence was pending → pause = `at_ms − last_voiced_end`; append to deque(maxlen 20) iff 120 ≤ pause ≤ 1500;
       clear pending silence; mark speaking. `speech_end`: if speaking → `last_voiced_end = at_ms`, not speaking.
       `probability` events ignored. Duplicate start while speaking / end while silent ignored.
     - `on_partial(text: str, at_ms: float) -> None` — only while a turn is open: store as latest raw partial;
       non-empty (after strip) sets `has_text` and `text_at_ms`. While idle: ignored (trailing update of an ended
       turn; Decision).
     - `adaptive_ms() -> float` (base if < 4 samples else clamp(max(base, 1.25·p90 + 80), 250, 1500)).
     - `timeout_ms() -> float` — fixed threshold → `max(threshold, 900 if ivr else 0)` with NO multiplier/clamp
       (Decision: §6 "timeout = interruption_threshold_ms", range 50–5000 per specs/05); else
       `clamp(adaptive × multiplier(latest partial), 250, 2000)`, then ivr floor `max(·, 900)`. Recomputed on every
       call (a later partial changes the deadline).
     - `text_wait_cap_ms() -> float` = `max(timeout_ms(), TIMEOUT_MAX_MS)` — the max-wait cap: when silence reaches
       the timeout but no partial text yet (STT lags), keep waiting for text until silence reaches this cap, then
       discard (Decision).
     - `deadline_ms() -> float | None` — next time `poll` can return something: None when idle or speaking;
       `last_voiced_end + timeout` if has_text else `last_voiced_end + text_wait_cap` … but must also return the
       timeout instant when no text so the caller re-polls? No: without text nothing happens before text or cap;
       `on_partial` changes the deadline and the caller re-reads it after every input.
     - `poll(now_ms: float) -> EndOfTurn | TurnDiscarded | None` — idle/speaking → None. silence = now − last_voiced_end.
       has_text and silence ≥ timeout → `EndOfTurn(now, "silence" if text_at_ms ≤ last_voiced_end + timeout else
       "late_transcript", …)`; no text and silence ≥ cap → `TurnDiscarded`. Either closes the turn (idle, text
       cleared; pause samples kept).
     - Properties `turn_open`, `speaking`; `reset()` (leg change: everything incl. pause samples).
   - Keep every function radon ≤ B (split `_on_start`, `_on_end`, `_fixed_timeout`).
3. specs/07 §6 `> Decision (2026-11-01)`: pure ms-timeline API (on_vad/on_partial/deadline/poll); partials attributed
   only to an open turn; multiplier order (number first); fixed `interruption_threshold_ms` bypasses multiplier and
   clamps but keeps the ivr floor; percentile = linear interpolation; text-wait cap = max(timeout, 2000) then discard;
   reasons `silence`/`late_transcript`; pause samples persist across nodes, cleared by `reset()`; ivr_mode keeps
   adaptation (floor only).
4. AGENTS.md bullet (P04-T04): module names, time-base rule, `text_norm` shared with T05.

### Tests
`packages/va_voice/tests/test_text_norm.py`: tokens/normalized (punctuation, case, hyphen, apostrophe), ends_sentence,
ends_with_digit ("zip is 9021." True, "five" False).
`packages/va_voice/tests/test_endpointing.py` (helpers `start(ms)`/`end(ms)` building VadEvent; table-driven):
- base per speed: 1/2/3 → EOT exactly at last_end+300/500/800 with neutral partial ("hello there friend" → wait,
  "hello" is a short answer; use "i would like to book"? "to" is continuation — use "i want a table tonight"), and
  `poll(deadline − 0.001)` is None.
- multipliers table: "Yes." / "thank you" / "Is it?" → ×0.7 ; "and" / "I went to the" / "um" → ×1.6 ; "my zip is 90210"
  collecting → ×1.8, not collecting → 1.0 ; "90210." collecting → 1.8 (number first); "" → 1.0; normal → 1.0.
- clamp: speed 1 × 0.7 = 210 → 250; speed 3 adaptive 1500 × 1.6 = 2400 → 2000.
- adaptation: < 4 pauses → base; feed pauses [200,300,400,600] via start/end cycles → p90 by linear interp
  (= 540) → adaptive = max(500, 1.25·540+80=755) = 755; samples outside 120–1500 (100, 1600 not possible before
  timeout? use fixed threshold 5000 to allow a 1600 ms pause) ignored; history keeps last 20 (feed 25, assert p90 of
  last 20); adaptive clamp at 1500 upper; percentile unit tests vs `numpy.percentile` on hypothesis lists.
- fixed `interruption_threshold_ms=1200`: timeout 1200 regardless of partial/adaptation; 50 with ivr → 900; 5000
  allowed; 49/5001 → ValueError; speed 4 → ValueError.
- ivr floor: speed 1 ivr → 900; adaptive 1500×1.6 ivr → 2000.
- partial rules: no partial → no EOT at timeout, `TurnDiscarded` at last_end+2000; partial arriving after timeout but
  before cap → EOT at the arrival poll with reason `late_transcript`; whitespace-only partial does not count; partial
  while idle ignored; a later partial changes deadline (Yes. → then "and" extends).
- resumption: speech resumes before timeout → no EOT, pause recorded; EOT measured from the NEW last_voiced_end;
  `eot_ms` = at − last_end; after EOT state idle and next turn needs new text; `configure` keeps samples, `reset`
  clears them; `probability` events ignored; duplicate start/end ignored; `collects_number` table (integer type,
  "phone_number", "ZIP", "Postcode" false).
- `deadline_ms` values in each state (idle None, speaking None, silence with/without text).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (100 % coverage on new modules),
`make test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src`, pip-audit + pnpm audit (no new deps).

### Risks
- Hypothesis vs numpy percentile float equality: compare with `pytest.approx`.
- Keep `text_norm` minimal but correct for T05 phrase matching (multi-word backchannels match on token tuples).

## Build P04-T04 (Builder, 2026-11-01)
Implemented per plan:
- `va_voice/text_norm.py`: `tokens` (letters/digits any script + inner apostrophes, lowercased), `normalized`,
  `ends_sentence` (ignores trailing spaces + closing quotes/brackets), `ends_with_digit` (ASCII digits only).
- `va_voice/endpointing.py`: constants, `percentile` (linear interp, validates q), `collects_number`,
  `completeness_multiplier` (number → complete → continuation), `EndpointerSettings` (validated),
  `EndOfTurn(at_ms, reason, last_voiced_end_ms, timeout_ms, text)` + `eot_ms`, `TurnDiscarded`, `Endpointer`
  (on_vad/on_partial/adaptive_ms/timeout_ms/text_wait_cap_ms/deadline_ms/poll/configure/reset, pause_samples).
- Deviations from plan: blank partials do not replace the stored partial; `EndOfTurn` also carries `text` (latest
  partial); `collects_number` is a plain substring match, so "Postcode" → True (plan said False; spec says "name
  contains code") — recorded in the Decision.
- specs/07 §6 Decision P04-T04; AGENTS bullet.
Gates: fmt ok; lint pass; typecheck pass (837 files); placeholders 0; spec refs 0; va_voice 239 passed; make test
6759 passed / 7 skipped (98.47 %; endpointing.py + text_norm.py 100 %) — first full run had 1 failure in
apps/sim/tests/test_sim_shared_redis.py (subprocess exit -15 under load; passes alone and on rerun, unrelated);
jscpd 0 clones; radon A (2.35); pip-audit clean; pnpm audit 3 moderate (no high).

## Review P04-T04 (Reviewer, 2026-11-01)
Approved. Re-ran: lint pass, typecheck pass (837 files), placeholders 0, make test 6759 passed / 7 skipped (98.47 %;
endpointing.py + text_norm.py 100 %). Checked against specs/07 §6 rules 1-7: base table, pause range 120-1500 / last 20,
p90 at ≥4 samples, adaptive clamp 250-1500, short-answer / continuation sets verbatim, number rule, timeout clamp
250-2000, non-empty partial requirement + silent discard, ivr floor 900, eot_ms. Decision documents the ambiguities.

## Plan P04-T05 (Planner, 2026-11-01)
Interruptibility policy + backchannels — specs/07 §7 (level table, backchannel set, effective level), §13 (DTMF barge-in
at levels ≥ 1), specs/05 (`interruptibility` 0–4 default 2, `block_interruptions` wins), specs/11 (node
`interruptibility` 0–3 or null; `Say(..., interruptible: bool | None)`). Pure, sync, no clock: caller passes
`speech_ms` (duration of the CURRENT continuous VAD speech segment = now − last `speech_start`; "continuous" per §7).

### Files
1. `packages/va_voice/src/va_voice/interrupt.py` (`__all__` everything public):
   - `InterruptLevel = Literal[0, 1, 2, 3, 4]`; constants `DEFAULT_LEVEL = 2`, `BLOCK, DIFFICULT, BALANCED, EASY,
     ALWAYS = 0..4`; `BACKCHANNELS: Final[tuple[str, ...]]` = §7 list verbatim (29 entries, raw spelling incl.
     `uh-huh`, `mm-hmm`). `_PHRASES: frozenset[tuple[str, ...]]` = `{text_norm.tokens(b) for b in BACKCHANNELS}`
     (so `uh-huh` and `uh huh` collapse to ("uh","huh")); `_MAX_PHRASE = max(len)`.
   - Thresholds as a frozen table `LEVEL_RULES: Mapping[int, LevelRule]` with
     `@dataclass(frozen=True, slots=True) LevelRule(min_speech_ms: float | None, min_words: int, speech_only_ms: float | None)`:
     1 → (1200, words ≥ 4 & not all backchannel, None) ; 2 → (500, 2 non-bc, 900) ; 3 → (250, 1 non-bc, 600) ;
     4 → (None, 0, 150) ; 0 → never. Simplest: implement explicit per-level functions in a dict dispatch to keep
     radon ≤ B; but table must make values greppable for tests.
   - `effective_level(*, call_level: int = 2, node_level: int | None = None, block_interruptions: bool = False,
     interruptible: bool | None = None) -> InterruptLevel`: validates call 0–4, node 0–3 (ValueError otherwise);
     order: `block_interruptions` → 0; `interruptible is False` (a `Say(interruptible=False)` / max-duration message)
     → 0; node level if not None; else call level. `interruptible=True` does not raise the level (Decision).
   - `@dataclass(frozen=True, slots=True) BackchannelMatch(words: tuple[str, ...], backchannel_words: int)` with
     properties `total_words`, `content_words` (= total − backchannel), `all_backchannel` (total > 0 and content == 0).
   - `class BackchannelClassifier` (stateless; `__init__(phrases: Iterable[str] = BACKCHANNELS)` precomputes token
     tuples, rejects empty phrases with ValueError):
     - `classify(text_or_words: str | Sequence[str]) -> BackchannelMatch` — tokens via `text_norm.tokens` when str,
       else lowercased given tokens; greedy left-to-right longest-phrase match (try length `_MAX_PHRASE` down to 1);
       matched tokens count as backchannel, others as content. E.g. "uh-huh" → 2 tokens both bc; "all right, go on" →
       bc 2 content 2; "I see the problem" → "i see" bc, "the problem" content 2; "okay okay" → all bc.
     - `is_backchannel(text) -> bool` = `all_backchannel`.
   - `@dataclass(frozen=True, slots=True) InterruptDecision(interrupt: bool, reason: str)` — reasons
     (Literal): `"not_speaking"`, `"blocked"`, `"speech_and_words"`, `"long_speech"`, `"any_speech"`, `"insufficient"`;
     `__bool__` returns `interrupt` so callers may `if policy.should_interrupt(...)`.
   - `class InterruptPolicy(classifier: BackchannelClassifier | None = None)`:
     - `should_interrupt(speech_ms: float, words: Sequence[str] | None, text: str, level: int, agent_speaking: bool)
       -> InterruptDecision` — `words` = pre-tokenized partial (STT word list when available; None → tokenize
       `text`). Order: not agent_speaking → no ("not_speaking"); level 0 → no ("blocked"); level 4 → speech ≥ 150;
       level 1 → speech ≥ 1200 AND total ≥ 4 AND not all_backchannel; level 2 → (speech ≥ 500 AND content ≥ 2) OR
       speech ≥ 900; level 3 → (speech ≥ 250 AND content ≥ 1) OR speech ≥ 600. Invalid level / negative speech →
       ValueError. Boundaries inclusive (≥).
     - `should_interrupt_dtmf(level: int, agent_speaking: bool) -> InterruptDecision` — interrupt iff agent_speaking
       and level ≥ 1 (reason `"dtmf"`; `blocked` at 0).
2. specs/07 §7 `> Decision (2026-11-01): P04-T05`: module/API; speech_ms = current continuous segment; level-1 reading
   "≥ 4 tokens total and at least one non-backchannel token"; non-backchannel words = tokens not covered by a greedy
   longest-phrase backchannel match on `text_norm.tokens` (hyphenated entries ≡ spaced); boundaries inclusive;
   effective-level order block → `interruptible=False` → node → call; DTMF rule; no interrupt when agent not speaking.
   Also fix specs/30 §5 `interruptibility_levels` row ("≥ 3 words" → "≥ 4 words", 07 is the contract) in the same commit.
3. `va_voice/__init__.py`: only if it re-exports modules (check; endpointing pattern decides).
4. AGENTS.md bullet (P04-T05).

### Tests `packages/va_voice/tests/test_interrupt.py`
- `test_backchannel_set_verbatim`: BACKCHANNELS equals the §7 list parsed from specs/07 (parse the paragraph after
  "Backchannel set" up to the period, backticked items) — keeps spec and code in lockstep.
- classifier table: "uh-huh", "Uh huh.", "mm-hmm", "mhm", "Okay okay!", "all right", "alright", "got it", "I see" →
  all_backchannel; "I see the problem" (bc 2, content 2); "yeah but wait" (content 2); "" → total 0, not all_bc;
  "right now please" (content 2); token-sequence input path; custom phrase set; empty phrase → ValueError.
- `test_matrix` parametrized over levels 0–4 × scenarios exactly per spec: backchannel ("uh-huh", 400 ms),
  backchannel long ("yeah", 1000 ms), short content ("wait", 300 ms), two words ("wait stop", 600 ms), long sentence
  ("hold on I have a question", 1300 ms), long silence-text (no partial "", 950 ms), 4 backchannels ("okay okay
  yeah right", 1300 ms) → expected bool table written out literally (+ reason).
- boundary tests per level (just below / at each threshold: 149/150, 249/250, 499/500, 599/600, 899/900, 1199/1200),
  level-1 3 words vs 4 words.
- `agent_speaking=False` → never, every level; `words` override beats `text`.
- `effective_level` table incl. validation errors (call 5, node 4, call -1); `interruptible=True` keeps node/call.
- DTMF: levels 0..4 × speaking.
- hypothesis property: monotonic in speech_ms (if interrupt at t then at t' > t for same text/level) and in level
  1→4 for levels 2..4 (level k interrupt ⇒ level k+1 interrupt for k ≥ 2; note level 1 vs 2 also monotone since
  1200∧4words ⊂ 900 rule) — assert for all k in 1..3.
- ValueError on level 5 / negative speech_ms.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (interrupt.py 100 %), `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a packages/va_voice/src`,
pip-audit + pnpm audit (no new deps).

### Risks
- Monotonic-in-level property: level 1 requires ≥4 tokens & speech ≥1200 → level 2 then true by speech ≥ 900. OK.
- Keep `should_interrupt` radon ≤ B: dispatch dict of per-level predicates.

## Build P04-T05 (Builder, 2026-11-01)
Implemented per plan: `va_voice/interrupt.py` (BACKCHANNELS verbatim, LEVEL_RULES table, effective_level,
BackchannelClassifier greedy longest phrase on text_norm.tokens, InterruptDecision truthy + reason, InterruptPolicy
should_interrupt / should_interrupt_dtmf). Deviations: `LevelRule` fields are min_speech_ms/min_total_words/
min_content_words/speech_only_ms; word lists are re-tokenized (punctuation/hyphens split); bool levels rejected;
effective_level validates node level even when blocked. specs/07 §7 Decision P04-T05; specs/30 interruptibility_levels
row fixed (≥ 4 words); AGENTS bullet. Tests `tests/test_interrupt.py` (134: spec-parsed backchannel set, 12-scenario
× 5-level matrix with reasons, inclusive boundaries, hypothesis monotonicity in speech and level, DTMF, effective level).
Gates: fmt ok; lint 0; typecheck 0; placeholders 0; spec refs 0; make test 6893 passed / 7 skipped (98.48 %;
interrupt.py 100 %); jscpd 0 clones; radon A (1.95); pip-audit clean; pnpm audit 3 moderate (no high).

## Review P04-T05 (Reviewer, 2026-11-01)
Approved. Re-ran: lint pass, typecheck pass, placeholders 0, make test 6893 passed / 7 skipped (98.48 %; interrupt.py
100 %). Checked against specs/07 §7 table (levels 1-4 thresholds, inclusive), backchannel set (spec-parsed test),
effective level order (block → interruptible=False → node 0-3 → call 0-4), §13 DTMF barge-in at levels ≥ 1. Decision
note covers ambiguities; specs/30 interruptibility_levels row aligned to 07 (≥ 4 words).
