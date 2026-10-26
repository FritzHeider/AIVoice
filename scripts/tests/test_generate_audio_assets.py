"""`scripts/generate_audio_assets.py`: deterministic, correctly levelled, band-limited, seamless loops."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

import generate_audio_assets as gaa

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets" / "audio"
NOISE_TRACKS = ("static", "office", "cafe", "restaurant")


def _pcm16k(track: str) -> NDArray[np.float64]:
    data = gaa.render(track)[f"{track}.pcm16k"]
    return np.frombuffer(data, dtype="<i2").astype(np.float64) / 32767.0


def _ulaw(track: str) -> NDArray[np.float64]:
    return gaa.ulaw_decode(gaa.render(track)[f"{track}.ulaw"]).astype(np.float64) / 32767.0


def _dbfs(x: NDArray[np.float64]) -> float:
    return 20.0 * float(np.log10(gaa.rms(x)))


def _energy_fraction(x: NDArray[np.float64], sr: int, lo: float, hi: float) -> float:
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    return float(power[(freqs >= lo) & (freqs <= hi)].sum() / power.sum())


def test_committed_assets_are_byte_identical_to_regeneration() -> None:
    assert gaa.main(["--check"], out_dir=ASSETS) == 0
    assert sorted(p.name for p in ASSETS.iterdir() if not p.name.startswith(".")) == sorted(gaa.render_all())


def test_rendering_is_deterministic() -> None:
    gaa.master.cache_clear()
    first = gaa.master("cafe").copy()
    gaa.master.cache_clear()
    assert np.array_equal(first, gaa.master("cafe"))


def test_write_then_check_and_idempotent_rewrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gaa.main(["--out", str(tmp_path)]) == 0
    assert capsys.readouterr().out.count(": wrote") == len(gaa.render_all())
    mtimes = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    assert gaa.main(["--out", str(tmp_path)]) == 0
    assert capsys.readouterr().out.count(": unchanged") == len(gaa.render_all())
    assert {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()} == mtimes
    assert gaa.main(["--check", "--out", str(tmp_path)]) == 0


def test_check_detects_modified_and_missing_assets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gaa.main([], out_dir=tmp_path) == 0
    target = tmp_path / "office.ulaw"
    data = bytearray(target.read_bytes())
    data[1000] ^= 0x01
    target.write_bytes(bytes(data))
    (tmp_path / "hold_music.wav").unlink()
    capsys.readouterr()
    assert gaa.main(["--check"], out_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "office.ulaw: differs from regeneration" in err
    assert "hold_music.wav: missing" in err
    assert gaa.main([], out_dir=tmp_path) == 0
    assert gaa.main(["--check"], out_dir=tmp_path) == 0


@pytest.mark.parametrize("track", gaa.TRACKS)
def test_sizes_and_formats(track: str) -> None:
    files = gaa.render(track)
    assert len(files[f"{track}.ulaw"]) == 480_000
    assert len(files[f"{track}.pcm16k"]) == 1_920_000


def test_hold_music_wav_header() -> None:
    data = gaa.render("hold_music")["hold_music.wav"]
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
            1,
            2,
            16_000,
            960_000,
        )
        assert wav.readframes(wav.getnframes()) == gaa.render("hold_music")["hold_music.pcm16k"]


@pytest.mark.parametrize("track", gaa.TRACKS)
def test_levels(track: str) -> None:
    pcm = _pcm16k(track)
    target = gaa.TARGET_DBFS[track]
    assert abs(_dbfs(pcm) - target) <= 0.5
    assert 20.0 * np.log10(np.max(np.abs(pcm))) <= -1.0
    assert abs(_dbfs(_ulaw(track)) - target) <= 1.0


def test_static_is_band_limited() -> None:
    assert _energy_fraction(_pcm16k("static"), gaa.SR, 300.0, 3400.0) > 0.99


@pytest.mark.parametrize("track", gaa.TRACKS)
def test_ulaw_band_limit(track: str) -> None:
    assert _energy_fraction(_ulaw(track), gaa.SR_ULAW, 3450.0, 4000.0) < 0.01


@pytest.mark.parametrize("track", gaa.TRACKS)
def test_loop_seam_is_continuous(track: str) -> None:
    pcm = _pcm16k(track)
    steps = np.abs(np.diff(pcm))
    assert abs(pcm[0] - pcm[-1]) <= np.percentile(steps, 99.9)


def test_stationary_track_has_no_level_dip_at_seam() -> None:
    pcm = _pcm16k("static")
    window = gaa.SR // 50
    around_seam = np.concatenate([pcm[-window // 2 :], pcm[: window // 2]])
    assert abs(_dbfs(around_seam) - _dbfs(pcm)) <= 3.0


def test_make_loop_crossfades_tail_into_head() -> None:
    x = np.arange(12, dtype=np.float64)
    out = gaa.make_loop(x, 8)
    assert len(out) == 8
    assert out[0] == x[8]
    assert np.array_equal(out[4:], x[4:8])


@pytest.mark.parametrize(
    ("pcm", "code"),
    [(0, 0xFF), (1, 0xFF), (-1, 0x7F), (32767, 0x80), (-32768, 0x00), (32635, 0x80), (-129, 0x6F)],
)
def test_ulaw_codec_known_values(pcm: int, code: int) -> None:
    assert gaa.ulaw_encode(np.array([pcm], dtype=np.int16)) == bytes([code])


def test_ulaw_round_trip_error_within_segment_step() -> None:
    ramp = np.arange(-32768, 32768, 7, dtype=np.int32).astype(np.int16)
    decoded = gaa.ulaw_decode(gaa.ulaw_encode(ramp)).astype(np.int32)
    original = np.clip(ramp.astype(np.int32), -gaa.ULAW_CLIP, gaa.ULAW_CLIP)
    magnitude = np.abs(original) + gaa.ULAW_BIAS
    step = 1 << (np.searchsorted(gaa.ULAW_SEGMENTS, magnitude, side="right") + 3)
    assert np.all(np.abs(decoded - original) <= step)
    assert np.array_equal(
        gaa.ulaw_decode(gaa.ulaw_encode(gaa.ulaw_decode(bytes(range(256))))),
        gaa.ulaw_decode(bytes(range(256))),
    )


def test_event_rates_match_spec() -> None:
    duration = float(gaa.LOOP_S)
    click_rate = (
        len(gaa.Rng(gaa.track_seed("office")).poisson_times(gaa.OFFICE_CLICK_RATE_HZ, duration)) / duration
    )
    clink_rate = len(gaa.Rng(gaa.track_seed("cafe")).poisson_times(gaa.CLINK_RATE_HZ, duration)) / duration
    assert 2.0 <= click_rate <= 6.0
    assert 0.2 <= clink_rate <= 1.0


def test_rng_is_stable() -> None:
    rng = gaa.Rng(1)
    expected = np.array([0.5118216247002567, 0.9504636963259353, 0.14415961271963373, 0.9486494471372439])
    assert np.array_equal(rng.uniform(4), expected)


def test_rng_distributions() -> None:
    rng = gaa.Rng(7)
    u = rng.uniform(100_000)
    assert u.min() >= 0.0 and u.max() < 1.0
    z = rng.normal(100_000)
    assert abs(float(z.mean())) < 0.02
    assert abs(float(z.std()) - 1.0) < 0.02


def test_hold_music_is_tonal_and_band_limited() -> None:
    pcm = _pcm16k("hold_music")
    assert _energy_fraction(pcm, gaa.SR, 3400.0, gaa.SR / 2) < 0.01
    power = np.abs(np.fft.rfft(pcm)) ** 2
    freqs = np.fft.rfftfreq(len(pcm), 1.0 / gaa.SR)
    median = float(np.median(power))
    for note in (60, 57, 53, 55):  # C4, A3, F3, G3: the four chord roots in the pad
        hz = gaa.midi_hz(note)
        band = (freqs > hz - 1.0) & (freqs < hz + 1.0)
        assert power[band].max() > 1000 * median


def test_hold_music_loop_holds_whole_progressions() -> None:
    beat = gaa.SR * 60 // gaa.HOLD_BPM
    chord = beat * gaa.BEATS_PER_BAR * gaa.BARS_PER_CHORD
    assert (gaa.LOOP_S * gaa.SR) % (chord * len(gaa.HOLD_CHORDS)) == 0
