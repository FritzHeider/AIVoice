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

## Plan P04-T06 (Planner, 2026-11-01)
Clause chunker, specs/09 §5 (consumer: specs/07 §8 step 4; each clause goes to `TtsSocket.send(ctx, clause, flush=True)`,
then `flush(ctx)` at the end of the utterance, see the specs/09 §11 Decision P03-T10 item 4). The module is pure and sync,
with a thin async adapter. specs/09 puts text utilities in `va_voice/text/`, so create that subpackage (T07 adds
`pronunciation.py` next to it).

### Files
1. `packages/va_voice/src/va_voice/text/__init__.py`: docstring only, no imports, so importing `va_voice.text.chunker`
   stays cheap.
2. `packages/va_voice/src/va_voice/text/markdown.py`: markdown and whitespace normalization.
   - `normalize_text(raw: str) -> str`: the reference whole-text function. Process line by line (`\r\n`/`\r`/`\n`).
     At line start (after leading spaces/tabs), drop in a loop: a run of `#` followed by whitespace/EOL, the list markers
     `- `, `* `, `+ ` and `\d{1,3}[.)] ` (one marker per line, after headings). Remove every backtick. Remove every
     `*` run. Remove a `_` run unless it has a letter or digit on BOTH sides (`user_id` is kept, `_word_` → `word`).
     Then newlines become spaces, whitespace runs (any `str.isspace`) collapse to one space, and the result is
     stripped.
   - `class MarkdownStripper`: the streaming twin. `feed(raw: str) -> str` returns the normalized text that is now
     final; `finish() -> str` returns the rest and resets. Contract (hypothesis-tested): for ANY split of a raw string
     into deltas, `"".join(feed(d) for d in deltas) + finish() == normalize_text(raw)`. Implementation: keep an
     undecided raw tail and only release text whose normalization can no longer change. Hold back:
     (a) the current line while its line-start prefix is undecided (only whitespace / `#` / `-*+` / digits seen, or a
     marker that is not yet followed by its space);
     (b) a trailing `_`/`*` run, plus one char of lookahead after a `_` run (intraword check needs the next char);
     (c) a trailing whitespace run. Emit it as a single space only once a non-space char follows; drop it at
     `finish()`; never emit leading whitespace at stream start.
     Simplest correct approach: track `_line_start: bool`, `_pending_space: bool`, `_started: bool`, `_prev_char` (last
     raw non-markup char, for the `_` rule) and the held raw tail. Process char by char in a small loop with helper
     methods; keep every function radon ≤ B.
3. `packages/va_voice/src/va_voice/text/chunker.py`:
   - Constants (`Final`): `ABBREVIATIONS` = the §5 set verbatim (`frozenset`: mr mrs ms dr st jr sr inc ltd co corp vs
     etc e.g i.e a.m p.m u.s no ave blvd dept approx est mt ft apt); `SENTENCE_END = ".!?"`, `CLAUSE_END = ",;:"`,
     `EM_DASH = "—"`; `FIRST_SENTENCE_MIN_WORDS = 2`, `SENTENCE_MIN_WORDS = 3`, `FIRST_CLAUSE_MIN_WORDS = 4`,
     `CLAUSE_MIN_WORDS = 7`, `LENGTH_CAP = 160`.
   - `class ClauseChunker`, with `__init__()` and state `_stripper: MarkdownStripper`, `_buf: str`,
     `_emitted_count: int`:
     - `feed(delta: str) -> list[str]`: run the stripper, append to `_buf`, then repeatedly take the earliest valid
       boundary. Each returned clause is `buf[:i+1].strip() + " "` (exactly one trailing space, specs/09 §3).
     - `flush() -> list[str]`: `finish()` the stripper, run the boundary loop once more (end of stream counts as
       whitespace after the last char), then return the stripped remainder (+ " ") when it has a letter or digit. A
       punctuation-only remainder is dropped (§5 rule 5). Reset all state so the instance can serve the next utterance.
     - `emitted_count` property; `pending` property (the current `_buf`, for tests and debugging).
   - Boundary search, `_boundary(buf, *, final: bool) -> int | None` = min over valid candidates of:
     - sentence: `buf[i] in ".!?"`, `buf[i+1]` is whitespace (or `i` is the last index and `final`); token before the
       terminator (chars back to the previous whitespace, the trailing terminator removed, leading opening quotes and
       brackets stripped, lowercased) is not in ABBREVIATIONS; not a digit on both sides; words(buf[:i+1]) ≥ 2 for the
       first clause, else ≥ 3.
     - clause: `buf[i] in ",;:"` or `buf[i] == "—"`, or `buf[i] == "-"` with whitespace before it; also followed by
       whitespace (or last+final); not digit-on-both-sides; words ≥ 4 for the first clause, else ≥ 7.
     - cap: `len(buf) >= 160` → `j = buf.rfind(" ", 0, 160)`; when `j > 0` the boundary is `j` (the clause is
       `buf[:j]`). With no space in the first 160 chars, hard-cut at 159 (clause = `buf[:160]`).
     - words = whitespace-separated tokens that contain at least one letter or digit.
     Rescanning from 0 on every delta is acceptable because buf stays ≤ ~160 chars. Keep `_boundary` under radon B by
     splitting it into `_sentence_ok(buf, i, first)`, `_clause_ok(...)`, `_cap(buf)`.
   - `async def chunk_stream(deltas: AsyncIterable[str], chunker: ClauseChunker | None = None) -> AsyncIterator[str]`:
     yields the clauses of each delta, then of `flush()` (the specs/09 §5 "async stream" input form).
4. specs/09 §5: add `> Decision (2026-11-01): P04-T06 ...` recording:
   - API: `feed`/`flush`/`chunk_stream`; each clause ends with exactly one space; `flush()` resets.
   - A boundary char must be followed by whitespace (end of stream counts) for clause and dash boundaries too, not
     only sentences. Otherwise `a,b` would be split and rejoining would change the text. This also guarantees that
     `3.14`, `$1,250.50` and `10:30` are never split.
   - ` - ` counts only with whitespace on both sides. `—` counts only when followed by whitespace.
   - `no` is treated as an abbreviation only when the next token starts with a digit (`No. 5`), because "No. I can
     help" is common in phone dialogue. Every other set member always counts.
   - The abbreviation token is compared after stripping leading quotes/brackets.
   - A boundary candidate that fails its minimum-word rule is skipped and the scan continues, so the earliest VALID
     index wins.
   - The length cap hard-cuts at 160 when the first 160 chars contain no space.
   - Markdown rules: the exact `*`/`_`/backtick/heading/list-marker rules above, with streaming output equal to the
     whole-text normalization.
   - A punctuation-only end-of-stream remainder is dropped, never sent.
   - The property "concatenation equals normalized input" means `"".join(clauses) == normalize_text(input) + " "`
     whenever every whitespace-separated token contains a letter or digit and no 160-char run lacks a space.
5. AGENTS.md bullet (P04-T06): module locations, the `va_voice.text` subpackage (T07 goes there), the trailing-space
   contract, and that `normalize_text` is the reference for the streaming stripper.

### Tests
`packages/va_voice/tests/text/test_markdown.py`:
- Table of `normalize_text` cases: `**bold**`, `*em*`, `_em_`, `snake_case_id` kept, `` `code` ``, `# Title\nText`,
  `### H`, `- a\n- b`, `* a`, `+ a`, `1. a\n2. b`, `12) x`, `#hashtag` mid-line kept (no space after `#` at line
  start is also kept), `-5 degrees` at line start kept, `15 people` at line start kept, `\r\n`, tabs, NBSP, leading
  and trailing whitespace, empty input.
- Streaming equivalence: hypothesis over text drawn from an alphabet of markdown-heavy chars
  (`"ab1 _*#-+.)\n\t`"` plus letters) × random split points: the join of `feed`s plus `finish` equals
  `normalize_text`. Also a char-by-char feed for every table case. `finish()` resets: a second use behaves like a
  fresh instance.

`packages/va_voice/tests/text/test_chunker.py`:
- Golden cases (feed the whole text, then flush; and char by char; both must give the same clauses):
  - "Thanks for calling Acme Dental. How can I help you today?" → 2 clauses.
  - First-clause rules: "Hi. I'm Sarah from Acme." → one clause, since "Hi." has 1 word < 2. "Hi there. Yes." →
    "Hi there." | "Yes." (the remainder comes out at flush).
  - Later sentence needs 3 words: "Hello there. Ok sure. Great, let's go." → "Hello there." | "Ok sure. Great, let's
    go."
  - Clause boundaries: first clause at a comma after ≥ 4 words ("Sure thing, my friend, I can..."); later clauses need
    ≥ 7 words.
  - Abbreviations: "Dr. Smith will see you at 3 p.m. tomorrow, ok." does not split after "Dr." or "p.m". Also "e.g."
    and "U.S.". "No. 5 Main St. is open." does not split, while "No. I can't do that today." splits after "No." only
    if the min-words rule passes. Assert: first clause "No." has 1 word, so no split; a later "No. I can't" has 1
    word < 3, so no split. Also assert a later "I said no. Then we left." splits after "no." (3 words).
  - Numbers: "It costs $1,250.50 total, which is 3.14 times more, at 10:30 today." is never split inside the
    numbers.
  - Ellipsis: "Well... I think so. Okay then." splits after "Well..." only by the word rules (1 word < 2, so no).
    "Let me check that... one moment please." gives the clause "Let me check that..." (4 words).
  - Dashes: "I can help — let me look that up for you right now." (em dash with spaces, 4 words); "well-known"
    never splits; "a - b" counts.
  - Length cap: 170 chars of words with no punctuation → the first clause ends at the last space before 160 and is
    ≤ 160 chars. A 300-char run with no spaces → hard cut at 160.
  - Markdown stream: "**Sure!** Here are options:\n1. Cleaning\n2. Whitening" → no `*`, no `1.` markers.
  - Punctuation-only remainder dropped: feed "Hello there friend. " then " ... )" → flush returns []. Whitespace-only
    input gives [].
  - Every returned clause ends with exactly one space and has no leading space.
  - `flush()` resets `emitted_count`: the next utterance applies the first-clause minimums again.
  - Streaming holds back an undecided terminator: feeding "It is 3." returns [] (or no clause ending in "3."), and
    feeding "14 now." then yields "It is 3.14 now." only once the rules allow it.
- Hypothesis properties (`@given` text built from tokens = alnum words (1-12 chars, letters/digits, optionally with
  embedded `.`/`,`/`:` between digits like `3.14`) + optional trailing punctuation from `.,;:!?`, plus "—" and "-"
  tokens, joined by random whitespace incl. `\n`; split into random deltas):
  1. `"".join(all clauses) == normalize_text(input) + " "` (or `[]` when normalized is empty).
  2. Every clause is ≤ 200 chars (actually ≤ 161 incl. the space).
  3. Number tokens `3.14`, `$1,250.50`, `10:30` inserted at random positions appear intact inside one clause.
  4. Split invariance: the clauses are identical for every delta split.
- `chunk_stream`: an async generator of deltas yields the same clauses as feed + flush, and works with a supplied
  chunker (pytest-asyncio, already used by the package; check `asyncio_mode` in root pyproject).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (100 % on `text/`), `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a packages/va_voice/src`,
pip-audit and pnpm audit (no new deps).

### Risks
- The streaming stripper equivalence is the hard part. Write `normalize_text` first, then build the stripper against
  the hypothesis test with a markdown-heavy alphabet. If a held-back rule is wrong, the property finds it fast.
- The `_` rule needs the next char, so hold one char. The line-start rule may hold a whole short line: that is fine,
  because lines are released as soon as a non-marker char arrives.
- Hypothesis text generation must avoid punctuation-only tokens and spaceless runs ≥ 160 for property 1 (both are
  covered by unit tests instead).

## Build P04-T06 (Builder, 2026-11-01)
Implemented per plan: `va_voice/text/__init__.py` (docstring only), `text/markdown.py` (`normalize_text` reference +
streaming `MarkdownStripper` with `space_pending`; line-start prefix regex + "undecided" regex), `text/chunker.py`
(`ClauseChunker` feed/flush/emitted_count/pending, `chunk_stream`). Deviations from plan: chunker uses the stripper's
`space_pending` so a boundary at the buffer end fires as soon as whitespace is known (no need to wait for the next
word); `no.` with the next token unknown makes the scan wait (split invariance); list markers need following
whitespace (headings may end the line). specs/09 §5 Decision P04-T06; AGENTS bullet. Tests `tests/text/test_markdown.py`
+ `test_chunker.py` (137: golden incl. char-by-char equality, every abbreviation, no-before-number, cap, punctuation-
only drop, flush reset, chunk_stream, hypothesis concat/≤160/number/split-invariance properties), shared
`tests/support/text_helpers.split_at`. Gotcha: the Write tool turned `\uXXXX` escapes into literal chars (RUF001) —
re-escaped. Gates: fmt ok; lint 0; typecheck 0; placeholders 0; spec refs 0; make test 7030 passed / 7 skipped
(98.49 %; text/ 100 %); jscpd 0 clones; radon A (2.84); pip-audit clean; pnpm audit 3 moderate (no high).

## Review P04-T06 (Reviewer, 2026-11-01)
Approved. Re-ran: lint pass, typecheck pass, placeholders 0, make test 7030 passed / 7 skipped (98.49 %; text/ 100 %).
Checked against specs/09 §5: abbreviation set verbatim (test), min words 2/3 first/later sentence and 4/7 clause,
cap at last space before 160, punctuation-only drop, numbers never split. Probed spec-like inputs by hand (Dr./p.m.,
No. 5, ellipsis, markdown list, $1,250.50/3.14/10:30) – correct. Known limitation (consistent with the spec's
"followed by whitespace" rule, not a defect): a terminator inside closing quotes (`there." She`) is not a boundary.

## Plan P04-T07 (Planner, 2026-11-01)
Pronunciation guide applier, specs/09 §6 (consumer: specs/07 §8 step 4, chunker clause → applier → `TtsSocket.send`;
also static/cached texts; offsets feed the T08 alignment/truncation via `OffsetMap.to_original`). Pure, sync, stdlib
`re` only, no new deps. va_voice does NOT depend on va_domain/va_api: rules are accepted structurally.

### Files
1. `packages/va_voice/src/va_voice/text/pronunciation.py`:
   - `class PronunciationRuleLike(Protocol)`: read-only attributes `word: str`, `pronunciation: str`,
     `case_sensitive: bool`, `spaced: bool` (the specs/05 §4.4 pydantic `PronunciationRule` satisfies it; declare
     them as `@property` in the Protocol so frozen/pydantic models type-check).
   - `@dataclass(frozen=True, slots=True) class PronunciationRule` (same 4 fields, defaults False/False) — the
     in-package concrete type used by tests and callers without pydantic.
   - `@dataclass(frozen=True, slots=True) class Replacement`: `sent_start, sent_end, original_start, original_end`.
   - `class OffsetMap`: built from the source length, sent length and an ordered tuple of `Replacement`s.
     `to_original(index) -> int` (floor: an index strictly inside a replaced span maps to the span's
     `original_start` — a partially spoken replacement counts as not spoken; `index == sent_end` maps to
     `original_end`; outside spans identity plus the accumulated length delta), `to_original_ceil(index)`
     (inside a span → `original_end`), `replacements` property, `identity(length)` classmethod. `ValueError` for
     index < 0 or > sent length. Lookup with `bisect` on the span starts (O(log n)).
   - `@dataclass(frozen=True, slots=True) class PronouncedText`: `text` (sent), `original`, `offsets: OffsetMap`.
   - `class PronunciationApplier`:
     - `__init__(rules: Iterable[PronunciationRuleLike])`: drop rules whose `word` is empty/whitespace-only, and
       rules with an empty `pronunciation` and `spaced=False` (no-op). Stable-sort the rest by `len(word)` desc
       (ties keep input order ⇒ for a duplicate word the earlier rule wins). Build ONE combined pattern: each rule
       is a capturing group `((?<!\w)(?i:<escaped>)(?!\w))` (scoped `(?i:...)` only for case-insensitive rules,
       plain for case-sensitive), joined with `|`; keep a parallel tuple of replacement specs indexed by
       `match.lastindex - 1`. Compiled once in the constructor ("precompiled per call"). No rules → `_pattern = None`.
     - `__bool__` / `rule_count` property.
     - `apply(text: str) -> PronouncedText`: `finditer` over the text (leftmost match, alternation order = longest
       first at a given position); build the output with list joins and collect `Replacement`s. Replacement text:
       `pronunciation`, or if `spaced`: `" ".join(ch for ch in (pronunciation or matched) if not ch.isspace())`
       (empty pronunciation spaces the matched text as written). Replacements are never re-scanned (single pass).
       Fast path: no pattern or no match → `PronouncedText(text, text, OffsetMap.identity(len(text)))`.
     - `replace(text: str) -> str`: `apply(text).text` (for static/cached phrases).
   - `spell_out(text: str) -> str` module-level helper (the spaced rule), exported.
2. specs/09 §6: `> Decision (2026-11-01): P04-T07 ...` recording: rules accepted structurally; empty/whitespace
   word and empty non-spaced pronunciation rules ignored (never crash a call on persisted config); ties in length
   keep input order, so for duplicate words the earlier rule wins; overlap resolution = leftmost match first, then
   longest rule at that position ("York City" vs "New York" on "New York City" ⇒ "New York" + "City"); spaced
   drops whitespace before joining; case-insensitive matches only the rule, the `(?i:)` scope; `\w` is Unicode;
   applied per clause so a rule cannot match across a clause boundary; `OffsetMap.to_original` floor semantics
   (partially spoken replacement = not spoken) and `to_original_ceil`; billing chars = `len(PronouncedText.text)`.
3. `packages/va_voice/src/va_voice/text/__init__.py` docstring: mention pronunciation (no imports).
4. AGENTS.md bullet (P04-T07).

### Tests `packages/va_voice/tests/text/test_pronunciation.py`
- Per flag: case-insensitive default (`"sql"` rule replaces `SQL`, `Sql`); `case_sensitive=True` replaces only exact
  case; word boundaries (`"cat"` not in `concatenate`, `cat's` → replaced before `'`, `cat_x` not replaced since `_`
  is `\w`, `cat1` not replaced); words with punctuation (`"Dr."`, `"C++"`, `"#1"`) escaped and matched; `spaced`
  (`"ABC123"` → `"A B C 1 2 3"`; empty pronunciation + spaced spells the matched text keeping its case; whitespace
  in the pronunciation dropped); empty pronunciation non-spaced ignored; empty/whitespace word ignored.
- Ordering/overlap: longest first (`"New York City"` before `"New York"` regardless of input order); leftmost wins
  over longer later match (the York City case); duplicates: earlier rule wins; replacement not re-matched (rule A
  `"AI"` → `"A I"` and rule B `"A"` → `"ay"` ⇒ `"A I"` stays); mixed case-sensitivity in one pattern (a sensitive
  rule `"US"` and insensitive `"us"`-free text).
- Unicode: `"café"` matched case-insensitively (`CAFÉ`); `\w` boundary with accented neighbours.
- OffsetMap: explicit example (`"Call SQL now"` → `"Call sequel now"`): `to_original` at 0, inside the span, at span
  end, at end of text; `to_original_ceil`; out-of-range ValueError; identity map when nothing matched; `bool(applier)`
  false for no rules; `replace` equals `apply().text`.
- Hypothesis (texts from a small alphabet `"ab AB.-_é"` + words drawn from rule words; rules from short words over
  `"abAB"` with random flags):
  1. Unchanged chars map exactly: for every sent index i not inside a replacement,
     `text[i] == original[to_original(i)]`.
  2. `to_original` and `to_original_ceil` are monotone non-decreasing, `to_original(0) == 0`,
     `to_original(len(sent)) == len(original)`, floor ≤ ceil.
  3. Equivalence with a naive reference implementation in the test (scan left to right, at each position try rules
     in longest-first order with boundary checks, copy the char otherwise).
  4. No rules ⇒ identity.
- Performance smoke (not timing-based): 200 rules compile, and applying to a 160-char clause is a single pass
  (assert result correct; no timing assert).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (pronunciation.py 100 %), `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a packages/va_voice/src`,
pip-audit + pnpm audit (no new deps).

### Risks
- Python `re` group count: 200 capturing groups is fine (limit removed in 3.5); test 200 rules.
- `(?i:...)` scoped flag with lookarounds outside the scope is fine; keep lookarounds outside it.
- Protocol with `@property` members so pydantic models and frozen dataclasses both satisfy mypy.
- The Write tool may turn `\uXXXX` escapes into literal chars (RUF001): use `é` via escapes carefully or allow it in
  test data with literal chars only where RUF001 doesn't fire (é is not confusable; fine).

## Build P04-T07 (Builder, 2026-11-01)
Implemented per plan: `va_voice/text/pronunciation.py` (`PronunciationRuleLike` protocol, `PronunciationRule`,
`Replacement`, `OffsetMap` with bisect floor/ceil mapping, `PronouncedText`, `PronunciationApplier` = one combined
regex, stable longest-first, scoped `(?i:)`, `spell_out`), `text/__init__.py` docstring, specs/09 §6 Decision P04-T07,
AGENTS bullet. Spaced rule with whitespace-only pronunciation spells the matched text (decision). Tests
`tests/text/test_pronunciation.py` (31: flags, escaping, spaced, ignored rules, overlap/longest/leftmost, duplicates,
no re-match, unicode, adjacent spans, OffsetMap example, structural rules, 200 rules; hypothesis: naive reference
equivalence, offset-map monotonic/exact-char properties, identity). Note `C++C` is not a `C++` match (lookahead).
Gates: fmt ok; lint pass; typecheck pass (847 files); placeholders 0; spec refs 0; make test 7061 passed / 7 skipped
(98 %; pronunciation.py 100 %); jscpd 0 clones; radon A (2.04); pip-audit no known vulns; pnpm audit 3 moderate (no high).

## Review P04-T07 (Reviewer, 2026-11-01)
Approved. Re-ran: lint 0, typecheck 0, placeholders 0, spec refs 0, make test 7061 passed / 7 skipped (98.50 %;
pronunciation.py 100 %). Checked against specs/09 §6: pattern `(?<!\w)` + escape + `(?!\w)` per rule, IGNORECASE
scoped per rule, longest-first, single combined pass (no re-match), spaced spelling incl. empty pronunciation →
matched word; OffsetMap.to_original as the spec requires. Decision note covers ignored rules / tie order / overlaps.

## Plan P04-T08 (Planner, 2026-11-01)
Playback tracker + alignment timeline + truncation. Specs: specs/07 §7 step 6 (barge-in truncation), §9 (PlaybackTracker,
marks, LEAD_MS_ESTIMATE), specs/09 §7 (AlignmentTimeline). Pure, sync, explicit ms timestamps (like `Endpointer`: no
clock inside; callers pass `now_ms` from their injected monotonic source). No new deps. va_voice must NOT import
va_elevenlabs: alignments accepted structurally.

### Files
1. `packages/va_voice/src/va_voice/text/alignment.py` (specs/09 §7 names it `va_voice.text.alignment.AlignmentTimeline`):
   - `class ChunkAlignmentLike(Protocol)`: `@property chars: Sequence[str]`, `char_start_ms: Sequence[int]`,
     `char_duration_ms: Sequence[int]` (va_elevenlabs `ChunkAlignment` satisfies it structurally).
   - `@dataclass(frozen=True, slots=True) class CharTiming(start_ms: float, end_ms: float)`.
   - `@dataclass(frozen=True, slots=True) class SpokenPrefix(text: str, chars: int)` — `text` = original prefix with
     trailing whitespace stripped; `chars` = length of the original prefix (before rstrip? NO: after rstrip, so
     `played_chars` counts spoken chars).
   - `class AlignmentTimeline(fmt: AudioFormat)`:
     - `add_clause(clause: PronouncedText | str) -> None` — appends one clause as sent to TTS (str ⇒ identity
       offsets). Keeps per clause: sent base, original base, OffsetMap; the global sent text / original text are the
       concatenations. Visible (non-whitespace, `str.isspace`) sent chars are queued for matching.
     - `add_chunk(audio: bytes | int, alignment: ChunkAlignmentLike | None) -> None` — `chunk_ms = fmt.duration_ms(n)`.
       With alignment: for each alignment char k whose char is not whitespace: `abs_start = offset_ms + start[k]`,
       `abs_end = abs_start + duration[k]`, assigned to the next unmatched visible sent char (positional, whitespace-
       insensitive; extra alignment chars beyond the sent text are ignored). Unequal array lengths ⇒ `ValueError`.
       Without alignment: record a pending gap `[offset_ms, offset_ms + chunk_ms]` (consecutive gaps merge).
       `offset_ms += chunk_ms` always. Empty audio with no alignment is a no-op.
     - Gap resync (Decision): when an aligned chunk follows a pending gap, find the chunk's visible alignment string
       (first ≤ 8 visible chars) in the unmatched visible sent text starting at the cursor (`str.find`, limited to
       the end of the clause after the current one); the skipped chars are linearly interpolated across the gap
       (`start = gap_start + i*gap/n`, `end = start + gap/n`); if not found, the skipped count is 0 and the gap is
       dropped (positional fallback). A trailing gap (no aligned chunk after it yet) is interpolated lazily at query
       time over the remaining unmatched visible chars of the clause containing the cursor (not materialized, so a
       later aligned chunk still resyncs).
     - `offset_ms` property (total audio ms received), `sent_text`, `original_text`, `total_chars` (= len original),
       `char_end_ms(sent_index) -> float | None`.
     - `text_until(played_ms: float) -> SpokenPrefix`: s = length of the longest sent prefix whose every visible char
       has a known timing with `end_ms ≤ played_ms` (whitespace never blocks); map s to the original via the owning
       clause's `offsets.to_original` (floor: partial replacement = unspoken) + clause original base; then cut back
       to a word boundary: keep `original[:o]` if `o == len(original)` or `original[o].isspace()`, else cut to the
       last whitespace index < o (0 if none); rstrip. Monotone in played_ms. `played_ms ≤ 0` ⇒ `SpokenPrefix("", 0)`.
     - Clause boundary handling: s exactly at a clause end maps through the next clause base (prefix includes the
       whole first clause's original including its trailing space, then rstrip).
2. `packages/va_voice/src/va_voice/playback.py`:
   - `LEAD_MS_ESTIMATE: Final = 60`, `MARK_LOSS_GRACE_MS: Final = 200`, uses `audio.FRAME_MS`.
   - `mark_name(turn: int, idx: int) -> str` = `f"seg:{turn}:{idx}"`; `parse_mark(name) -> tuple[int, int] | None`
     (strict `^seg:(\d+):(\d+)$`, else None).
   - `@dataclass(frozen=True, slots=True) class Segment(idx, start_ms, end_ms, chars)`.
   - `class PlaybackTracker(turn: int, *, lead_ms: int = LEAD_MS_ESTIMATE)` per utterance:
     - `frame_sent(now_ms: float) -> None` (first call sets `t_first_frame_sent`; ignored after `stop`/`finish`
       ValueError? → after `stop`: ignored; frames after `finish`: `RuntimeError`).
     - `end_segment(chars: int) -> str` after the last frame of a segment: appends `Segment(idx=len, start=prev end,
       end=frames_sent*20, chars)`, returns the mark name to send. A segment with 0 new frames ⇒ `ValueError`.
     - `finish() -> None` — no more audio for this utterance (needed for `is_playing` end detection).
     - `on_mark(name: str) -> bool`: parse; foreign turn / unknown idx / malformed ⇒ False; else
       `confirmed_ms = max(confirmed_ms, segments[idx].end_ms)` and record the highest idx echoed; True.
     - `played_ms(now_ms) -> float` = `max(confirmed_ms, clamp((now − t_first) − lead, 0, frames_sent*20))`; 0 before
       the first frame; after `stop(at)` frozen at the value at `at`.
     - `is_playing(now_ms) -> bool`: False before first frame and after `stop`; True until `finish()` was called AND
       (last segment's mark echoed OR `(now − t_first) − lead ≥ total_ms + 200`). Without `finish()` it stays True
       (stream still producing/underrun).
     - `stop(at_ms) -> float` (barge-in): freezes and returns `played_ms(at_ms)`; idempotent (second call returns the
       first value).
     - properties: `turn`, `t_first_frame_sent: float | None`, `frames_sent`, `total_ms`, `segments` (tuple),
       `confirmed_ms`, `finished`, `stopped`, `completed(now_ms)` = finished and not playing and not stopped.
   - `@dataclass(frozen=True, slots=True) class TruncatedMessage(text: str, text_spoken: str, history_text: str,
     played_chars: int, total_chars: int, unspoken: str)`.
   - `truncate_assistant_message(timeline: AlignmentTimeline, played_ms: float) -> TruncatedMessage`: `text` = full
     planned original (stripped), `text_spoken` = `timeline.text_until(played_ms).text`, `history_text` =
     `f"{text_spoken}—"` (specs/07 §7 step 6; `"—"` alone when nothing was spoken), `played_chars`/`total_chars`
     (total = len of stripped original), `unspoken` = `original[played_chars:].strip()` (for the false-barge-in
     resume, T12).
3. `text/__init__.py` docstring: add `alignment` (§7). `va_voice/__init__.py` unchanged unless it lists modules.
4. specs/07 §9 `> Decision (2026-11-01): P04-T08 ...`: pure ms API; `end_segment` returns the mark; frames after
   `stop` ignored; mark of other turns ignored; `is_playing` requires `finish()`; `played_ms` floor of confirmed
   marks; one Framer per utterance so frames_sent×20 equals TTS audio ms except the final padded frame (timeline
   offsets come from raw chunk bytes, so they never drift). specs/09 §7 Decision: structural alignment, positional
   whitespace-insensitive matching, gap resync/interpolation rule, word boundary = whitespace in the original text,
   `SpokenPrefix.chars` excludes trailing whitespace, history `"{spoken}—"`.
5. AGENTS.md bullet (P04-T08).

### Tests
`packages/va_voice/tests/text/test_alignment.py`:
- helper building synthetic alignments: `aligned(text, ms_per_char, start_ms=0)` → (chunk bytes for its duration in
  ULAW_8000, alignment object = small local frozen dataclass), plus whitespace chars with timings (vendor sends them).
- single chunk: `text_until` at every char boundary returns whole words only ("Hello there friend": at end of "there"
  ⇒ "Hello there"; mid "friend" ⇒ "Hello there"; at total ⇒ full; 0 ⇒ "").
- chunk-relative → absolute: two chunks, second chunk's times relative ⇒ offsets added (the AGENTS gotcha).
- punctuation attached to words ("Hi, Bob." – "Hi," spoken only when ',' ended).
- multi-clause (`"Thanks for calling. "`, `"How can I help? "`) with pronunciation: clause "Call SQL now " with rule
  SQL→sequel; interrupt inside "sequel" ⇒ "Call"; after ⇒ "Call SQL".
- missing alignment: a gap chunk then aligned chunk ⇒ skipped chars interpolated linearly (assert exact interpolated
  end times via `char_end_ms`); trailing gap ⇒ remaining clause chars interpolated across it; resync not found ⇒
  positional.
- unequal array lengths ⇒ ValueError; pcm_16000 duration (32 bytes/ms) offsets.
- hypothesis: random words (letters/punct) split into random clauses and random chunking with monotone timings:
  for any played_ms, result is a whole-word prefix of the original (original.startswith(text) and next char is
  whitespace/end), monotone non-decreasing in played_ms, and equals a naive reference (scan words, keep while the
  word's last char end ≤ played_ms).
`packages/va_voice/tests/test_playback.py`:
- mark_name/parse_mark round trip + malformed names (`seg:1`, `seg:a:1`, `x:1:2`, `seg:1:2:3`).
- played_ms formula: lead 60 subtraction, clamp to 0 and to frames_sent×20, before first frame = 0.
- mark echo raises played_ms floor; foreign turn / unknown idx ignored (False).
- is_playing: false before frames; true while streaming even when estimate passes total; ends on last mark after
  finish; ends at total+200 when marks lost (and not at total+199); false after stop.
- stop freezes and is idempotent; frames after stop ignored; frame after finish ⇒ RuntimeError; empty segment ⇒
  ValueError.
- truncation at arbitrary interrupt times (acceptance): table + hypothesis over interrupt time using tracker +
  timeline built from synthetic alignments (20 ms frames fed per audio chunk): `text_spoken` always word boundary,
  `history_text` endswith "—", `played_chars ≤ total_chars`, `text_spoken + " " + unspoken` reconstructs (modulo
  whitespace) the original; full playback (mark echo) ⇒ text_spoken == text.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (new modules 100 %), `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a packages/va_voice/src`,
pip-audit + pnpm audit (no new deps).

### Risks
- Gap resync heuristics: keep bounded and deterministic; unit-test each branch for 100 % coverage.
- Floating ms: chunk_ms may be fractional (odd byte counts); use float throughout, compare with ≤.
- Keep complexity low (radon): split text_until into prefix scan / map / boundary helpers.
- mypy narrows properties across asserts: compare tuples from helper functions in tests.

## Build P04-T08 (Builder, 2026-11-01)
Implemented per plan with one addition from the plan-file deliverable (`played_text(at)`): `PlaybackTracker(turn, fmt,
lead_ms)` owns its `AlignmentTimeline` (`tracker.timeline`) and exposes `played_text(at_ms)`. Files:
`va_voice/text/alignment.py` (ChunkAlignmentLike protocol, CharTiming, SpokenPrefix, AlignmentTimeline with positional
whitespace-insensitive matching, prefix-max end bisect, gap resync via ≤8-char probe, lazy trailing-gap
interpolation, word-boundary cut), `va_voice/playback.py` (LEAD_MS_ESTIMATE, MARK_LOSS_GRACE_MS, INTERRUPTED_SUFFIX,
mark_name/parse_mark, Segment, PlaybackTracker, TruncatedMessage, truncate_assistant_message), shared test
`text_helpers.Alignment`, specs/07 §9 + specs/09 §7 Decisions P04-T08, AGENTS bullet.
Gates: fmt ok; lint pass; typecheck pass (851 files); placeholders 0; spec refs 0; make test 7121 passed / 7 skipped
(98.51 %; playback.py + alignment.py 100 %), va_voice re-run after test-helper dedupe 601 passed; jscpd 0 clones;
radon A (2.27); pip-audit no known vulns; pnpm audit no high.

## Review P04-T08 (Reviewer, 2026-11-01)
Approved. Re-ran: lint 0, typecheck 0, placeholders 0, spec refs 0, make test 7121 passed / 7 skipped (98.51 %;
playback.py + alignment.py 100 %). Checked against specs/07 §7 step 6 / §9 (played_ms formula, LEAD 60, mark floor,
is_playing until last echo or total+200, history `text_spoken—`) and specs/09 §7 (chunk-relative → absolute, sequential
whitespace-insensitive matching, OffsetMap mapping, word-boundary text_until, missing-alignment interpolation per
Decision). Pure ms API, no sleeps; hypothesis reference tests for word-boundary truncation.

## Plan P04-T09 (Planner, 2026-11-01)
Outbound pacer + background mixer + `noise_cancellation` VAD-path gate. Specs: specs/07 §9 (OutboundClock: drift-
corrected targets, idle frames, lead ≤ 3 frames, lead reset after clear, marks after a segment's last frame, 100005
backoff), §10.1 (SpectralGate), §10.2 (tracks, levels, unit-gain mixing, clip, no ducking, outbound only), §15
(hd: PCM16LE TTS in, L16 wire byte order out), §21/§22. No new deps (numpy only). va_voice stays transport-agnostic.

### Files
1. `packages/va_voice/src/va_voice/background.py`
   - `BackgroundTrack = Literal["office", "cafe", "restaurant", "none", "phone_static"] | None`.
   - `resolve_background(track: str | None) -> str | None`: `None`/`"phone_static"` → `"static"` (org default name,
     specs/04 §7), `"none"` → `None`, office/cafe/restaurant → itself, anything else `ValueError`.
   - `class BackgroundAssetError(Exception)`.
   - `class BackgroundLoop(samples: NDArray[np.int16], frame_samples: int)`: read-only loop, `next_frame() ->
     NDArray[np.int16]` (wraps around the seam, copies across the seam only), `position` (sample index), `reset()`;
     empty samples or frame_samples ≤ 0 → ValueError.
   - `class BackgroundLibrary(assets_dir: Path)`: `async loop(track: str | None, fmt: AudioFormat) ->
     BackgroundLoop | None` (None for `"none"`); 8 kHz formats read `<name>.ulaw` (ulaw_decode), 16 kHz read
     `<name>.pcm16k` (LE int16); decoded arrays cached per (name, rate) and shared (`flags.writeable=False`), each
     call returns a fresh loop (own position); file read + decode in `asyncio.to_thread`; missing/empty/odd-length
     file → `BackgroundAssetError`. Path comes from the caller (media worker passes `VA_ASSETS_DIR`/`assets` later).
2. `packages/va_voice/src/va_voice/outbound.py`
   - Constants: `LEAD_FRAMES: Final = 3`, `THROTTLED_LEAD_FRAMES: Final = 1`, `RATE_LIMIT_PAUSE_MS: Final = 100`,
     `RATE_LIMIT_WINDOW_MS: Final = 5000`, `MAX_CATCHUP_FRAMES: Final = 5`.
   - `class FrameMixer(fmt: AudioFormat, *, background: BackgroundLoop | None = None, gain_db: float = 0.0,
     wire_byte_order: L16ByteOrder = "big")`: `fmt` is the TTS format (`ULAW_8000` → μ-law wire; `PCM16LE_16000` →
     L16 wire in `wire_byte_order`; other formats ValueError). `speech(frame: bytes) -> bytes` (len must equal
     `fmt.frame_bytes`, else ValueError): no background + μ-law ⇒ exact passthrough (no decode); otherwise decode →
     int32 add `bg × gain` → clip int16 → encode. `idle() -> bytes | None`: background-only frame, `None` when no
     background. The background advances one frame per call of either method (continuous bed). `has_background`.
   - `class FramePacer(*, frame_ms=20, lead_frames=LEAD_FRAMES)` (pure, ms): anchor `t0`, `sent` since anchor.
     `next_send_ms(now) -> float`: if not anchored or Telnyx has drained (`now ≥ t0 + sent×20`) ⇒ `now` (re-anchor
     happens in `on_sent`); else `max(t0 + (sent − lead)×20, paused_until)`. `on_sent(now)`: re-anchor when needed
     (`t0 = now, sent = 0`), and when `now − target > MAX_CATCHUP_FRAMES×20` (loop stalled) re-anchor too (no burst);
     `sent += 1`; returns lateness ms (`now − target`, ≥ 0). `reset()` (after clear: Telnyx buffer empty, lead
     restarts at 0 ⇒ next frame immediately, burst of up to `lead` frames again). `throttle(now)`: pause until
     now+100, lead = 1 until now+5000 (then back to configured). Lead in effect: `lead_frames_at(now)`.
   - `class OutboundSink(Protocol)`: `async send_audio(frame: bytes) -> None`, `async send_mark(name: str) -> None`
     (T12 Transport satisfies it; base64/JSON is the transport's job).
   - `@dataclass OutboundStats`: `frames_sent`, `speech_frames`, `idle_frames`, `marks_sent`, `late_frames`
     (lateness > 0 ms... use > `LATE_THRESHOLD_MS = 30`, specs/07 §21), `max_lateness_ms`, `dropped_frames`.
   - `class OutboundAudio(sink, mixer: FrameMixer, *, clock: Clock, pacer: FramePacer | None = None)`:
     - `start_utterance(tracker: PlaybackTracker)`: subsequent audio belongs to it (ValueError if tracker.format ≠
       mixer fmt is not needed — skip; RuntimeError if an utterance is open and not finished).
     - `push_audio(chunk: bytes)`: through an internal `Framer` into the queue (requires open utterance).
     - `end_segment(chars: int)`: queue a segment-end item; when the clock reaches it right after sending the
       segment's last frame (same tick) it calls `tracker.end_segment(chars)` and `sink.send_mark(name)`. A segment
       with no frames since the previous one carries its chars into the next segment end (never an empty segment).
     - `finish_utterance()`: flush the Framer (pad), close the trailing segment (carried chars), queue finish →
       `tracker.finish()` after the last mark. Utterance closed for new audio.
     - `clear() -> int`: drop queued frames + Framer + control items, detach the tracker (caller calls
       `tracker.stop`), `pacer.reset()`, `stats.dropped_frames += n`, return n (sync; barge-in, sends nothing —
       transport.clear is the pipeline's call, T12).
     - `rate_limited()`: `pacer.throttle(now)` (Telnyx 100005).
     - `run()`: loop until cancelled: when the queue is empty and the mixer has no background, wait on an
       `asyncio.Event` (set by push/finish/end_segment); else `target = pacer.next_send_ms(now)`; if target > now →
       `await clock.sleep((target − now)/1000)` and re-check (a clear during the sleep is respected); else pop a
       speech frame (`mixer.speech`) or build `mixer.idle()`; `await sink.send_audio(frame)`;
       `lateness = pacer.on_sent(now)`; speech frames call `tracker.frame_sent(now)`; then process control items at
       the head (marks/finish) in order. Time in ms from `clock.monotonic() × 1000`.
     - properties `buffered_frames`, `buffered_ms`, `idle` (no queued speech), `stats`, `tracker`.
3. `packages/va_voice/src/va_voice/noise.py` — `SpectralGate` (16 kHz int16 streaming):
   - Constants: `FFT_SIZE = 512`, `HOP = 256`, `GAIN_FLOOR = 0.25`, `OVER_SUBTRACTION = 1.5`, `NOISE_PERCENTILE = 20`,
     `PROFILE_WINDOW_S = 1.5` (⇒ 94 frames), `SMOOTHING = 0.6`, `MIN_PROFILE_FRAMES = 16` (0.25 s), `LATENCY_MS = 16`,
     `PERCENTILE_TO_MEAN = sqrt(pi/2) / sqrt(-2 ln 0.8)` (≈ 1.876, Rayleigh mean / p20).
   - Periodic Hann analysis window (sums to 1 at 50 % overlap ⇒ plain overlap-add reconstructs exactly with g = 1).
   - `process(pcm16k: NDArray[int16]) -> NDArray[int16]`: any chunk size, output has exactly `len(input)` samples,
     delayed by 256 samples (first 256 output samples are zeros); `set_speech(active: bool)` (VAD feedback: profile
     ring updated only while False); `reset()`; `profile_ready`.
   - Per frame: `mag = |rfft(frame × w)|`; if not speech append mag to a ring (94); profile =
     `PERCENTILE_TO_MEAN × percentile(ring, 20, axis=0)` recomputed only when the ring changed; until
     `MIN_PROFILE_FRAMES` frames: g = 1; `g_raw = max(0.25, 1 − 1.5 × noise / max(mag, ε))`;
     `g = 0.6 × g_prev + 0.4 × g_raw`; `irfft(X × g)` overlap-added; output rounded + clipped to int16.
   - Pure CPU, vectorised; caller runs it on the VAD path (T12 wires it in and feeds VAD events to `set_speech`).
4. `va_voice/__init__.py` docstring if it lists modules (check). specs/07 Decisions (2026-11-01) P04-T09:
   - §9: `FramePacer`/`OutboundAudio` semantics above (targets from anchor via the injected Clock's monotonic, lead =
     burst of up to 3 frames after an (re)anchor = Telnyx queue empty, drained/idle ⇒ re-anchor, stall >5 frames ⇒
     re-anchor instead of a burst, marks in the same tick as the last frame, empty segments merged, clear is local,
     late frame = > 30 ms past target).
   - §10.1: the spec formula with the raw 20th percentile only reaches ≈ −8 dB on stationary Gaussian noise (Rayleigh
     magnitudes: p20 ≈ 0.53 × mean), short of §22's ≥ 10 dB; the profile is therefore the p20 scaled by the
     Rayleigh mean/p20 ratio 1.876 (≈ −11.6 dB measured); smoothing is `g = α g_prev + (1−α) g_raw` with α = 0.6;
     unity gain until 0.25 s of non-speech; output length = input length, 256-sample delay.
   - §10.2: `phone_static` (org default enum) ≡ null ⇒ `static`; `BackgroundLibrary` file choice per rate; gain 0 dB.
5. AGENTS.md bullet (P04-T09).

### Tests (`packages/va_voice/tests/`)
- `test_background.py`: resolve table (+ ValueError); library loads every track for ULAW_8000 and PCM16LE_16000 from
  repo `assets/audio` (path via `Path(__file__).parents[3] / "assets" / "audio"`), cache shares the array (`is`) but
  loops have independent positions; `"none"` → None; missing/odd-length file → BackgroundAssetError (tmp_path);
  loop wraps exactly at the seam (concatenated frames over 2 loops == tile of samples); level: RMS of a full loop of
  idle frames for office/cafe/restaurant −22 ± 1 dBFS and static −40 ± 1 (both formats) — acceptance ±1 dB.
- `test_outbound_mixer.py`: μ-law passthrough identity (bytes object equal, no background); mix = clip(speech + bg)
  for PCM16LE → L16 big and little byte orders (exact); μ-law mixed decode within 1 quantization step of the
  int16 sum; clipping at ±32767/−32768; gain_db −6 halves bg (≈); mixed speech+office RMS within 1 dB of the power
  sum of the parts; wrong frame length ValueError; unsupported format ValueError; idle() None without background.
- `test_outbound_pacer.py` (pure FramePacer, explicit ms): first 3 frames immediately then every 20 ms (targets
  0,0,0,0?? → frames 0..3 at t0 since lead 3 means frame n due at t0+(n−3)×20 for n≥3: n=0..3 at t0, n=4 at t0+20);
  no drift: targets depend on anchor only (simulate jittery wake-ups, the 1000th target is exactly t0 + 997×20);
  drained ⇒ re-anchor; stall > 100 ms ⇒ re-anchor (no burst), lateness reported; reset ⇒ immediate next send;
  throttle ⇒ pause 100 ms then lead 1 for 5 s then back to 3.
- `test_outbound_audio.py` (async, `ManualClock` from clock_helpers, recording sink with `(t_ms, kind, value)`):
  utterance of 10 frames: first 3 sent at t=0 and rest at 20 ms cadence (`wait_for_sleep`/`run_for`), mark
  `seg:1:0` sent right after the segment's last frame and before any later frame, tracker `frames_sent`,
  `segments`, `finished`; arbitrary chunk sizes re-framed (padded last frame on finish); empty segment chars carry
  forward; with background: idle frames every 20 ms before/after speech, speech frames mixed; without background:
  no frames while idle (loop parks on the event, no sleeper), speech after idle re-anchors (burst again);
  `clear()` mid-utterance drops the rest (returned count, dropped_frames), no further speech frames or marks of that
  utterance, next utterance starts with a burst (lead reset); rate_limited pauses 100 ms; push without utterance
  RuntimeError; stats (speech/idle/marks, late_frames 0 with ManualClock); cancellation of `run()` is clean.
- `test_noise.py`: unity reconstruction (no profile / speech flag on) output == input delayed by 256 (±1 LSB);
  output length == input length for random chunk splits (hypothesis or param list) and chunking-invariant output;
  stationary noise (PCG64 Gaussian, seeded via `numpy.random.PCG64(seed).random_raw()` or default_rng is fine in
  tests — not committed data) reduced ≥ 10 dB after the profile window (measure 1.5 s → end); profile not updated
  while speech flag on (loud tone during speech is not learned: gain stays 1 for it afterwards… assert profile array
  unchanged); reset clears; real-model recall: speech fixture (`read_fixture("speech.wav")`) + stationary noise at
  −40 dBFS (its static gaps already noisy), gate in front of `SileroVad` with VAD events driving `set_speech`, speech
  recall vs `SPEECH_SEGMENTS_MS` ≥ 95 % (shift detections by LATENCY_MS); module skips like test_vad_silero when
  the model is absent (reuse its fixtures/conftest pattern).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (new modules 100 %), `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a packages/va_voice/src`,
pip-audit + pnpm audit (no new deps).

### Risks
- ManualClock + event waits: assert with `wait_for_sleep` before `run_for`; the no-background idle path must park
  on the Event (no zero-length sleeps → no busy loop).
- Per-frame CPU: mixing ≈ µs; gate ≈ one rfft/irfft per 16 ms — fine; percentile only when the ring changes.
- jscpd: share a recording sink/helpers in `tests/support/` if two test modules need them.
- μ-law level of static at −40 dBFS is near μ-law quantization: the asset was levelled per format already (decision
  2026-10-25), so ±1 dB holds; if not, measure and report rather than loosening.

## Build P04-T09 (Builder, 2026-11-01)
Implemented per plan with these deviations (all recorded in specs/07 §9/§10.1/§10.2 Decisions P04-T09):
- `FramePacer`: no separate MAX_CATCHUP stall rule. With lead 3, a frame more than 60 ms late already means Telnyx
  has drained, so the drained re-anchor covers stalls. Burst targets are clamped at the anchor (`max(0, n - lead)`),
  so burst frames are not counted late. `mark_gap()` (called when the loop parks with nothing to send) means the
  next frame's wait is not counted as lateness.
- `FrameMixer.idle()` raises RuntimeError when there is no background (check `has_background` first) instead of
  returning None, which removes a dead branch in the loop.
- `SpectralGate.process` returns whole 256-sample hops: the output is the input delayed by exactly 256 samples, but
  each call's output is not the same length as its input. Exact per-call length would need a 512-sample delay;
  SileroVad accepts any chunk size.
- A partial frame stays in the Framer across segment boundaries (no padding until finish_utterance), so a mark can
  trail its clause by up to 20 ms. clear() uses a generation counter, so a frame that was in flight during the clear
  does not count.
Files: va_voice/{background,outbound,noise}.py; tests test_{background,outbound_mixer,outbound_pacer,outbound_audio,noise}.py;
specs/07 Decisions; AGENTS bullet.
Measured: gate -11.6 dB on -30 dBFS Gaussian noise, 0.49 ms CPU per 20 ms frame. Gated Silero finds both fixture
segments, recall 100 %. Asset levels are within 0.13 dB of target in both formats.
Gates: fmt ok; lint pass; typecheck pass (859 files); placeholders 0; spec refs 0; va_voice 679 passed (the 3 new
modules at 100 %); make test 7199 passed / 7 skipped (98.54 %); jscpd 0 clones; radon A (2.10 va_voice); pip-audit
no known vulns; pnpm audit: 3 moderate, no high.

## Review P04-T09 (Reviewer, 2026-11-01)
Approved. Re-ran: lint 0, typecheck 0, placeholders 0, spec refs 0, make test 7199 passed / 7 skipped (98.54 %),
jscpd 0 clones, radon A (2.01 on the new modules). Checked against specs/07 §9 (≤ 3 frames ahead of playback, lead
reset after clear, mark after a segment's last frame, 100005 pause 100 ms + lead 1 for 5 s), §10.1 (STFT 512/256
Hann, p20 profile over 1.5 s non-speech, floor 0.25, α 0.6; Rayleigh scale recorded as a Decision for §22 ≥ 10 dB),
§10.2 (asset per rate, unit gain, clip, no ducking, outbound only), §15 (L16 wire byte order). Tests run on
ManualClock with events (no sleeps); clear-in-flight and stopped-tracker paths covered; real-Silero recall test.

## Plan P04-T10 (Planner, 2026-11-01)
Latency tracer + turn metrics. Specs: specs/07 §8 step 7, §20 (metric table, span tree, histograms); specs/27 §2.1
(`turn`, `turn.eot`, `turn.stt_final`, `turn.route`, `turn.tool <name>`), §3.3 catalog, §4 (latency events);
specs/19 §2 `call.latency`; specs/03 `call_turns.latency` jsonb keys. va_voice deps unchanged (va_common already brings
opentelemetry-api/sdk + prometheus_client). va_voice must NOT import va_events/va_llm/va_elevenlabs.

### Key decisions (record in specs/07 §20 as `> Decision (2026-11-01) P04-T10`)
1. Histogram names follow the specs/27 §3.3 catalog: `va_turn_eot_ms` (07 §20 says `va_eot_ms`; catalog wins, fix the
   07 text in the decision).
2. No double counting: `va_stt_final_ms`, `va_tts_ttfa_ms`, `va_llm_ttft_ms` are already observed by the vendor clients
   (va_elevenlabs CatalogSttMetrics/CatalogTtsMetrics, va_llm LlmMetrics — they own the `model`/`purpose` labels).
   `TurnMetrics` observes only `va_turn_e2e_ms{org_tier}`, `va_turn_eot_ms`, `va_barge_in_total{honored}`,
   `va_barge_in_reaction_ms`.
3. `stt_final_ms` starts at the commit (sent at the EOT decision, so 27 §4's "EOT decision →" is the same instant
   when the commit is immediate); mark `stt_commit`.
4. Spans are emitted retroactively when the turn finishes, with explicit start/end timestamps: monotonic ms →
   epoch ns through an anchor `(clock.now(), clock.monotonic())` taken when the tracer is built. Child names
   `turn.eot`, `turn.stt_final`, `turn.route`, `turn.llm`, `turn.tts_first_audio`, `turn.tool <name>`; add the two
   missing rows (`turn.llm` attrs `va.llm_ttft_ms`; `turn.tts_first_audio` attrs `va.tts_ttfa_ms`) to specs/27 §2.1.
5. Marks are first-wins except `user_speech_end` (last voiced window end; resumed speech moves it: last-wins) and
   `user_speech_start` (first-wins). A metric is `None` when a mark is missing or end < start (misordered input logs
   `turn_latency_misordered` with the metric name, debug-free warning once per metric). Values rounded half-up to int ms.
6. Summary event `"E2E: {e2e}ms"` only when `e2e_ms` is measured (interrupted-before-audio turns have no summary);
   stage events `"STT: {n}ms"`, `"LLM: {n}ms"`, `"TTS: {n}ms"` each only when measured. Order: summary, STT, LLM, TTS.

### Files
1. `packages/va_voice/src/va_voice/latency.py`
   - `class TurnMark(StrEnum)`: `USER_SPEECH_START="user_speech_start"`, `USER_SPEECH_END`, `EOT`, `STT_COMMIT`,
     `STT_FINAL`, `ROUTE_START`, `ROUTE_END`, `LLM_REQUEST`, `LLM_FIRST_TOKEN`, `TTS_FIRST_TEXT`, `TTS_FIRST_AUDIO`,
     `FIRST_AUDIO_OUT`.
   - `METRIC_SPANS: Final` table: metric name → (start mark, end mark, child span name, attribute key):
     eot_ms (USER_SPEECH_END→EOT, `turn.eot`, `va.eot_ms`), stt_final_ms (STT_COMMIT→STT_FINAL, `turn.stt_final`,
     `va.stt_final_ms`), route_ms (ROUTE_START→ROUTE_END, `turn.route`, `va.route_ms`), llm_ttft_ms
     (LLM_REQUEST→LLM_FIRST_TOKEN, `turn.llm`, `va.llm_ttft_ms`), tts_ttfa_ms (TTS_FIRST_TEXT→TTS_FIRST_AUDIO,
     `turn.tts_first_audio`, `va.tts_ttfa_ms`), e2e_ms (USER_SPEECH_END→FIRST_AUDIO_OUT, no child; on `turn` as
     `va.e2e_ms`).
   - `@dataclass(frozen=True) ToolTiming(name, start_ms, end_ms, status_code: int | None)`; `duration_ms`.
   - `@dataclass(frozen=True) TurnLatency`: `eot_ms, stt_final_ms, route_ms, tool_ms, llm_ttft_ms, tts_ttfa_ms,
     e2e_ms: int | None`; `as_dict() -> dict[str, int]` (measured keys only, spec key order — this is the
     `call_turns.latency` jsonb); tool_ms = sum of tool durations (None when no tools).
   - `class TurnTimer(turn_index: int, clock: Clock, *, node_id: str | None = None)`: `mark(m, at_ms: float | None =
     None)` (default `clock.monotonic()*1000`; same ms timeline as the endpointer/pacer callers), `at(m) -> float |
     None`, `add_tool(name, start_ms, end_ms, status_code=None)`, `set_node(node_id)`, `interrupted` (settable via
     `mark_interrupted()`), `latency() -> TurnLatency`, `started_ms` (first mark seen; span start = min of all marks),
     `ended_ms` (max), properties `turn_index`, `node_id`, `tools`.
   - `@dataclass(frozen=True) LatencyEvent(message: str, data: dict[str, int])`; `latency_events(latency, turn_index)
     -> list[LatencyEvent]` (summary data = `as_dict()` + `turn_index`; stage events data `{"<metric>": n,
     "turn_index": i}`).
   - `class TurnMetrics(metrics: ServiceMetrics, *, org_tier: str)`: `observe(latency)` (e2e with label, eot),
     `barge_in(*, honored: bool, reaction_ms: float | None)` (counter label `"true"/"false"`, histogram only when
     honored and reaction given).
   - `class TurnTracer(clock: Clock, *, call_id: str, org_id: str, tracer_provider: TracerProvider | None = None,
     parent: Context | None = None)`: `emit(timer)`: no marks → nothing; `turn` span from min to max mark with
     `va.turn_index`, `va.node_id` (when set), `va.interrupted`, `va.e2e_ms` (when measured), `va.call_id`,
     `va.org_id`; children per measured metric with its attribute (+ `va.chars`? no — keep metric attrs only, plus
     `turn.tool <name>` with `va.tool.duration_ms`, `http.status_code` when given). Uses
     `trace.get_tracer("va_voice", tracer_provider=...)`, `start_span(name, context=..., start_time=ns)` +
     `span.end(end_time=ns)`; children parented with `trace.set_span_in_context(turn_span)`.
   - `class LatencyTracer(clock, *, metrics: TurnMetrics | None = None, tracer: TurnTracer | None = None)`:
     `start_turn(turn_index, node_id=None) -> TurnTimer`; `finish(timer) -> TurnReport(latency, events)` (observes
     metrics, emits spans, builds events; idempotent: second finish of the same timer raises RuntimeError);
     `barge_in(...)` delegates.
2. specs/07 §20 Decision; specs/27 §2.1 two rows (`turn.llm`, `turn.tts_first_audio`), keep table format.
3. AGENTS.md bullet (P04-T10).

### Tests `packages/va_voice/tests/test_latency.py` (FrozenClock / ManualClock from clock_helpers; no sleeps)
- metric arithmetic table: full mark set → exact ints for every metric; rounding half-up (e.g. 12.5 → 13);
  missing marks → None; misordered → None + warning logged (json_log_stream/log_events or structlog capture).
- `user_speech_end` last-wins, other marks first-wins; default timestamp from clock.monotonic (advance clock).
- tool_ms sum of two tools, None without tools; `as_dict` key order and omission.
- `latency_events`: messages exactly `"E2E: 742ms"`, `"STT: 63ms"`, `"LLM: 266ms"`, `"TTS: 218ms"`; summary data
  keys; no summary without e2e; stage events omitted when unmeasured.
- TurnMetrics with `build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))`: sample counts/sums
  via `registry.get_sample_value(...)` for e2e (org_tier label), eot; stage histograms untouched (stt/llm/tts
  `_count` stay 0 — proves no double count); barge_in honored/not.
- TurnTracer with `obs_helpers.memory_provider()` (or local InMemorySpanExporter + SimpleSpanProcessor
  TracerProvider): one `turn` span + children names, parent ids, attributes, start/end ns equal anchor + offsets
  exactly; parent context respected (children of a `media.session` span); no marks → no spans; tool span attrs.
- LatencyTracer.finish: report equals parts, double finish RuntimeError, works with metrics/tracer None.
- Coverage 100 % for latency.py.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q`, `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src/va_voice/latency.py`, pip-audit/pnpm audit (no new deps).

### Risks
- OTel timestamps are int ns: compute `anchor_ns + round((ms - anchor_mono_ms) * 1e6)` to avoid float noise; tests
  assert exact ints.
- `test_metrics` catalog parity unaffected (no new metrics). If the specs/27 §2.1 table is parsed anywhere, keep
  row format.
- va_voice tests may need `obs_helpers`/`clock_helpers` on pythonpath — already shared via packages/va_common/tests
  (check pytest pythonpath; existing va_voice outbound tests already import clock_helpers).

## Build P04-T10 (Builder, 2026-11-01)
Implemented per plan. Deviations/additions (recorded in specs/07 §20 Decision P04-T10):
- `TurnTimer.intervals()` computes the measured (start, end) once per finish; `latency(intervals)` and
  `TurnTracer.emit(timer, intervals)` reuse it, so a misordered metric logs `turn_latency_misordered` exactly once.
- `TurnTimer.annotate(span, {...})` adds caller-known catalog attributes (`va.chars`, `va.silence_threshold_ms`,
  `va.route.to`, ...) to `turn` or a child; ignored when that span is not emitted.
- Child spans carry their metric attribute (`va.route_ms` on `turn.route` too); specs/27 §2.1 gained `turn.llm`
  and `turn.tts_first_audio` rows.
- New `packages/va_voice/tests/conftest.py` re-exports `reset_logging` (json_log_stream in tests).
Files: va_voice/latency.py; tests/test_latency.py, tests/conftest.py; specs/07 §20, specs/27 §2.1; AGENTS bullet.
Gates: fmt ok; lint pass; typecheck pass (862 files); placeholders 0; spec refs 0; test_latency 25 passed,
latency.py 100 %; make test 7224 passed / 7 skipped (98.55 %); jscpd 0 clones; radon A (2.26); pip-audit no known
vulns; pnpm audit 3 moderate, no high.

## Review P04-T10 (Reviewer, 2026-11-01)
Approved. Re-ran: lint 0, typecheck 0, placeholders 0, spec refs 0, make test 7224 passed / 7 skipped (98.55 %),
jscpd 0 clones, radon A (2.26). Checked against specs/07 §20 metric table (marks match start/end columns),
specs/27 §2.1/§3.3/§4, specs/03 call_turns.latency key order, specs/19 call.latency messages. No sleeps; spans
asserted with exact ns timestamps; misordered path logged once and covered.

## Plan P04-T11 (Planner, 2026-11-01)
Phrase cache. Specs: specs/09 §9 (request, key, value JSON, 30 d / 1 h TTL, 2 MB cap, prefetch, fallback to socket),
§14 (key stability, TTL, fallback); specs/03 Redis row `tts:cache:*`; specs/27 §3.3 `va_tts_cache_hits_total` /
`va_tts_cache_misses_total` (already in the media-worker catalog, no labels).

### Placement decision (record in specs/09 §9 as `> Decision (2026-11-01) P04-T11`)
`va_voice.phrase_cache` (phase scope lists `phrase_cache` in va_voice). va_voice must NOT import va_elevenlabs
(specs/09 §7 Decision), so the cache depends on a structural Protocol that `va_elevenlabs.ElevenLabsRest` satisfies
as-is (`tts_with_timestamps`). Add `va-domain` to va_voice deps (for `VoiceSettings`; pydantic only) +
`[tool.uv.sources] va-domain = { workspace = true }`; run `uv lock`/`uv sync`.
Other decisions for the same note:
1. Key = `tts:cache:{voice_id}:{sha256("model|format|lang|settings_json|text")}` hex; `lang` = `""` when None;
   `settings_json` = `orjson.dumps(settings.model_dump(mode="json"), option=OPT_SORT_KEYS)` (all fields incl.
   defaults, so a partial VoiceSettings and the explicit-default one share a key); `"-"`? no — None settings
   ⇒ `VoiceSettings()` defaults (same as vendor default). Update the specs/03 row to this exact key and value
   (`string JSON {"audio_b64","alignment","duration_ms"}`, 30 d, per-call 1 h) in the same commit.
2. Alignment stored = the response `alignment` (original text, not `normalized_alignment`) converted to integer ms:
   `{"chars": [...], "char_start_ms": [...], "char_duration_ms": [...]}` (round half-up of seconds×1000, duration =
   max(0, end−start)); null when the vendor sent none. `duration_ms` = len(audio) / bytes_per_ms of the format.
3. Formats: only `ulaw_8000` and `pcm_16000` (ValueError otherwise) — use va_voice `ULAW_8000` / `PCM16LE_16000`
   AudioFormat (name = vendor output_format).
4. Values > 2 MB (`MAX_VALUE_BYTES = 2 * 1024 * 1024`, encoded JSON length) are returned but not stored
   (warning log `tts.phrase_cache_oversize`).
5. Redis failures fail open (log `tts.phrase_cache_unavailable` operation get/set, render without cache, no raise);
   a malformed cached value counts as a miss and is overwritten.
6. REST failure (any `ElevenLabsError`-like exception — catch `Exception` subclasses from the renderer except
   `asyncio.CancelledError`) raises `PhraseUnavailableError(text, cause)`; the caller (P04-T12) falls back to the
   live socket. Not cached.
7. Single flight: concurrent misses of one key in one process share one render (dict key → `asyncio.Future`;
   removed when done; a failure propagates to every waiter; a cancelled first caller does not cancel others —
   run the render in a task owned by the cache, shielded; pending tasks cancelled by `aclose()`).
8. Hit/miss metrics: hit = served from Redis; miss = rendered (one miss per render, waiters joining a flight count
   as hits? no — count each get_or_render call: hit when Redis served it, else miss). Keep simple: per call.

### Files
1. `packages/va_voice/src/va_voice/phrase_cache.py`
   - Protocols: `RestAlignmentLike` (`characters: Sequence[str]`, `character_start_times_seconds`,
     `character_end_times_seconds: Sequence[float]`), `TimestampedAudioLike` (`audio: bytes`, `alignment:
     RestAlignmentLike | None`), `TimestampedSynthesizer` with `async def tts_with_timestamps(self, voice_id: str,
     text: str, *, model_id: str, output_format: str, voice_settings: VoiceSettings | None = ..., language_code:
     str | None = ..., apply_text_normalization: Literal["auto","on","off"] = ...) -> TimestampedAudioLike`.
     (Verify mypy accepts `ElevenLabsRest` for it in a test: `cache = PhraseCache(sim.client, ...)` typed.)
     Use read-only `@property` members in the result protocols so frozen dataclasses / pydantic models match.
   - `@dataclass(frozen=True, slots=True) PhraseVoice(voice_id: str, model_id: str, settings: VoiceSettings,
     language_code: str | None = None)`.
   - `@dataclass(frozen=True, slots=True) PhraseAlignment(chars: tuple[str, ...], char_start_ms: tuple[int, ...],
     char_duration_ms: tuple[int, ...])` — satisfies `va_voice.text.alignment.ChunkAlignmentLike` (absolute = chunk
     relative, one chunk).
   - `@dataclass(frozen=True, slots=True) CachedPhrase(text, fmt: AudioFormat, audio: bytes, alignment:
     PhraseAlignment | None, duration_ms: int, cached: bool)` (`cached` = served from Redis).
   - Constants `SHARED_TTL_S = 30*86400`, `PER_CALL_TTL_S = 3600`, `MAX_VALUE_BYTES`, `STANDARD_FILLERS = ("Sure.",
     "Got it.", "One moment.", "Let me check that for you.")` (spec §9 list verbatim; test parses the §9 line).
   - `phrase_cache_key(voice: PhraseVoice, text: str, fmt: AudioFormat) -> str` (pure).
   - `encode_entry(phrase) -> bytes` / `decode_entry(raw, text, fmt) -> CachedPhrase | None` (pure; None for
     malformed JSON, wrong types, unequal alignment arrays, bad base64).
   - `class PhraseCacheMetrics` protocol-free small class: `PhraseCacheMetrics(metrics: ServiceMetrics)` with
     `hit()`/`miss()` on `va_tts_cache_hits_total`/`va_tts_cache_misses_total` (check how `ServiceMetrics` exposes
     counters — follow `reorder_metrics.CatalogReorderMetrics`); optional (None = no metrics).
   - `class PhraseUnavailableError(Exception)` with `.text`, `__cause__` set.
   - `class PhraseCache(synth: TimestampedSynthesizer, redis: Redis, *, metrics: PhraseCacheMetrics | None = None)`:
     `async get_or_render(voice, text, fmt, *, ttl_s: int = SHARED_TTL_S) -> CachedPhrase`;
     `async prefetch(voice, fmt, texts: Iterable[str] = STANDARD_FILLERS, *, ttl_s=SHARED_TTL_S) -> dict[str,
     CachedPhrase | None]` (gather; failures map to None, logged `tts.phrase_prefetch_failed`); `async aclose()`.
     Rendering = `synth.tts_with_timestamps(voice.voice_id, text, model_id=voice.model_id,
     output_format=fmt.name, voice_settings=voice.settings, language_code=voice.language_code,
     apply_text_normalization="auto")`. Empty/whitespace text ⇒ ValueError.
2. `packages/va_voice/pyproject.toml` (+va-domain); `uv.lock`.
3. specs/09 §9 Decision; specs/03 Redis row; AGENTS.md bullet (P04-T11).

### Tests
- `packages/va_voice/tests/test_phrase_cache_unit.py` (fakeredis `FakeAsyncRedis`, a scripted fake synthesizer
  recording calls, returning frozen dataclasses with alignment seconds):
  key stability (golden hex for a fixed input computed independently in the test with hashlib from the documented
  string), key changes with each of model/format/lang/each settings field/text/voice_id, partial vs explicit-default
  settings same key; miss renders + stores with TTL 30 d (`redis.ttl` ≈ 2 592 000) and second call is a hit with no
  synth call (`cached=True`, identical audio/alignment); per-call ttl 3600; alignment ms conversion (rounding,
  duration from end−start, None alignment stored as null); duration_ms for ulaw (8 bytes/ms) and pcm_16000 (32);
  unsupported format ValueError; empty text ValueError; malformed cached value ⇒ miss and overwritten; oversize ⇒
  returned, not stored, warning logged; Redis down (fakeredis `server.connected = False`) ⇒ rendered, no raise,
  warnings for get and set; synth raises ⇒ PhraseUnavailableError with cause, nothing stored; single flight:
  10 concurrent get_or_render for one key with a synth gated by an Event ⇒ one synth call, all equal; a cancelled
  first caller still lets the others finish; failure propagates to all waiters; aclose cancels pending renders;
  metrics hits/misses counts via `build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))`
  + `registry.get_sample_value`; prefetch returns all fillers, failures → None + log; STANDARD_FILLERS equals the
  quoted list in the specs/09 §9 paragraph (parse it); cached alignment feeds `AlignmentTimeline.add_chunk` and
  `text_until(duration_ms)` returns the full text (integration with T08).
- `packages/va_voice/tests/test_phrase_cache_sim.py` (FakeElevenLabs via `rest_client_helpers.sim_rest()`, voice
  `tts_helpers.VOICE_ID`/`KAREN_VOICE_ID`, model from the sim catalog e.g. `eleven_flash_v2_5`): miss then hit
  asserted via the sim's request log (`await sim.records("/sim/elevenlabs/tts/requests", "requests")` count stays 1
  after the hit — check the actual record path/key in va_sim control), audio non-empty μ-law with duration matching
  len/8, alignment chars == text chars; different settings ⇒ second request; sim fault (`/sim/elevenlabs/tts/fault`
  or REST error status — check the fault body shape) ⇒ PhraseUnavailableError. Also asserts `ElevenLabsRest` is
  accepted for the protocol (mypy).
Use fakeredis for Redis in both. No sleeps; events for gating.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (phrase_cache.py 100 %), `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src/va_voice/phrase_cache.py`, pip-audit + pnpm audit (no new third-party deps).

### Risks
- Protocol vs `ElevenLabsRest.tts_with_timestamps` signature: parameter names/defaults must match exactly
  (keyword-only after `*`); result `TtsTimestamped.alignment` is `RestAlignment | None` (pydantic model) — use
  property-based protocols so list[str] attrs satisfy Sequence. If mypy rejects, adapt the protocol, not va_elevenlabs.
- Adding va-domain to va_voice must not create an import cycle (va_domain imports nothing internal) — check.
- fakeredis TTL asserts: use `ttl` exact ints (fakeredis time is wall clock; assert range [ttl-1, ttl]).
- Sim REST TTS costs `tts_ttfa_ms` sim time; sim_rest uses realtime state with ttfa 0, fine.

## Build P04-T11 (Builder, 2026-11-01)
Implemented per plan (specs/09 §9 Decision P04-T11, specs/03 §11 row updated). Deviations:
- Log field for the cache entry is `entry=` (the log redactor masks any field name containing `key`).
- Flight joiners count as misses (hit only when Redis served the call); prefetch dedupes texts and re-raises
  non-PhraseUnavailableError exceptions (e.g. ValueError for empty text).
- va_voice deps: + orjson, redis, va-domain (uv.lock updated).
Files: va_voice/phrase_cache.py; tests/test_phrase_cache_unit.py (52), tests/test_phrase_cache_sim.py (3, mypy
accepts ElevenLabsRest for the protocol); specs/09 §9, specs/03 §11; AGENTS bullet.
Gates: fmt 0; lint 0; typecheck 0 (865 files); placeholders 0; spec refs 0; make test 7279 passed / 7 skipped
(98.56 %); phrase_cache.py 100 %; jscpd 0 clones; radon A (2.41); pip-audit no known vulns; pnpm audit 3 moderate.

## Review P04-T11 (Reviewer, 2026-11-01)
Approved. Re-ran: lint 0, typecheck 0, placeholders 0, spec refs 0, make test 7279 passed / 7 skipped (phrase_cache
100 %), jscpd 0 clones, radon A (2.41). Checked key material/value JSON/TTLs/2 MB cap against specs/09 §9 and the
specs/03 §11 row; single-flight race (re-check after the await read), shielded waiters, aclose, fail-open Redis paths
all tested; no sleeps beyond sleep(0) yields.

## Plan P04-T12a (Planner, 2026-11-01)
P04-T12 was too large for one Builder activation, so I split it in plan/phase-04.md: T12a (core + harness + normal
turn + opener + max duration + EndCall + cleanup), T12b (barge-in/interrupt policy), T12c (silence + DTMF). T13 now
depends on T12c. Design all of T12a so T12b/T12c only add behaviour, not new plumbing.

### Placement / decisions (record in specs/07 as `> Decision (2026-11-01) P04-T12a` after §16)
- `va_voice.pipeline` is the transport-agnostic orchestrator. va_media's `CallSession` (phase 05) owns the sockets
  (open/close STT/TTS, Telnyx WS, Redis control, ownership/heartbeat, persistence) and adapts `ConversationEngine` (§16)
  to the narrower `Brain`. The pipeline never imports va_elevenlabs/va_telnyx. Its protocols are structural so that
  `va_elevenlabs` objects (`TtsAudio`, `SttEvent`) satisfy the chunk/event protocols as-is (read-only `@property`
  protocol members).
- One timeline: pipeline ms = `clock.monotonic() * 1000`. VAD stream time → pipeline time via `origin_ms` = arrival time
  (pipeline ms) of the first in-order inbound frame minus 20 ms (frame k ends at stream time 20·k). STT partial times =
  arrival time. The Endpointer is fed on this timeline.
- DTMF (T12c) goes to the Brain as a `UserTurn` with `dtmf` set (§13), so `Brain` has no `on_dtmf`.
- The pipeline does not hang up, persist, or publish. `run()` returns `PipelineResult`; turn records and events go to an
  injected `PipelineObserver` (sync callbacks, default no-op) that phase 05 maps to write-behind persistence and
  `call.turn`/`call.latency`.
- Telephony outputs (Transfer, SendSms, SendDtmf, Hold, ...) are opaque `BrainAction` objects handed to the injected
  `ActionHandler.handle(action)` (default: log `pipeline_action_unhandled` and ignore). Only speech and EndCall are
  interpreted.

### Files
1. `packages/va_voice/src/va_voice/pipeline/__init__.py` — re-exports public names (`__all__`).
2. `pipeline/protocols.py`
   - Inbound transport events (frozen slots dataclasses): `InboundAudio(chunk: int, payload: bytes)`,
     `InboundDtmf(digit: str)`, `MarkEcho(name: str)`; `type TransportEvent = InboundAudio | InboundDtmf | MarkEcho`.
   - `class Transport(OutboundSink, Protocol)`: `def events(self) -> AsyncIterator[TransportEvent]` (iterator end =
     far end closed), `async def clear(self) -> None` (used by T12b; declare now).
   - `class SttWordLike(Protocol)`: `text` property. `class SttEventLike(Protocol)`: properties `kind: str`
     (`"partial" | "committed" | "error" | "warning"`, other kinds ignored), `text: str`, `words: Sequence[SttWordLike]`,
     `language_code: str | None`. `class SttStream(Protocol)`: `async send_audio(audio: bytes)`, `async commit()`,
     `def events(self) -> AsyncIterator[SttEventLike]`.
   - `class TtsChunkLike(Protocol)`: properties `context_id: str`, `audio: bytes`, `alignment: ChunkAlignmentLike | None`,
     `is_final: bool`. `class TtsStream(Protocol)`: `async start_context(context_id)`, `async send(context_id, text, *,
     flush: bool = False)`, `async flush(context_id)`, `async cancel(context_id)`, `def audio(self) ->
     AsyncIterator[TtsChunkLike]`. (Signatures must be compatible with `va_elevenlabs.TtsSocket` — start_context has an
     extra optional `voice` param there; a protocol with fewer params is satisfied. Add a typing-only test that assigns
     a `TtsSocket` / `ElevenLabsRealtimeStt` to the protocols — if the STT client's `send_audio(audio, *, commit=False)`
     and `events()` match, good; else adapt the protocol, not the client.)
   - `class VoiceActivityDetector(Protocol)`: `async feed(pcm16k: NDArray[np.int16]) -> list[VadEvent]` (SileroVad fits).
   - `class Brain(Protocol)`: `def start(self) -> AsyncIterator[BrainOutput]`, `def on_user_turn(self, turn: UserTurn)
     -> AsyncIterator[BrainOutput]`, `def on_no_input(self) -> AsyncIterator[BrainOutput]` (T12c),
     `async def on_interrupted(self, spoken_text: str) -> None` (T12b), `def timing(self) -> BrainTiming`.
   - `class ActionHandler(Protocol)`: `async handle(self, action: BrainAction) -> None`.
   - `class PipelineObserver(Protocol)` with `on_user_turn(UserTurn)`, `on_agent_turn(AgentTurn)`,
     `on_latency(TurnReport)`; `NULL_OBSERVER`.
3. `pipeline/models.py`
   - `UserTurn(index: int, text: str, start_ms: float, end_ms: float, words: tuple[str, ...] = (), language_code: str |
     None = None, dtmf: str | None = None)`.
   - Brain outputs: `SpeakText(tokens: AsyncIterable[str], interruptible: bool = True, llm_request_ms: float | None =
     None)` (the Brain may give the instant it sent the LLM request; else the pipeline marks LLM_REQUEST when it starts
     iterating; LLM_FIRST_TOKEN = first non-empty delta), `SpeakStatic(text: str, interruptible: bool = True)`,
     `EndCall(final_text: str | None = None, reason: str = "agent_hangup")`, `BrainAction` (base class, frozen
     dataclass with `kind: str`; subclass-able by phase 05). `type BrainOutput = SpeakText | SpeakStatic | EndCall |
     BrainAction`.
   - `BrainTiming(interruptibility: int | None = None, resumption_speed: ResumptionSpeed = 2, collecting_number: bool =
     False, no_input_prompt: str | None = None)`.
   - `AgentTurn(index, text, text_spoken, interrupted: bool, started_ms, ended_ms)` (T12a: interrupted always False,
     text_spoken = text).
   - `EndReason = Literal["transport_closed", "agent_hangup", "max_duration", "no_input_timeout", "error"]` (EndCall may
     carry another reason string; keep `PipelineResult.end_reason: str`). `PipelineResult(end_reason: str, turns: int,
     error: BaseException | None = None)`.
4. `pipeline/config.py` — `PipelineConfig` frozen dataclass: `fmt: AudioFormat = ULAW_8000`, `interruptibility: int =
   2`, `block_interruptions: bool = False`, `interruption_threshold_ms: int | None = None`, `ivr_mode: bool = False`,
   `max_duration_s: float = 1800.0` (Bland `max_duration` minutes × 60 by the caller), `max_duration_message: str =
   "We have reached the maximum call duration. Goodbye."` (check specs/05 or 24 for the actual default text and use it;
   document the source), `max_duration_warning_s: float = 10.0`, `stt_commit_timeout_ms: int = 1500`,
   `final_line_timeout_s: float = 8.0`, `no_input_timeout_ms: int = 8000`, `max_reprompts: int = 2`,
   `no_input_goodbye: str = "It seems we got disconnected. Goodbye."`, `ignore_button_press: bool = False`,
   `pronunciation: tuple[PronunciationRuleLike, ...] = ()`, `reorder_window: int = 5`. Validate ranges in
   `__post_init__` (ValueError). T12c fields are declared now but used later.
5. `pipeline/pipeline.py` — `class CallPipeline`:
   `__init__(self, *, transport, stt, tts, vad, brain, config, clock, mixer: FrameMixer | None = None, latency:
   LatencyTracer | None = None, observer=NULL_OBSERVER, actions: ActionHandler | None = None, context_ids:
   Callable[[int], str] = lambda turn: f"turn-{turn}")`; `async run(self) -> PipelineResult` (call once; second call
   RuntimeError).
   Task group (asyncio.TaskGroup) inside `run`:
   - `inbound_reader`: iterate `transport.events()`; InboundAudio → `ReorderBuffer.push` → for each OrderedFrame:
     `stt.send_audio(payload)` (raw codec bytes), decode to PCM (ulaw_decode or L16 per fmt) + resample to 16 kHz
     (`va_voice.audio` Resampler; 16 kHz L16 passes through) → `vad.feed` (await inline; VAD ordering) → VadEvents to
     the endpointer on the pipeline timeline; MarkEcho → current tracker `on_mark`, re-check playback completion;
     InboundDtmf → stored for T12c (T12a: ignore with debug log). Iterator end → `reorder.drain()` frames processed,
     then end with `transport_closed`.
   - `stt_receiver`: partials → endpointer.on_partial(text, now) + keep `latest_partial`; committed → resolve the
     pending commit future (one outstanding commit at a time); error/warning → log.
   - `endpoint_timer`: wake event + `clock.sleep` until `endpointer.deadline_ms()`; `poll(now)` → EndOfTurn → start a
     turn: timer marks USER_SPEECH_START (from the turn's first speech_start), USER_SPEECH_END (last voiced end, last
     wins), EOT, STT_COMMIT; `stt.commit()`; await committed with `stt_commit_timeout_ms` (timeout → latest partial /
     EndOfTurn.text, log `stt_commit_timeout`); STT_FINAL; build `UserTurn`, observer.on_user_turn, then run the Brain
     reply in the `turn_runner`. TurnDiscarded → debug log only. Never hold CPU: the sleep loop wakes on every VAD
     edge/partial (event set).
   - `outbound`: `OutboundAudio(transport, mixer or FrameMixer(fmt), clock=clock).run()`.
   - `tts_receiver`: `tts.audio()` → chunks for the current context: first audio → TTS_FIRST_AUDIO; `tracker.timeline.
     add_chunk(audio, alignment)`; `outbound.push_audio`; `is_final` → resolve the context's done future. Chunks of
     other (stale) context ids are dropped (counted in `PipelineStats.stale_tts_chunks`).
   - `watchdog`: sleep until `max_duration_s - warning_s` → speak `max_duration_message` non-interruptible (cancels
     any running reply first — it pre-empts the Brain; T12b handles interruptibility), at `max_duration_s` end
     `max_duration` (wait for the warning's playback only up to max_duration).
   - Speaking (`_speak` coroutine, one utterance at a time, a lock): new turn number, `PlaybackTracker(turn, fmt)`,
     `outbound.start_utterance(tracker)`, `tts.start_context(ctx)`; SpeakText: tokens → ClauseChunker.feed → each clause
     → `applier.apply(clause)` → `tracker.timeline.add_clause(pronounced)` → `tts.send(ctx, pronounced.text,
     flush=True)` (first send → TTS_FIRST_TEXT) and `outbound.end_segment(len(clause))`? — NO: segment boundaries must
     follow audio. Rule: call `outbound.end_segment(chars)` when the TTS audio of that clause is complete; since
     per-clause audio boundaries are not signalled, use one segment per utterance in T12a?? Decision: segment
     boundaries are placed at alignment boundaries — simplest correct approach: segment = audio between successive
     `is_final`? ElevenLabs gives one is_final per context. So: **one segment per utterance** (end_segment(total chars)
     at is_final, then finish_utterance()). Marks therefore confirm the whole utterance; played_ms between marks comes
     from the estimate. Record this in the Decision (multi-segment marks need per-clause contexts; revisit only if
     barge-in accuracy demands it). After the stream ends: `chunker.flush()` clauses, `tts.flush(ctx)`, await the
     context's is_final (no timeout in T12a beyond cancellation; the TTS client has its own no-audio timeout), then
     `outbound.end_segment(total_chars)` + `finish_utterance()`, then wait for playback completion: completion =
     `tracker.completed(now)` checked on every MarkEcho and on a timer at `t_first + total_ms + lead + 200 ms`
     (MARK_LOSS_GRACE_MS). Then `observer.on_agent_turn(AgentTurn(...))`.
     SpeakStatic: same path with one clause (`applier.apply(text)`; send with flush=True; no chunker; no LLM marks).
     EndCall: speak `final_text` if any (interruptible False), wait completion bounded by `final_line_timeout_s`, end
     with its reason.
     First audio frame → FIRST_AUDIO_OUT: detect when `tracker.t_first_frame_sent` becomes set — expose it via a tiny
     wrapper sink: the pipeline passes `OutboundAudio` a sink that forwards to the transport and records the first
     frame time per utterance (`_FirstFrameSink`), or read `tracker.t_first_frame_sent` (it is set by OutboundAudio on
     the same timeline? OutboundAudio uses clock ms → check `_now_ms` uses `clock.monotonic()*1000`; if yes, use the
     tracker value after completion: mark FIRST_AUDIO_OUT at `tracker.t_first_frame_sent`).
     Latency: user turn → `latency.start_turn(index, node_id=None)`; finish after the reply's first audio is out (or
     when the reply ends without audio) → `observer.on_latency(report)`. Opener turn has no user marks (no report
     unless measured stages; still finish it so TTS ttfa is reported — fine either way; test asserts no E2E event for
     the opener).
   - Turn runner: one active reply task (`asyncio.Task`), consuming `brain.on_user_turn(turn)` outputs sequentially in
     order. In T12a a new EndOfTurn while a reply is still generating waits (queues) — T12b replaces this with the
     concurrency rule. Keep the reply task handle so T12b can cancel it.
   - Ending: a single `_end(reason, error=None)` sets the result once and cancels the task group (raise a private
     `_PipelineEnded` exception caught around the TaskGroup, or cancel tasks via a stop event + `TaskGroup` cancel
     scope). Any task exception → result `error` with the exception (log `pipeline_failed` with error type);
     `CancelledError` of `run()` itself propagates after cleanup. Cleanup: every task cancelled and awaited (TaskGroup
     guarantees), current TTS context cancelled best-effort (`tts.cancel(ctx)` in a `finally`, exceptions logged),
     brain async generators closed (`aclose()` when they have it).
   - `PipelineStats` (dataclass): turns, user_turns, stale_tts_chunks, commit_timeouts, discarded_turns.
   - Opener: at start (before any user turn) run `brain.start()` outputs through the same reply runner (§11 step 5/6;
     greeting waits/AMD are va_media's concern — document).
   - Apply `brain.timing()` at the start of each user turn: `endpointer.configure(EndpointerSettings(resumption_speed,
     config.interruption_threshold_ms, config.ivr_mode, collecting_number))`.
   Keep each method small (radon ≤ B); split into helpers/modules if pipeline.py grows past ~450 lines
   (e.g. `pipeline/speaker.py` for the utterance logic, `pipeline/inbound.py` for decode/resample/VAD fan-out).
6. specs/07: Decision P04-T12a (after §16 code block): the items above (pipeline vs CallSession split, Brain subset,
   DTMF as UserTurn, timeline, one segment per utterance, observer/actions, end reasons, opener scope).
7. AGENTS.md bullet (P04-T12a): module map + harness usage.

### Harness `packages/va_voice/tests/support/pipeline_harness.py` (support dir is already on pythonpath/mypy_path)
- Clock: `va_sim.clock.AcceleratedClock()` (virtual time advances only when the loop is idle; no wall delay). Verify
  it is importable from va_voice tests (root dev deps) and `OutboundAudio` works with it.
- `FakeTransport`: `asyncio.Queue` events; `send_audio` records `(t_ms, frame)`, `send_mark` records and schedules an
  echo `MarkEcho(name)` after `echo_delay_ms` (default 60) via a task on the clock; `clear` records `t_ms`; `close()`
  ends `events()`. Shared `calls: list[tuple[str, float]]` call log (T12b ordering asserts).
- `FakeCaller(transport, clock)`: `say(ms, loud=True)` / `silence(ms)` push 20 ms μ-law frames with increasing chunk
  numbers paced by `clock.sleep(0.02)`; loud = `b"\x10\x90" * 80`, silence = `b"\xff" * 160`.
- `EnergyVad`: implements VoiceActivityDetector with the real `VadDecider` over 512-sample windows (probability 1.0
  when window RMS > −40 dBFS else 0.0; level = rms_dbfs). This keeps hysteresis real without ONNX.
- `FakeStt`: `utterances: deque[str]`; once it has received ≥ 5 loud frames (decode μ-law, energy check) it emits a
  `partial` with the head utterance text; on `commit()` emits `committed` with it after `commit_latency_s` (default 0)
  and pops it; records commits; `events()` from a queue. Event objects = small frozen dataclasses satisfying
  `SttEventLike`.
- `FakeTts`: on `send(ctx, text)` emits audio chunks after `ttfa_s` (default 0): μ-law tone bytes, 8 bytes/ms with
  `ms_per_char` (default 5) per character, alignment chars = text chars evenly spaced (chunk-relative); on `flush(ctx)`
  emits `is_final` after the last audio; `cancel` records and stops that context; records every call in the shared log.
- `ScriptedBrain`: `opener: list[BrainOutput]`, `replies: deque[list[BrainOutput]]` (each SpeakText built from a
  string via `tokens("Hello there, how can I help?")` async generator yielding word deltas, optional per-token delay on
  the clock); records `user_turns`, `no_input_calls`, `interrupted`; `timing()` returns a settable `BrainTiming`.
- `RecordingObserver`: lists of user turns, agent turns, latency reports.
- `harness(config=..., brain=...)` async context manager: builds everything, starts `pipeline.run()` as a task, yields
  a `Harness` (fields: clock, transport, caller, stt, tts, brain, observer, pipeline, `result()` awaits the run task,
  `until(predicate, timeout_s)` sleeps on the virtual clock in 20 ms steps until true — bounded, no wall sleeps), and on
  exit closes the transport and awaits the run.

### Tests `packages/va_voice/tests/pipeline/` (add `__init__`-less dir like other test dirs; check conftest pattern)
- `test_pipeline_turn.py`
  - opener: `ScriptedBrain(opener=[SpeakStatic("Hi, this is Ava.")])` → frames sent, a mark sent and echoed, agent
    turn recorded with the text; `tts.start_context`/`send(..., flush=True)`/`flush` in order.
  - normal turn: caller says 600 ms + silence → Brain gets one UserTurn with the committed text (not the partial when
    they differ), STT commit happened after EOT, reply audio frames paced 20 ms apart after the first burst (lead 3),
    observer gets agent turn after the mark echo, latency report with e2e/eot/stt_final/tts_ttfa measured and the
    `"E2E: {n}ms"` event; with zero-latency fakes e2e ≈ eot + small overhead (assert e2e − eot ≤ 30 ms of virtual time).
  - committed text missing (FakeStt with `commit_latency_s=2`) → commit timeout after 1.5 s, latest partial used,
    `stats.commit_timeouts == 1`.
  - pronunciation rule applied to TTS text while the timeline/agent turn keeps the original text.
  - LLM marks: SpeakText with token delay 0.2 s → `llm_ttft_ms` = 200.
  - stale TTS chunk (FakeTts emits a chunk for an old context id) is dropped and counted.
  - noise burst without STT text → TurnDiscarded, Brain not called.
  - reorder: out-of-order inbound chunks still reach STT in order (FakeStt records audio order).
  - L16 16 kHz config (`PCM16_16000`/L16 format — use the va_voice format constant for hd_audio) path: VAD fed without
    resampling (EnergyVad sees 512-sample windows of the same samples).
- `test_pipeline_lifecycle.py`
  - transport close → `PipelineResult("transport_closed")`, all pipeline tasks done (no pending tasks created by the
    pipeline: compare `asyncio.all_tasks()` before/after), current TTS context cancelled.
  - `EndCall("Goodbye.")` → final line played (mark echoed) then `agent_hangup`; `EndCall()` without text ends at once.
  - EndCall when marks are never echoed (`echo_delay_ms=None`) → ends after the mark-loss grace, before
    `final_line_timeout_s`.
  - max duration (config max 30 s, warning 10 s): warning spoken at 20 s (non-interruptible SpeakStatic path), result
    `max_duration` at 30 s virtual.
  - a failing component (FakeStt.events raises RuntimeError) → result `error` with that exception, log
    `pipeline_failed`.
  - BrainAction is passed to the ActionHandler; without one it is logged and ignored.
  - `run()` twice → RuntimeError; cancelling the run task cancels everything and re-raises CancelledError.
  - config validation errors.
- `test_pipeline_protocols.py`: typing-level checks (functions annotated to accept the protocols, called with
  `va_elevenlabs` `TtsSocket`/`ElevenLabsRealtimeStt` instances constructed without opening, `SileroVad` type) so mypy
  enforces compatibility; runtime asserts trivial.
Coverage: `va_voice/pipeline/*` ≥ 95 % (aim 100 %); va_voice package stays ≥ 85 %.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q`, `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src/va_voice/pipeline`, pip-audit/pnpm audit (no new third-party deps; va_sim only in tests via the
root dev group — if va_voice tests importing va_sim breaks the acyclic/layering checks, put an equivalent idle-advancing
clock in `packages/va_common/tests/clock_helpers.py` instead).

### Risks
- Accelerated clock + OutboundAudio parks on an event without background: fine. Mark echo tasks must use the clock.
- Endpointer timeline: VAD speech_end at_ms in stream time must be mapped before `on_vad`; build a new `VadEvent` via
  `dataclasses.replace(event, at_ms=origin + event.at_ms)`.
- Ending from inside a task group: use one stop path; avoid swallowing CancelledError of the outer task (check
  `asyncio.current_task().cancelling()`).
- Commit future races: a committed event arriving with no pending commit (late) is ignored with a debug log.
- Keep radon complexity low; jscpd: harness fakes must not duplicate va_elevenlabs test helpers.

## Build P04-T12a (Builder, 2026-11-01)
Implemented `va_voice.pipeline` (protocols, models, config, inbound, speaker, pipeline, waits) per the plan, plus the
harness (`tests/support/pipeline_fakes.py`, `pipeline_harness.py`) and 60 tests in `tests/pipeline/` (turn, lifecycle,
edges, protocols). Deviations/decisions (all in the specs/07 §16 Decision P04-T12a):
- `OutboundAudio.finish_utterance(chars=0)` + `on_finished=` callback (needed so one-segment utterances carry their
  chars and the speaker learns about `tracker.finish()` without polling); tests added in test_outbound_audio.py.
- Reply tasks are plain tasks owned by the reply loop (not TaskGroup children): creating a child while the group is
  aborting raised and leaked the coroutine. The loop cancels/awaits its reply on exit and re-raises its result.
- Spoken defaults follow the specs/04 org settings (va_domain OrgSettings), not specs/05's max_duration wording;
  `no_input_prompt` default "Are you still there?" (§12). `PipelineStats` is part of `PipelineResult`.
- STT `is_update` committed events are ignored (protocol member `is_update`).
- Once TTS `is_final` arrived the context is not cancelled on abandon (nothing open at the vendor).
Gates: make fmt/lint/typecheck/placeholders pass; make test 7341 passed / 7 skipped (coverage 98.58 %, pipeline
99-100 %); spec refs 0; jscpd 0 clones; radon avg A (1.99), worst B (existing OutboundAudio._send_controls);
pip-audit clean; pnpm audit 3 moderate (pre-existing, high threshold passes).

## Review P04-T12a (Reviewer, 2026-11-01)
Approved. Re-ran: make lint/typecheck/placeholders (pass), pytest packages/va_voice 821 passed (pipeline.py 99 %),
make test 7341 passed / 7 skipped (98.58 %), check_spec_refs 0, jscpd 0 clones. Diff checked against specs/07
§1/§5/§6/§8/§9/§11/§16/§18/§20 and the Decision; spoken defaults match specs/04 §7 (tested against OrgSettings).
Note for P04-T12b: when an utterance is cancelled mid-SpeakText, `Speaker` does not aclose `SpeakText.tokens`
(only the Brain output stream is closed); the barge-in "cancel LLM stream" step should close the token iterator
explicitly.

## Plan P04-T12b (Planner, 2026-11-01)
Barge-in, interruption policy, concurrency rule and false-barge-in resume in `CallPipeline` (specs/07 §7, §8, §9).
Record every decision below in specs/07 as `> Decision (2026-11-01) P04-T12b` placed right after the §8 concurrency
paragraph (one block; reference §7 inside it on the same line as `specs/07`/§ refs as needed).

### Decisions (put in the spec Decision block)
1. **Evaluation** runs while `Speaker.is_playing(now)` (an utterance whose first frame went out, not stopped, not
   completed) and user speech is active (current continuous VAD segment open). It is evaluated after every inbound
   frame batch (`_read_transport`, after `_on_vad`) and after every STT partial. `speech_ms = now − at_ms of the
   segment's speech_start`; text = latest STT partial of the open user turn (reset at EOT/discard/commit); words=None
   (tokenize text). Level = `effective_level(call_level=config.interruptibility, node_level=timing.interruptibility,
   block_interruptions=config.block_interruptions, interruptible=<current utterance's interruptible>)`; timing is
   the cached `BrainTiming` from `_apply_timing` (store it in `self._timing`; never call `brain.timing()` per frame).
2. **Reaction.** `call.interrupt` `reaction_ms = t_clear_sent − t_speech_start` (spec §7 step 7; includes the level's
   speech threshold). The ≤ 120 ms target is decision → clear (`clear_delay_ms`); tests assert `clear_delay_ms ≤ 120`
   and at level 4 `reaction_ms ≤ 150 + 120`.
3. **Sequence** (one coroutine `CallPipeline._barge_in(decision, now)`, called inline from the inbound or stt task;
   re-entry guarded synchronously because `tracker.stop` happens before the first await):
   (a) `Speaker.interrupt(at_ms)` — sync: `tracker.stop(at)`, `outbound.clear()`, mark the utterance `cancelled`;
   then `await transport.clear()` (t_clear = now after it), then `await tts.cancel(ctx)` (unless `is_final`
   arrived); returns `Interruption(utterance_index, truncated: TruncatedMessage, played_ms, clear_ms, resumable)`
   or `None` when nothing is playing. `_abandon` must not cancel the TTS context a second time (check `cancelled`).
   (b) cancel the reply task and await it (closes the Brain output stream; Speaker closes `SpeakText.tokens`
   explicitly — fixes the T12a review note; this is step 4 "cancel the LLM stream"; step 5 tool speech = whatever the
   reply task was doing). The cancelled utterance is recorded as an interrupted `AgentTurn` by `_abandon` with the
   frozen `played_ms` (already true).
   (c) `await brain.on_interrupted(truncated.history_text)` (history = `text_spoken + "—"`).
   (d) `observer.on_interrupt(InterruptRecord(...))`, `latency.barge_in(honored=True, reaction_ms=...)`,
   `stats.barge_ins += 1`, log `pipeline_barge_in` (turn_index, reason, level, reaction_ms, played_chars, total_chars).
   (e) `_pending_resume = truncated.unspoken` if non-blank and the interrupted utterance was not itself a resume.
   Endpointing simply continues (step 8).
4. **Unhonored barge-ins**: a VAD speech segment that started or ran while `Speaker.is_playing` and ended (speech_end,
   or the utterance completed) without an interrupt → `latency.barge_in(honored=False)` once per segment,
   `stats.unhonored_barge_ins += 1`.
5. **Turn routing at EndOfTurn** (`_route_turn(turn, timer)` replaces `_jobs.put_nowait`):
   - `_closing` → drop (existing behaviour).
   - false barge-in check first: if `_pending_resume` is set and the committed transcript is missing (commit timeout
     = `stt_commit_timeout_ms`, 1 500 ms) or blank → **false barge-in**: the turn is NOT given to the Brain (no partial
     fallback for post-interrupt turns), `stats.false_barge_ins += 1`, log `pipeline_false_barge_in`, queue a resume
     job speaking `SpeakStatic("Sorry — " + unspoken, interruptible=True)` (no Brain stream; flagged `resume=True`
     so it never arms another resume). A `TurnDiscarded` while `_pending_resume` is set is the same false barge-in.
     Any delivered user turn or any new reply clears `_pending_resume`. (`false_barge_in_total` has no specs/27 metric
     yet → PipelineStats counter only; phase 05 maps it.)
   - no reply running → queue the turn (merged with `_held`, see below).
   - reply running and **no audio of this reply heard yet** (`Speaker.reply_audio_started` false) → concurrency rule:
     cancel the generation without `clear` (`Speaker.cancel_generation()` = tts.cancel of the open context, then the
     reply task cancel), `stats.merged_turns += 1`, new turn = `merge_turns(reply_turn, turn, index)` where text =
     `f"{prev.text} {new.text}"` (stripped; just the new text when the reply was the opener / had no user turn),
     `start_ms` = prev start, `end_ms` = new end, words concatenated, `language_code` = new's or prev's,
     `supersedes=prev.index` (new `UserTurn.supersedes: int | None = None` field; observer receives the merged turn
     too). Queue it.
   - reply running and audio already started: if the user's speech overlapped interruptible playback (level ≥ 1)
     without barge-in and the turn text is all backchannel (`BackchannelClassifier.is_backchannel(text)`) → drop
     (`stats.ignored_backchannels += 1`, log debug `pipeline_backchannel_ignored`). Otherwise **hold**: `_held` =
     merge(_held, turn) (level 0 / non-interruptible / insufficient speech: "appended to the next user turn after
     playback ends").
   - Held delivery: after each reply finishes, if `_held` and `not endpointer.turn_open` → queue it; if a turn is
     open, its EOT merges `_held` in front (`merge_turns(held, turn)`); after a `TurnDiscarded` with no reply running
     → queue `_held`.
   Track `_reply_turn: UserTurn | None` (turn the running reply answers) in `_run_reply`.
6. `Speaker` gains: `_Utterance.interruptible`, `cancelled`, `resume`; `is_playing(now_ms) -> bool`,
   `current_interruptible -> bool | None`, `reply_audio_started` (+ `begin_reply()` called by the pipeline at each
   reply start; set true when any utterance of the reply sent a frame — check the active tracker's
   `t_first_frame_sent` live plus a flag set in `_record`/`_abandon`), `interrupt(at_ms) -> Interruption | None`,
   `cancel_generation()` (tts.cancel, no clear, only valid before audio), `speak(..., resume=False)`. In `speak`,
   wrap clause iteration with `contextlib.aclosing` and close `output.tokens` (if it has `aclose`) in `finally`.

### Files
- `src/va_voice/pipeline/barge_in.py` (new, pure/sync, keeps pipeline.py small): `BargeInMonitor(policy:
  InterruptPolicy | None = None)` with `on_vad(event) -> bool` (returns True when an unhonored attempt just ended),
  `on_partial(text)`, `reset_turn()`, `evaluate(now_ms, *, playing: bool, level: int) -> InterruptDecision | None`
  (None when not playing / no speech; marks the segment as an attempt; truthy decision once per segment),
  properties `speech_started_ms`, `overlapped_playback` (current user turn overlapped interruptible playback),
  `playback_ended()` (closes an open attempt as unhonored → returns bool). `merge_turns(prev: UserTurn | None, new:
  UserTurn, index: int) -> UserTurn`.
- `src/va_voice/pipeline/models.py`: `UserTurn.supersedes`, `InterruptRecord(turn_index, at_ms, speech_start_ms,
  clear_ms, reaction_ms, clear_delay_ms, played_ms, played_chars, total_chars, level, reason)`, `PipelineStats` +
  `barge_ins`, `unhonored_barge_ins`, `false_barge_ins`, `ignored_backchannels`, `merged_turns`, `held_turns`.
- `src/va_voice/pipeline/protocols.py`: `PipelineObserver.on_interrupt(record)`; `_NullObserver` too.
- `src/va_voice/pipeline/speaker.py`, `pipeline.py` per the decisions (job types `_TurnJob(turn, timer)` /
  `_ResumeJob(text)`; `_Job` union). Keep radon ≤ B per function; move routing into small methods.
- `src/va_voice/pipeline/__init__.py`: export `InterruptRecord`, `BargeInMonitor`, `merge_turns`.
- specs/07 Decision block; AGENTS.md bullet (P04-T12b: barge-in entry points + harness helpers).

### Harness / fakes (`tests/support/pipeline_fakes.py`, `pipeline_harness.py`)
- `tokens(..., per_token_delay_s=0.0, calls=None)`: logs `("llm.cancel", t)` in `finally` when closed before
  exhaustion (GeneratorExit/CancelledError).
- `ScriptedBrain(calls=...)`: `on_user_turn`/`start` finally log `("brain.closed", t)`; `on_interrupted` logs
  `("brain.interrupted", t)`. Harness passes `transport.calls` to brain (shared log).
- `RecordingObserver.interrupts: list[InterruptRecord]`.
- `harness(..., brain_calls=True)` not needed if the brain gets the clock+calls via a harness setter; keep simple:
  `ScriptedBrain.bind(clock, calls)` called by the harness.
- `FakeStt` partial cadence: tests that need ≥ 2 words before 500 ms rely on `partial_after` (10 loud frames =
  200 ms); expose `partial_after` as a harness knob.

### Tests `tests/pipeline/test_pipeline_barge_in.py` (+ `tests/pipeline/test_barge_in_monitor.py` pure unit tests)
Harness tests (AcceleratedClock, long agent utterances e.g. 400 chars × 5 ms = 2 s audio):
- `test_barge_in_sequence_order_and_timing`: level 2, SpeakText with per-token delay so LLM still streams; caller
  speaks 900 ms "please stop talking now" while playing → calls order `transport.clear` < `tts.cancel` <
  `llm.cancel` < `brain.closed` < `brain.interrupted`; `InterruptRecord.clear_delay_ms ≤ 120`; reaction_ms =
  clear − speech start; no frames of that utterance sent after clear (`transport.frames` times ≤ clear); interrupted
  AgentTurn `text_spoken` is a whole-word prefix; `brain.interrupted[0]` ends with "—"; latency metric
  `va_barge_in_total{honored="true"}` = 1 when the harness uses a real `LatencyTracer` with metrics (latency knob).
- `test_level4_any_speech_interrupts_quickly`: level 4, backchannel "mm" → interrupt, `reaction_ms ≤ 270`.
- `test_backchannel_ignored_at_level_2`: caller says "yeah" 300 ms during playback → no clear, utterance completes
  (not interrupted), Brain gets no user turn for it, `stats.ignored_backchannels == 1`, unhonored count 1.
- `test_level2_long_speech_without_words_interrupts` (≥ 900 ms, partial is backchannel-only) → interrupt reason
  `long_speech`.
- `test_level0_blocks_and_appends_to_next_turn`: config interruptibility 0 (and a variant `block_interruptions=True`),
  caller speaks "wait a second" during playback → no clear, playback completes, then Brain gets UserTurn "wait a
  second" (held → delivered after playback); variant where the caller speaks again before... use two utterances: one
  during playback, one starting right as playback ends while held → single merged turn "a b" with `supersedes` None
  (held merge) — assert text concatenation.
- `test_non_interruptible_static_not_interrupted`: SpeakStatic(interruptible=False) at level 4 → no clear.
- `test_node_level_overrides_call_level` (timing.interruptibility=0 with call level 4 → blocked).
- `test_pre_audio_turn_cancels_generation_and_concatenates`: reply = SpeakText(hanging/slow first token, e.g.
  first_delay 5 s) so no audio; caller speaks a second utterance → `tts.cancel` (or no context yet), no
  `transport.clear`, Brain's next `on_user_turn` text == "first second", `supersedes` == first index,
  `stats.merged_turns == 1`, latency report of the cancelled turn has no E2E event.
- `test_false_barge_in_resumes_once`: interrupt by noise (Said("") commit blank, partial "uh what" to cross words
  threshold or 900 ms speech) → no Brain turn, resume utterance text starts with "Sorry — " + unspoken remainder,
  `stats.false_barge_ins == 1`; interrupt the resume with noise again → no second resume.
- `test_false_barge_in_commit_timeout` (commit_latency 2 s → timeout → resume) and `test_real_turn_after_interrupt_
  clears_resume` (committed text → Brain gets it, no resume).
- `test_unhonored_speech_counted` / `test_barge_in_during_opener` (opener interruptible, brain.start closed).
- Speaker unit edges: `interrupt` with nothing playing → None; double trigger (partial + frame same tick) → one
  interrupt; tts.cancel failure during interrupt logged, sequence continues.
Monitor unit tests: evaluate None when not playing/no speech; speech_ms from segment start; one decision per segment;
attempt bookkeeping; `merge_turns` text/fields (opener prev None, blank prev).
Update existing tests if `_NullObserver`/observer signature or job flow changes; all 60 T12a tests must still pass.
Coverage: pipeline modules ≥ 95 % (aim 100 %).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q`, `make test`,
`uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src/va_voice/pipeline`, pip-audit + `corepack pnpm audit` (no new deps).

### Risks
- Cancelling the reply task from the inbound task: the reply's `_abandon` awaits only local work after (a); never await
  something that needs the inbound task (mark echoes) while cancelling. `asyncio.wait({task})` after cancel.
- CancelledError landing inside `tokens.__anext__` finalizes the generator itself (logs `llm.cancel` at cancel time),
  which is after tts.cancel because the cancel is issued after (a) — keep that order.
- AcceleratedClock: barge-in tests must keep the caller streaming (FakeCaller) — never busy-poll.
- VAD speech_start arrives ≥ START_WINDOWS windows after the speech began, so level-4 decisions happen on the first
  frame after speech_start; assert bounds, not exact ms.
- Held/merge state must be cleared on `_closing`; watchdog/EndCall lines are non-interruptible (level 0) → speech
  during them is held then dropped by `_closing`.

## Build P04-T12b (Builder, 2026-11-01)
Implemented per plan: `va_voice.pipeline.barge_in` (`BargeInMonitor` with `on_unhonored` callback, `merge_turns(prev,
new, supersede=)`), Speaker (`interrupt`, `cancel_generation`, `is_playing`, `current_interruptible`,
`reply_audio_started`/`begin_reply`, `speak(..., resume=)`, `aclosing` clauses + explicit `SpeakText.tokens` close,
`Interruption`), pipeline (`_check_barge_in`, `_barge_in`, `_route_turn`, `_false_barge_in`, `_flush_held`,
`_TurnJob`/`_ResumeJob`, `RESUME_PREFIX`), models (`UserTurn.supersedes`, `InterruptRecord`, 6 new stats),
`PipelineObserver.on_interrupt`. Spec: specs/07 §8 Decision P04-T12b. Deviations from plan:
- `merge_turns` takes `supersede=` (held merges have no `supersedes`; concurrency merges set it).
- Turns that end while the call is closing are still reported to the observer (transcript) but not answered.
- Backchannel drop applies whether or not the reply is still running (the overlap flag decides).
- False barge-ins consume no turn index (index allocated after the commit result; STT_COMMIT mark uses the
  pre-commit instant). Held turns delivered on their own carry no latency timer.
- `preempt()` now also cancels the TTS context (it marks the utterance cancelled, so `_abandon` skips it).
Tests: tests/pipeline/test_pipeline_barge_in.py (20) + test_barge_in_monitor.py (9) + null observer; 89 pipeline
tests; pipeline coverage 97-100 %.
Gates: make fmt/lint/typecheck/placeholders pass (mypy 882 files clean, 0 placeholder findings); make test 7370 passed /
7 skipped, coverage 98.56 % (pipeline 97-100 %); 3 consecutive xdist runs of tests/pipeline green; check_spec_refs 0;
jscpd 0 clones; radon avg A (2.12); pip-audit no known vulnerabilities; pnpm audit 3 moderate (high threshold passes).

## Review P04-T12b (Reviewer, 2026-11-01)
Gates re-run: make lint/typecheck/placeholders pass (mypy 882 files, 0 placeholder findings); make test 7370 passed /
7 skipped, coverage 99 % total. The barge-in sequence, InterruptRecord, false-barge-in resume, held/merge routing and
the Decision block all look right. REJECTED, 2 findings:
1. `packages/va_voice/src/va_voice/pipeline/pipeline.py:450` (`_route_turn`, concurrency branch) and `:573`/`:585`
   (`_handle_action`): when a turn ends while a reply runs with no audio yet, `_cancel_reply()` cancels the whole reply
   task, including an `ActionHandler.handle(action)` that is in flight (transfer, SMS, DTMF, hold). specs/07 §8 says
   the concurrency rule cancels "the generation (same as barge-in steps 3–4 without `clear`)" (TTS context and LLM
   stream only), and §7 step 5 says tool requests "continue to completion". Cancelling a half-done transfer or SMS
   leaves telephony state inconsistent, and the Brain may retry and send twice. Fix: never cancel a reply while it
   is inside `_handle_action` or `_end_call`. Either hold the new turn until the action returns, then cancel the
   rest of the generation and merge, or shield the action (`asyncio.shield` plus waiting for it) so cancellation
   lands only between outputs. Add a harness test: a reply yields `BrainAction` with a slow `ActionHandler`, the
   caller's turn ends meanwhile, and the action must still complete (handler records completion, not
   CancelledError). Record the rule in the P04-T12b Decision block.
2. `packages/va_voice/src/va_voice/pipeline/barge_in.py:87-111`: when playback stops mid-segment, `evaluate(playing=
   False)` calls `_close_attempt()`, which sets `_decided = True`. If the same continuous VAD segment then overlaps
   the reply's next utterance (e.g. a non-interruptible `SpeakStatic` followed by an interruptible `SpeakText`, or
   a level-0 line followed by a normal one), that utterance is never evaluated, which contradicts §7 "user speech is
   evaluated continuously". Fix: closing an attempt because playback ended should report it unhonored and clear
   `_attempt`, but must not block later evaluation in the same segment. Only an honored decision should set
   `_decided`. Add a monitor unit test (attempt → playing=False → unhonored once → playing=True with enough speech →
   decision) and a harness test (non-interruptible line then interruptible line, with the caller speaking across
   both: the second line is interrupted).

## Build P04-T12b fixes (Builder, 2026-11-01)
Both review findings are fixed:
1. Actions are never cancelled. `_handle_action` runs `ActionHandler.handle` in its own task (`self._action`) and
   awaits it through `asyncio.shield`. A reply cancel (barge-in, concurrency merge, watchdog) therefore ends the reply
   right away, so the inbound path never blocks on an action, while the action itself runs to completion. The reply
   loop calls `_settle_action()` after every `_run_reply`: it waits for the orphaned action before the next reply
   starts, and the action's failure still fails the pipeline. `run()` calls `_abandon_action()` in `finally`, which
   cancels an action still in flight when the pipeline ends (`pipeline_action_abandoned`). EndCall cannot be cut:
   it sets `_closing` first, and turns arriving while closing are never answered. The rule is in the Decision block.
2. `BargeInMonitor._close_attempt` no longer sets `_decided`. Playback ending reports the attempt unhonored once,
   and the same segment is evaluated again against the next utterance. Only an honored decision ends evaluation.
Tests: monitor unit test `test_a_segment_outliving_playback_is_evaluated_against_the_next_utterance` (the old
"not evaluated again" assertion is removed). Harness tests: `test_a_segment_spanning_two_lines_interrupts_the_interruptible_one`,
`test_a_turn_during_an_action_merges_without_cancelling_it`, `..._action_outliving_its_reply_fails_the_pipeline...`,
`test_the_pipeline_end_cancels_an_action_in_flight`. Fake: `pipeline_fakes.SlowActions(duration_s, fail=)`, bound
to the clock by the harness. All 5 new tests fail when the two fixes are reverted.
Gates: make fmt/lint pass; make typecheck pass (882 files); placeholders 0; make test 7375 passed / 7 skipped,
coverage 98.55 % (pipeline 97-100 %); pipeline tests 3x xdist green; spec refs 0; jscpd 0; radon avg A (2.15);
pip-audit clean; pnpm audit 3 moderate (high threshold passes).

## Review P04-T12b fixes (Reviewer, 2026-11-01)
APPROVED. Gates re-run: make lint/typecheck/placeholders pass (0 findings); make test 7375 passed / 7 skipped, coverage
98.55 %. Finding 1: `_handle_action` runs the handler in its own task under `asyncio.shield`, `_settle_action` waits for
orphans before the next reply, `_abandon_action` cancels at pipeline end; Decision block updated. Finding 2:
`_close_attempt` no longer sets `_decided`; monitor + harness tests cover a segment spanning two lines.

## Plan P04-T12c (Planner, 2026-11-01)
Task: silence/no-input policy (specs/07 §12) and DTMF input (§13) in `CallPipeline`. Depends on T12b (ticked).

### Decisions (record in specs/07 as `> Decision (2026-11-01): P04-T12c.` right after §13; one block for both)
1. **No-input timer** lives in a new pipeline task `silence` (add to `_tasks()`; update the task list in the
   P04-T12a Decision text "(`inbound`, ..., `watchdog`)" → add `silence`, `dtmf`). State: `_no_input_deadline_ms:
   float | None`, `_silence_wake: asyncio.Event`, `_reprompts: int`.
   - **Armed** by `_arm_no_input()` = `now + config.no_input_timeout_ms` only when the leg is idle: not `_closing`, no
     reply running, `_jobs.empty()`, `not endpointer.turn_open`, `_held is None`, no commit pending (`_commit is
     None`), DTMF buffer empty. Called (a) in `_reply_loop` right after `_flush_held()` before `await self._jobs.get()`
     (= the agent's last utterance completed: `speak` returns only after the mark echo / grace, so this is "after the
     last mark echo"; also covers an opener/reply that produced no audio), (b) at the end of `_on_discarded` (after
     `_flush_held`), (c) after an ignored backchannel in `_route_turn` (after `_flush_held`). `_arm_no_input` always
     re-reads the conditions; it never extends an already-armed deadline (arm only when `None`).
   - **Disarmed** (`deadline = None`, wake set) by: any VAD `speech_start` (`_on_vad`), any DTMF digit accepted into
     the buffer, `_deliver` / any job enqueue, `_closing` (watchdog/EndCall: `_arm` refuses anyway).
   - **Expiry** (silence loop: deadline reached, re-check idle conditions; if not idle → just clear): if
     `_reprompts < config.max_reprompts` → `_reprompts += 1`, `stats.no_input_reprompts += 1`, log info
     `pipeline_no_input_reprompt` (count), enqueue `_NoInputJob()`; else log info `pipeline_no_input_timeout`, enqueue
     `_GoodbyeJob()`. `max_reprompts=0` → goodbye at first expiry.
   - `_NoInputJob` → `_run_reply(self._no_input_outputs(), None, None)` where `_no_input_outputs` is an async
     generator: iterate `self._brain.on_no_input()` (closing it in `finally` with `_close`), yield each output; if the
     Brain yielded nothing, yield `SpeakStatic(timing.no_input_prompt or config.no_input_prompt)` (node override, §12
     default "Are you still there?"). Reprompt is interruptible (default SpeakStatic).
   - `_GoodbyeJob` → `_run_reply(_single(EndCall(config.no_input_goodbye, "no_input_timeout")), None, None)`; the
     existing `_end_call` path plays it non-interruptibly (bounded by `final_line_timeout_s`) and ends the pipeline
     with `end_reason="no_input_timeout"` (already in `EndReason`). Goodbye text = `config.no_input_goodbye`
     (org setting default, P04-T12a decision already supersedes the §12 literal; restate it in the block).
   - **Reprompt count reset**: in `_deliver` (every turn handed to the Brain: speech, held, merged, DTMF) →
     `_reprompts = 0`. VAD speech that produces no turn (discarded noise, ignored backchannel, false barge-in) only
     disarms/re-arms the timer and does not reset the count. ("Any user speech" read as "speech that becomes a turn".)
   - After a reprompt reply completes the reply loop re-arms → second expiry etc.
2. **DTMF** in `_read_transport` (`InboundDtmf` branch → `await self._on_dtmf(event.digit)`), buffered with a new
   `dtmf` task (same shape as the endpoint loop: `_dtmf_wake` event, `_dtmf_deadline_ms`).
   - `config.ignore_button_press` → drop: `stats.ignored_dtmf += 1`, log debug `pipeline_dtmf_ignored` (keep the
     event name). Digits outside `"0123456789*#ABCD"` → dropped with log warning `pipeline_dtmf_invalid` (no stats).
   - Barge-in first: if `speaker.is_playing(now)`: `level = self._level()`; `decision =
     self._monitor.evaluate_dtmf(playing=True, level=level)` (new monitor method wrapping
     `InterruptPolicy.should_interrupt_dtmf`, returns the decision or `None` when falsy) → `await
     self._barge_in(decision, now, level, speech_start_ms=now, resumable=False)`. Refactor `_barge_in` to take
     `speech_start_ms: float | None` (speech path passes `self._monitor.speech_started_ms`) and `resumable: bool =
     True`; a DTMF barge-in never arms the false-barge-in resume (digits are real input). reaction_ms = clear − digit
     time. Level 0 / non-interruptible → no interrupt; the digit is still buffered (turn held behind playback by the
     normal routing, §7 level-0 rule).
   - Buffer: `_dtmf_digits: list[str]`, `_dtmf_first_ms`, `_dtmf_last_ms`. `#` terminates: flush the buffer now
     (`#` itself is not part of `dtmf`); a `#` with an empty buffer is ignored (debug log). Other digits (incl. `*`)
     append and set `_dtmf_deadline_ms = now + DTMF_SEQUENCE_TIMEOUT_MS` (module constant 2000, exported) and wake the
     dtmf loop; disarm the no-input timer.
   - Flush (`_flush_dtmf()` async): build `UserTurn(index=self._next_index(), text=f"[DTMF: {' '.join(digits)}]",
     start_ms=first, end_ms=last, dtmf="".join(digits))`, `stats.dtmf_turns += 1`, log info `pipeline_dtmf_turn`
     (digits count only — never log the digits: they may be PINs/card numbers), then `await self._route_turn(turn,
     None, overlapped=False)`. DTMF turns carry no latency timer (`_route_turn` / `_TurnJob` must accept
     `timer: TurnTimer | None`); never classified as backchannel (overlapped=False). Routing (closing / hold behind
     audible reply / merge with a reply still generating) is the same as for speech turns.
   - `merge_turns`: `dtmf` = concatenation of both non-None parts (`None` when both are None) so a held DTMF turn
     merged with speech (or another DTMF turn) keeps every digit; text concatenation unchanged.
   - Stream end (`_read_transport` after the loop): pending digits are dropped (call over) — no flush.
3. Brain protocol: `on_no_input` docstring: "an empty stream lets the pipeline speak the node's / default
   reprompt". No new Brain method (DTMF stays a UserTurn, P04-T12a decision).

### Files
- `packages/va_voice/src/va_voice/pipeline/pipeline.py`: new tasks `_silence_loop`, `_dtmf_loop`; `_on_dtmf`,
  `_flush_dtmf`, `_arm_no_input`, `_disarm_no_input`, `_on_no_input_expired`, `_no_input_outputs`; jobs
  `_NoInputJob`, `_GoodbyeJob` (frozen slots dataclasses, no fields) in the `_Job` union; `_reply_loop` dispatch via a
  small `_outputs_for(job) -> tuple[AsyncIterator[BrainOutput], TurnTimer | None, UserTurn | None, bool]` helper to
  keep radon ≤ B; module docstring paragraphs for silence + DTMF. `DTMF_SEQUENCE_TIMEOUT_MS: Final = 2000`,
  `DTMF_TERMINATOR: Final = "#"` exported in `__all__`. If pipeline.py grows past ~850 lines, move the DTMF buffer
  into a pure `va_voice/pipeline/dtmf.py` `DtmfBuffer` (`press(digit, now_ms) -> DtmfSequence | None`,
  `deadline_ms`, `poll(now_ms) -> DtmfSequence | None`, `clear()`, `DtmfSequence(digits, first_ms, last_ms)` with
  `.text`) — preferred: do this anyway (pure, unit-testable, keeps pipeline small).
- `pipeline/barge_in.py`: `BargeInMonitor.evaluate_dtmf(*, playing: bool, level: int) -> InterruptDecision | None`;
  `merge_turns` dtmf concatenation.
- `pipeline/models.py`: `PipelineStats` + `no_input_reprompts`, `dtmf_turns`, `ignored_dtmf`.
- `pipeline/protocols.py`: Brain.on_no_input docstring.
- `pipeline/__init__.py`: export `DTMF_SEQUENCE_TIMEOUT_MS`, `DtmfBuffer`, `DtmfSequence` (if dtmf.py).
- specs/07 Decision block after §13; AGENTS.md bullet (P04-T12c: silence/DTMF entry points + harness helpers).

### Harness / fakes
- `ScriptedBrain(no_input=[[...outputs...], ...])`: separate deque for `on_no_input` (stop popping `replies`
  there); logs `("brain.no_input", t)`; empty deque → yields nothing (pipeline fallback). Keep `no_input_calls`.
- `FakeCaller.press(digit)` already exists. Add `Harness.press(*digits)` convenience? optional.
- Existing tests: the timer now fires after 8 s of idle virtual time. Grep tests whose `until`/run spans > 8 s idle
  after the last utterance (e.g. watchdog tests with `max_duration_s` small, long idle `until(now >= ...)`): give
  them `PipelineConfig(no_input_timeout_ms=30_000)` or bigger max_reprompts as needed, without weakening asserts.
  Update `test_dtmf_and_unknown_marks_do_not_disturb_the_turn` (a "5" now becomes a DTMF turn after 2 s: either
  assert it, or set `ignore_button_press=True`; rename accordingly).

### Tests `packages/va_voice/tests/pipeline/test_pipeline_silence.py`, `test_pipeline_dtmf.py`, `test_dtmf_buffer.py`
Silence (AcceleratedClock harness; `no_input_timeout_ms=3000` to keep runs short):
- `test_silence_reprompts_then_hangs_up`: opener "Hi."; caller silent → `brain.no_input` at ≈ opener end + 3000 ms
  (assert window ±40 ms vs the opener's AgentTurn.ended_ms), Brain yields nothing → agent turn text "Are you still
  there?"; second reprompt; third expiry → goodbye `config.no_input_goodbye` spoken (agent turn, not interrupted) and
  `result.end_reason == "no_input_timeout"`, `stats.no_input_reprompts == 2`.
- `test_brain_reprompt_and_node_prompt`: Brain `no_input` script yields `SpeakStatic("Hello?")` → spoken; with empty
  script and `BrainTiming(no_input_prompt="Still with me?")` → that text.
- `test_speech_resets_reprompt_count`: max_reprompts=1; expiry → reprompt; caller turn delivered (reply "Okay.");
  expiry → reprompt again (count reset) not goodbye; next expiry → goodbye.
- `test_speech_start_postpones_timer`: caller noise (VAD speech then discarded turn, e.g. Said("") / short burst
  below endpointer?) at 2000 ms of a 3000 ms timer → no reprompt at 3000; reprompt ≈ 3000 ms after the discard;
  count unchanged.
- `test_timer_not_armed_while_reply_or_turn_open`: slow reply (SpeakText tokens with long per-token delay > timeout)
  → no reprompt during the reply; caller speaking a 4 s utterance → no reprompt mid-turn.
- `test_max_reprompts_zero_hangs_up_at_first_expiry`; `test_no_input_not_during_closing` (EndCall in progress / watchdog).
- `test_reprompt_is_interruptible` (barge-in on the reprompt works, no crash).
DTMF:
- `test_dtmf_sequence_delivered_after_timeout`: press 1,2,3,4 at 100 ms spacing → Brain gets UserTurn text
  `"[DTMF: 1 2 3 4]"`, `dtmf == "1234"`, delivered ≈ 2000 ms after the last digit (window), stats.dtmf_turns 1,
  observer.user_turns has it; no latency report for it.
- `test_hash_terminates_sequence`: 9,8,# → delivered at once (same tick-ish, < 40 ms after `#`), `dtmf == "98"`;
  lone `#` ignored; `*` kept (`"*5"`).
- `test_ignore_button_press_drops_digits`: no Brain turn after 3 s, `stats.ignored_dtmf == n`, no barge-in while
  playing.
- `test_dtmf_interrupts_playback_at_level_1`: config interruptibility=1, long opener (SpeakText(hanging(...)) or long
  static) playing → press "1" → `transport.clear` + `InterruptRecord(reason="dtmf")`, reaction_ms ≤ 120 (clear −
  press), no false-barge-in resume afterwards, Brain then gets `"[DTMF: 1]"`.
- `test_dtmf_does_not_interrupt_at_level_0` (and non-interruptible SpeakStatic at level 4): no clear, opener
  completes, then the DTMF turn is delivered after playback (held).
- `test_invalid_digit_dropped` (log `pipeline_dtmf_invalid`), `test_digits_reset_reprompts` and `test_dtmf_disarms_
  no_input` (pressing at 2.5 s of 3 s timer → no reprompt before the DTMF turn), `test_pending_digits_dropped_at_
  transport_close`, `test_dtmf_merges_with_held_speech` (merge_turns keeps dtmf).
Pure: `test_dtmf_buffer.py` (press/poll/deadline/terminator/empty terminator/clear/text format) and monitor
`evaluate_dtmf` + `merge_turns` dtmf cases in `test_barge_in_monitor.py`.
Coverage: pipeline modules ≥ 95 % (aim 100 %).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_voice -q` (3× xdist on tests/pipeline for
flakiness), `make test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`, `uvx radon cc -s -a
packages/va_voice/src/va_voice/pipeline`, pip-audit + `corepack pnpm audit` (no new deps).

### Risks
- Existing harness tests idle > 8 s virtual now get reprompts/hang-ups → adjust configs, not assertions.
- AcceleratedClock: silence/dtmf loops must sleep via `wait_until_set` on the injected clock (never busy-loop) or
  virtual time freezes. Re-check idle conditions at expiry (a job may have been queued in the same tick).
- `_arm_no_input` called from `_reply_loop` while `_route_turn` runs on another task: both mutate only sync state; keep
  arm/disarm synchronous.
- DTMF barge-in inside `_read_transport` awaits `_cancel_reply` — same as speech barge-in, already safe.
- Never log digit values (PII).

## Build P04-T12c (Builder, 2026-11-01)
Implemented per plan. New pure modules `pipeline/silence.py` (`NoInputTimer`: arm-once, disarm, due, expire →
reprompt|goodbye, reset) and `pipeline/dtmf.py` (`DtmfBuffer`, `DtmfSequence.text`, `is_dtmf_digit`,
`DTMF_SEQUENCE_TIMEOUT_MS`, `DTMF_TERMINATOR`). `CallPipeline` gains tasks `silence` and `dtmf`, a shared
`_until(wake, deadline)` helper (endpoint loop uses it too), `_idle()`/`_arm_no_input()`/`_disarm_no_input()`,
`_NoInputJob`/`_GoodbyeJob` dispatched by `_run_job`, `_no_input_outputs` (Brain → node prompt → default),
`_on_dtmf` (ignore / invalid / barge-in level ≥ 1 with `resumable=False` / buffer / `#`), `_route_dtmf`.
`_route_turn` is serialised by an asyncio.Lock (speech + keypad turns from different tasks) and takes
`timer: TurnTimer | None`. `BargeInMonitor.evaluate_dtmf`; `merge_turns` joins `dtmf`. Stats `no_input_reprompts`,
`dtmf_turns`, `ignored_dtmf`. Pending digits dropped at transport close (`pipeline_dtmf_dropped`, info). Digits never
logged. Decision block in specs/07 after §13; P04-T12a task list updated; AGENTS.md bullet.
Harness: `ScriptedBrain(no_input=[[...]])` (no longer pops `replies`), logs `brain.no_input`. Only existing test touched:
`test_max_duration_warns_then_ends` (idles 20 s → `no_input_timeout_ms=30_000`).
Tests: `test_no_input_dtmf_units.py` (pure), `test_pipeline_silence.py` (11), `test_pipeline_dtmf.py` (10).
Gates: fmt/lint pass; typecheck pass (887 files); placeholders 0; make test 7413 passed / 7 skipped, coverage 98.55 %
(pipeline.py 97 %, dtmf/silence 100 %); pipeline tests 3× xdist green; spec refs 0; jscpd 0 clones; radon avg A (2.18);
pip-audit clean; pnpm audit 3 moderate (high threshold passes).

## Review P04-T12c (Reviewer, 2026-11-01)
APPROVED. Gates re-run: make lint/typecheck/placeholders pass; make test 7413 passed / 7 skipped, coverage 98.54 %;
pipeline tests 2x xdist green (132 passed). Diff checked against specs/07 §12/§13: timer armed only when idle after
playback, disarmed on speech_start/digit/job, reprompt via Brain → node prompt → default, goodbye EndCall
no_input_timeout; DTMF 2 000 ms / `#`, ignore_button_press, barge-in level ≥ 1 non-resumable, digits never logged.

## Plan P04-T13 (Planner, 2026-11-01)
Task: pipeline benchmark (`bench` marker) + generated `packages/va_voice/BENCHMARKS.md`. Spec: specs/07 §21 (CPU
budget, "no frame > 30 ms late"), §22 last bullet (overhead = e2e − vendor latencies), phase exit gate (overhead ≤ 30 ms
p50 with zero-latency fakes; 50 concurrent harness pipelines on one loop, no frame deadline misses).
No production code changes are expected. `CallPipeline.speaker.outbound.stats` (`OutboundStats.late_frames`,
`max_lateness_ms`, `LATE_THRESHOLD_MS = 30`) and `LatencyTracer` reports (`observer.reports[i].latency`) give everything.

### Decisions (record as `> Decision (2026-11-01) P04-T13:` at the end of specs/07 §21)
1. **Platform overhead per turn** = `e2e_ms − eot_ms − (stt_final_ms + llm_ttft_ms + tts_ttfa_ms)` (unmeasured
   vendor values count as 0) from the turn's `TurnLatency`. Endpointing silence (`eot_ms`) is policy, not overhead.
   With zero-latency fakes the vendor terms are 0.
2. Two overhead runs:
   (a) **harness time**: AcceleratedClock, `BENCH_TURNS = 200` turns in one call (deterministic scheduling cost:
   pacer slot alignment, task hops measured in virtual ms);
   (b) **wall clock**: `va_sim.clock.RealtimeClock`, `BENCH_REALTIME_TURNS = 30` turns: the same formula on real
   time, so it includes the actual CPU/event-loop cost. This is the number gated by the exit criterion; (a) is also
   asserted ≤ 30 ms p50.
   Both report p50 / p95 / max; assert p50 ≤ `OVERHEAD_P50_LIMIT_MS = 30` and that every turn was measured
   (`len(reports) == turns`, `e2e_ms is not None`).
3. **Concurrency run**: `BENCH_CONCURRENT_CALLS = 50` harness pipelines on ONE event loop with RealtimeClock, each
   3 turns (speech 600 ms, short reply). Assert: every pipeline delivers 3 user turns and 3 agent turns, ends with the
   harness closing it (no error), Σ `outbound.stats.late_frames == 0` (no frame > 30 ms past its target), and
   max(`max_lateness_ms`) is reported. An event-loop lag probe task (sleep 10 ms, record overshoot) runs alongside:
   report lag p50/p99/max (informational; the §21 loop-lag assert belongs to the phase-05 load test). Also report
   frames sent total and wall duration.
4. Zero-latency fakes: `ttfa_s=0`, `commit_latency_s=0`, brain replies `SpeakText(tokens(REPLY))` with no delays;
   `PipelineConfig(no_input_timeout_ms=30_000)` (max allowed? check NO_INPUT_TIMEOUT_MAX_MS; use the max) so the
   timer never fires during a bench; echo_delay default.
5. **Bench tests are deselected unless the mark expression mentions `bench`**:
   `packages/va_voice/tests/bench/conftest.py` `pytest_collection_modifyitems(config, items)`: if `"bench" not in
   (config.option.markexpr or "")` → move bench-marked items to `config.hook.pytest_deselected(items=...)`. So
   `make test` never runs them (timing asserts under xdist load would flake) and `pytest -m bench packages/va_voice`
   does. Register the marker in root pyproject `markers`: `"bench: performance benchmarks, run with -m bench"`.
   (Check the hook works with `--import-mode=importlib` conftest in a subdir; a conftest hook there only sees its own
   items — filter items whose path is under the bench dir / have the marker.)
6. **BENCHMARKS.md generation**: each bench test stores a `BenchSection(title, rows: list[tuple[str, str]], verdict)`
   in a session-scoped `bench_report` fixture (bench/conftest.py). At session teardown, when no section is missing
   and not under xdist (`PYTEST_XDIST_WORKER` unset), render with `pipeline_bench.render_report(sections, machine)`
   and write to `os.environ.get("VA_BENCH_REPORT", <repo>/packages/va_voice/BENCHMARKS.md)`. If a section was not
   run (e.g. `-k`), do not write (prints nothing; avoids clobbering with a partial report). `machine` =
   `platform.platform()`, `platform.python_version()`, `os.cpu_count()`, `platform.processor()`/machine, UTC date
   (`datetime.now(UTC)` OK in test code). Header says the file is generated by
   `uv run pytest -m bench packages/va_voice` and must not be edited by hand. Builder runs the bench and commits the
   generated file.

### Files
- `packages/va_voice/tests/support/pipeline_harness.py`: `harness(..., clock: AcceleratedClock | RealtimeClock |
  None = None)` (default AcceleratedClock); `Harness.clock` typed as the union (or a `Clock` protocol with `sleep`
  + `monotonic`); `latency: Callable[[Clock], LatencyTracer]`. `until` already uses `clock.sleep` → works realtime.
  Existing tests unchanged.
- `packages/va_voice/tests/support/pipeline_bench.py` (new, on pythonpath): constants above; `REPLY = "Sure, I can
  help with that."`; `bench_brain(turns) -> ScriptedBrain` (fresh `SpeakText(tokens(REPLY))` per turn);
  `bench_utterances(turns)`; `turn_overhead_ms(latency: TurnLatency) -> float | None`;
  `async def run_conversation(*, clock, turns) -> ConversationResult(overheads: list[float], frames_sent,
  late_frames, max_lateness_ms, user_turns, agent_turns)` (opens `harness(...)` with a `LatencyTracer(clock)`,
  loops `await h.turn(600)` + `await h.until(lambda: len(h.observer.agent_turns) >= i+1)`, reads reports and
  `h.pipeline.speaker.outbound.stats`); `percentile(values, q)` (nearest-rank, pure; or numpy.percentile
  method="linear" — pick one and state it in the report); `class LoopLagProbe` (`async run(stop: asyncio.Event)`,
  `samples_ms`, uses `time.perf_counter` + `asyncio.sleep(0.01)` — real time intentionally);
  `BenchSection`, `Machine`, `render_report(sections, machine) -> str` (markdown tables, deterministic order).
- `packages/va_voice/tests/bench/conftest.py` (deselect hook + `bench_report` session fixture + writer).
- `packages/va_voice/tests/bench/test_pipeline_bench.py`: `pytestmark = pytest.mark.bench`; tests
  `test_turn_overhead_harness_time`, `test_turn_overhead_wall_clock` (`@pytest.mark.timeout(300)`),
  `test_fifty_concurrent_pipelines_without_late_frames` (`@pytest.mark.timeout(300)`). Each asserts its thresholds
  AFTER storing its section (so a failing run still records the numbers... but the report is written only when all
  pass? → write whenever all three sections exist; verdict column PASS/FAIL).
- `packages/va_voice/tests/pipeline/test_pipeline_bench_support.py` (normal suite, fast): `turn_overhead_ms`
  (subtracts vendor terms, None when e2e/eot missing), `percentile` cases, `render_report` golden-ish asserts
  (title, tables, generated-by line), `run_conversation(clock=AcceleratedClock(), turns=3)` gives 3 overheads ≤ 30,
  0 late frames; deselect hook test via `pytester`? (pytester plugin may not be enabled — instead unit-test a pure
  `select_bench(items_markers, markexpr)` helper if the hook logic is factored; keep it simple).
- `pyproject.toml`: `bench` marker. (mypy `files` already covers packages/va_voice/tests? verify the new `bench`
  dir is included; `tests/bench/` has no `__init__` like other test dirs.)
- `packages/va_voice/BENCHMARKS.md` (generated, committed).
- specs/07 §21 Decision; AGENTS.md: Commands row `Pipeline benchmark | uv run pytest -m bench packages/va_voice
  (rewrites packages/va_voice/BENCHMARKS.md)` + a P04-T13 learned bullet.

### Gates
`make fmt lint typecheck placeholders`; `uv run pytest -m bench packages/va_voice -p no:randomly` (run twice, report
numbers); `uv run pytest packages/va_voice -q` (confirm bench deselected, count unchanged + new support tests);
`make test`; spec refs; jscpd; radon; pip-audit + pnpm audit.

### Risks
- 50 realtime pipelines on one loop: inbound 2 500 frames/s + outbound 2 500 frames/s (soxr resample, EnergyVad, mixer)
  may saturate the loop and produce late frames. That would be a real finding: profile (`cProfile` on the concurrency
  run) and optimise the hot path (e.g. avoid per-frame allocations) rather than loosening the 30 ms threshold.
  If it truly cannot meet it, emit build.blocked with profile numbers.
- The realtime harness `until` polls in 20 ms sleeps; fine. FakeCaller streaming on RealtimeClock uses
  `clock.sleep(0.02)` drift (accumulates) — acceptable for inbound; outbound pacing is the pipeline's own.
- Do not let the bench conftest hook deselect non-bench items or run under `make test` (verify test count).
- Writing BENCHMARKS.md from a test touches the repo: only at session end, only with all sections, never under xdist.

## Build P04-T13 (Builder, 2026-11-01)
Implemented per plan, test code only (no production changes). `harness(clock=...)` accepts `RealtimeClock`
(`HarnessClock` union). `tests/support/pipeline_bench.py`: constants, `turn_overhead_ms` (spec formula) +
`gross_overhead_ms` (e2e − eot: with zero-latency fakes the "vendor" terms are in-process fake time, so the gross is
gated too — deviation from plan, recorded in the Decision), nearest-rank `percentile`, `run_conversation`,
`LoopLagProbe`, `BenchSection`/`Machine`/`render_report`. `tests/bench/conftest.py` deselects bench items unless `-m`
mentions bench; session `bench_report` writes BENCHMARKS.md when all 3 sections exist and not under xdist.
`tests/bench/test_pipeline_bench.py` (3 tests), `tests/pipeline/test_pipeline_bench_support.py` (fast, normal suite).
`bench` marker in pyproject; specs/07 §21 Decision; AGENTS.md row + bullet. BENCHMARKS.md generated.
Results: harness-time overhead p50 0.0 ms (200 turns); wall clock p50 1.0 ms (30 turns); 50 concurrent realtime
pipelines: 0 late frames, max lateness 2.0 ms, gross overhead p50 6.0 ms, loop lag p99 2.2 ms. Bench run twice: 3 passed.
Gates: fmt/lint/typecheck (891 files)/placeholders pass; make test 7430 passed / 7 skipped, coverage 98.55 %;
packages/va_voice 910 passed / 3 deselected; spec refs 0; jscpd 0 clones; radon avg A (3.71); pip-audit clean;
pnpm audit 3 moderate (high threshold passes).

## Review P04-T13 (Reviewer, 2026-11-01)
APPROVED. Re-ran: make lint/typecheck/placeholders pass; make test 7430 passed / 7 skipped, coverage 98.55 %;
`pytest -m bench packages/va_voice` 3 passed (to VA_BENCH_REPORT=/var/tmp): overhead p50 0.0 ms harness / 0.0 ms
wall, 50 concurrent: 0 late frames, max lateness 1.8 ms, gross p50 7 ms; packages/va_voice normal run 910 passed /
3 deselected; spec refs 0; jscpd 0 clones. Test-only change; Decision in specs/07 §21. All P04 tasks ticked →
Planner should verify the phase exit gate (make check + bench) and print LOOP_COMPLETE.
