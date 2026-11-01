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
