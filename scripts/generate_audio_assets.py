"""Procedurally synthesize the background tracks and hold music (specs/07 §10.2, specs/06 §10.2).

Every track is a 60 s seamless loop written as raw G.711 μ-law at 8 kHz (`<track>.ulaw`) and raw
little-endian int16 mono at 16 kHz (`<track>.pcm16k`); hold music is also written as a 16 kHz WAV for
Telnyx `playback_start`. No third-party audio is used. Output is byte-identical across runs: all randomness
comes from the raw output of `numpy.random.PCG64` (stable across numpy versions, unlike `Generator`
methods) and float conversion is ours.

Modes: default writes files whose content changed and prints `wrote`/`unchanged` per file; `--check`
renders in memory and exits 1 when a committed file is missing or differs; `--out DIR` picks another
output directory.
"""

from __future__ import annotations

import argparse
import functools
import io
import math
import sys
import wave
import zlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
OUT_DIR: Final = REPO_ROOT / "assets" / "audio"

SR: Final = 16_000
SR_ULAW: Final = 8_000
LOOP_S: Final = 60
XFADE_S: Final = 0.5
SEED: Final = 20261025
TELEPHONE_LO_HZ: Final = 300.0
TELEPHONE_HI_HZ: Final = 3400.0
BROWN_HIGHPASS_HZ: Final = 20.0
PEAK_LIMIT: Final = 10 ** (-1.05 / 20)

TRACKS: Final = ("static", "office", "cafe", "restaurant", "hold_music")
TARGET_DBFS: Final[Mapping[str, float]] = {
    "static": -40.0,
    "office": -22.0,
    "cafe": -22.0,
    "restaurant": -22.0,
    "hold_music": -18.0,
}

OFFICE_CLICK_RATE_HZ: Final = 4.0
CLINK_RATE_HZ: Final = 0.6

# Hold music: I-vi-IV-V in C major, two bars per chord, 64 BPM so two progressions fill the 60 s loop exactly.
HOLD_BPM: Final = 64
BEATS_PER_BAR: Final = 4
BARS_PER_CHORD: Final = 2
# MIDI notes per chord: (bass, pad triad).
HOLD_CHORDS: Final[tuple[tuple[int, tuple[int, int, int]], ...]] = (
    (48, (60, 64, 67)),  # C
    (45, (57, 60, 64)),  # A minor
    (41, (53, 57, 60)),  # F
    (43, (55, 59, 62)),  # G
)
ARPEGGIO_PATTERN: Final = (0, 1, 2, 1, 0, 1, 2, 1)
HARMONICS: Final = ((1, 1.0), (2, 10 ** (-9 / 20)), (3, 10 ** (-15 / 20)))

Signal = NDArray[np.float64]


class Rng:
    """Deterministic random streams built only on raw PCG64 output."""

    def __init__(self, seed: int) -> None:
        self._bits = np.random.PCG64(seed)

    def uniform(self, n: int) -> Signal:
        """`n` floats in [0, 1) from the top 53 bits of each raw 64-bit draw."""
        raw = np.asarray(self._bits.random_raw(n), dtype=np.uint64)
        return (raw >> np.uint64(11)).astype(np.float64) * 2.0**-53

    def between(self, lo: float, hi: float, n: int = 1) -> Signal:
        return lo + (hi - lo) * self.uniform(n)

    def normal(self, n: int) -> Signal:
        """Standard normal samples via Box-Muller."""
        u1 = 1.0 - self.uniform(n)
        u2 = self.uniform(n)
        return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)

    def poisson_times(self, rate_hz: float, duration_s: float) -> Signal:
        """Event times of a Poisson process in [0, duration_s)."""
        count = math.ceil(rate_hz * duration_s * 2 + 20)
        gaps = -np.log(1.0 - self.uniform(count)) / rate_hz
        times = np.cumsum(gaps)
        return times[times < duration_s]


def track_seed(track: str) -> int:
    return zlib.crc32(track.encode("ascii")) ^ SEED


def rms(x: Signal) -> float:
    return float(np.sqrt(np.mean(np.square(x))))


def unit(x: Signal) -> Signal:
    """Scale to unit RMS."""
    return x / rms(x)


def shape_spectrum(x: Signal, sr: int, weight: Callable[[Signal], Signal]) -> Signal:
    """Multiply the spectrum by `weight(freqs)` (circular FFT filter, so loops stay seamless)."""
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    return np.fft.irfft(spectrum * weight(freqs), n=len(x))


def bandpass(x: Signal, lo: float, hi: float, sr: int = SR) -> Signal:
    return shape_spectrum(x, sr, lambda f: ((f >= lo) & (f <= hi)).astype(np.float64))


def pink(n: int, rng: Rng) -> Signal:
    return shape_spectrum(
        rng.normal(n), SR, lambda f: np.where(f >= 1.0, 1.0 / np.sqrt(np.maximum(f, 1.0)), 0.0)
    )


def brown(n: int, rng: Rng) -> Signal:
    return shape_spectrum(
        rng.normal(n),
        SR,
        lambda f: np.where(f >= BROWN_HIGHPASS_HZ, 1.0 / np.maximum(f, BROWN_HIGHPASS_HZ), 0.0),
    )


def envelope(n: int, rng: Rng, f_lo: float = 3.0, f_hi: float = 6.0) -> Signal:
    """Slow speech-like amplitude envelope in [0.2, 1.0] from three random sinusoids."""
    t = np.arange(n) / SR
    freqs = rng.between(f_lo, f_hi, 3)
    phases = rng.between(0.0, 2.0 * np.pi, 3)
    waves = np.sin(2.0 * np.pi * freqs[:, None] * t[None, :] + phases[:, None])
    mean = waves.mean(axis=0)
    return 0.2 + 0.8 * (mean + 1.0) / 2.0


def murmur(n: int, rng: Rng) -> Signal:
    """Distant voices: 200-2 000 Hz noise modulated by 3-6 Hz envelopes."""
    return unit(bandpass(rng.normal(n), 200.0, 2000.0)) * envelope(n, rng)


def _place(buf: Signal, start: int, event: Signal) -> None:
    end = min(len(buf), start + len(event))
    buf[start:end] += event[: end - start]


def clicks(n: int, rng: Rng, rate_hz: float = OFFICE_CLICK_RATE_HZ) -> Signal:
    """Keyboard clicks: 5-20 ms white-noise bursts with exponential decay at Poisson onsets."""
    buf = np.zeros(n)
    for onset in rng.poisson_times(rate_hz, n / SR):
        length = int(rng.between(0.005, 0.020)[0] * SR)
        gain = rng.between(0.3, 1.0)[0]
        decay = np.exp(-np.arange(length) / (length / 4.0))
        _place(buf, int(onset * SR), gain * rng.normal(length) * decay)
    return buf


def clinks(n: int, rng: Rng, rate_hz: float = CLINK_RATE_HZ) -> Signal:
    """Glass and cutlery: decaying 2-5 kHz sinusoids (40-120 ms decay) at Poisson onsets."""
    buf = np.zeros(n)
    for onset in rng.poisson_times(rate_hz, n / SR):
        freq = rng.between(2000.0, 5000.0)[0]
        tau = rng.between(0.040, 0.120)[0]
        gain = rng.between(0.3, 1.0)[0]
        t = np.arange(int(5 * tau * SR)) / SR
        _place(buf, int(onset * SR), gain * np.sin(2.0 * np.pi * freq * t) * np.exp(-t / tau))
    return buf


def rumble(n: int, rng: Rng) -> Signal:
    return bandpass(brown(n, rng), 20.0, 120.0)


def make_loop(x: Signal, n: int) -> Signal:
    """Fold the `len(x) - n` extra samples into the loop start with an equal-power cross-fade."""
    nx = len(x) - n
    theta = (np.pi / 2.0) * np.arange(nx) / nx
    out = x[:n].copy()
    out[:nx] = x[n:] * np.cos(theta) + x[:nx] * np.sin(theta)
    return out


def _noise_track(track: str, n: int, rng: Rng) -> Signal:
    if track == "static":
        return bandpass(pink(n, rng), TELEPHONE_LO_HZ, TELEPHONE_HI_HZ)
    if track == "office":
        return unit(brown(n, rng)) + 0.35 * unit(clicks(n, rng)) + 0.5 * murmur(n, rng)
    if track == "cafe":
        return unit(murmur(n, rng)) + 0.5 * unit(clinks(n, rng))
    return 1.5 * unit(murmur(n, rng)) + 0.5 * unit(clinks(n, rng)) + 0.5 * unit(rumble(n, rng))


def midi_hz(note: int) -> float:
    return float(440.0 * 2.0 ** ((note - 69) / 12))


def adsr(n_on: int, release: int) -> Signal:
    """Attack 20 ms, decay 150 ms to 0.6 sustain over `n_on` samples, then a linear release."""
    attack, decay = int(0.020 * SR), int(0.150 * SR)
    env = np.full(n_on + release, 0.6)
    env[:attack] = np.linspace(0.0, 1.0, attack, endpoint=False)
    env[attack : attack + decay] = np.linspace(1.0, 0.6, decay, endpoint=False)
    env[n_on:] = np.linspace(0.6, 0.0, release)
    return env


def tone(note: int, n_on: int) -> Signal:
    """Soft additive tone with harmonics kept below the telephone band edge."""
    release = int(0.300 * SR)
    t = np.arange(n_on + release) / SR
    f0 = midi_hz(note)
    out = np.zeros_like(t)
    for k, gain in HARMONICS:
        if f0 * k < TELEPHONE_HI_HZ:
            out += gain * np.sin(2.0 * np.pi * f0 * k * t)
    return out * adsr(n_on, release)


def _place_wrapped(buf: Signal, start: int, event: Signal) -> None:
    idx = (start + np.arange(len(event))) % len(buf)
    np.add.at(buf, idx, event)


def hold_music(n: int) -> Signal:
    """I-vi-IV-V pad, bass, and eighth-note arpeggio, rendered circularly so the loop is seamless."""
    beat = SR * 60 // HOLD_BPM
    chord_len = beat * BEATS_PER_BAR * BARS_PER_CHORD
    eighth = beat // 2
    buf = np.zeros(n)
    for index in range(n // chord_len):
        bass, triad = HOLD_CHORDS[index % len(HOLD_CHORDS)]
        start = index * chord_len
        _place_wrapped(buf, start, 0.5 * tone(bass, chord_len))
        for note in triad:
            _place_wrapped(buf, start, 0.25 * tone(note, chord_len))
        for step in range(chord_len // eighth):
            note = triad[ARPEGGIO_PATTERN[step % len(ARPEGGIO_PATTERN)]] + 12
            _place_wrapped(buf, start + step * eighth, 0.35 * tone(note, eighth))
    return buf


@functools.cache
def master(track: str) -> Signal:
    """The unleveled 16 kHz loop for `track`."""
    n = LOOP_S * SR
    if track == "hold_music":
        return hold_music(n)
    rng = Rng(track_seed(track))
    return make_loop(_noise_track(track, n + int(XFADE_S * SR), rng), n)


def level(x: Signal, target_dbfs: float) -> Signal:
    """Scale to the target RMS; soft-limit peaks above -1 dBFS (tanh), which barely moves the RMS."""
    y = x * (10 ** (target_dbfs / 20) / rms(x))
    if float(np.max(np.abs(y))) > PEAK_LIMIT:
        y = PEAK_LIMIT * np.tanh(y / PEAK_LIMIT)
    return y


def decimate_to_8k(x: Signal) -> Signal:
    """Telephone-band low-pass in the FFT domain, then 2:1 decimation."""
    return shape_spectrum(x, SR, lambda f: (f <= TELEPHONE_HI_HZ).astype(np.float64))[::2]


def to_int16(x: Signal) -> NDArray[np.int16]:
    return np.clip(np.rint(x * 32767.0), -32768, 32767).astype(np.int16)


def to_pcm16(x: Signal) -> bytes:
    return to_int16(x).astype("<i2").tobytes()


ULAW_BIAS: Final = 0x84
ULAW_CLIP: Final = 32635
ULAW_SEGMENTS: Final = np.array([256, 512, 1024, 2048, 4096, 8192, 16384])


def ulaw_encode(pcm: NDArray[np.int16]) -> bytes:
    """G.711 μ-law encode (bias 0x84, clip 32635), vectorized."""
    wide = pcm.astype(np.int32)
    sign = np.where(wide < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(wide), ULAW_CLIP) + ULAW_BIAS
    exponent = np.searchsorted(ULAW_SEGMENTS, magnitude, side="right")
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    codes: NDArray[np.uint8] = (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)
    return codes.tobytes()


def ulaw_decode(data: bytes) -> NDArray[np.int16]:
    """G.711 μ-law decode, the inverse of `ulaw_encode`."""
    byte = ~np.frombuffer(data, dtype=np.uint8).astype(np.int32) & 0xFF
    exponent = (byte >> 4) & 0x07
    magnitude = (((byte & 0x0F) << 3) + ULAW_BIAS << exponent) - ULAW_BIAS
    return np.where(byte & 0x80, -magnitude, magnitude).astype(np.int16)


def wav_bytes(pcm16k: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SR)
        wav.writeframes(pcm16k)
    return buf.getvalue()


@functools.cache
def render(track: str) -> Mapping[str, bytes]:
    """File name -> bytes for one track."""
    x = master(track)
    target = TARGET_DBFS[track]
    pcm16k = to_pcm16(level(x, target))
    files = {
        f"{track}.ulaw": ulaw_encode(to_int16(level(decimate_to_8k(x), target))),
        f"{track}.pcm16k": pcm16k,
    }
    if track == "hold_music":
        files[f"{track}.wav"] = wav_bytes(pcm16k)
    return files


def render_all() -> dict[str, bytes]:
    return {name: data for track in TRACKS for name, data in render(track).items()}


def check(out_dir: Path) -> int:
    problems = 0
    for name, data in render_all().items():
        path = out_dir / name
        if not path.is_file():
            print(f"{path}: missing", file=sys.stderr)
            problems += 1
        elif path.read_bytes() != data:
            print(f"{path}: differs from regeneration", file=sys.stderr)
            problems += 1
    return 1 if problems else 0


def write(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in render_all().items():
        path = out_dir / name
        if path.is_file() and path.read_bytes() == data:
            print(f"{path}: unchanged")
            continue
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        print(f"{path}: wrote")
    return 0


def main(argv: Sequence[str] | None = None, *, out_dir: Path = OUT_DIR) -> int:
    parser = argparse.ArgumentParser(description="Generate the procedural audio assets (assets/audio/).")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if files on disk differ from regeneration"
    )
    parser.add_argument("--out", type=Path, default=out_dir, help="output directory")
    args = parser.parse_args(argv)
    return check(args.out) if args.check else write(args.out)


if __name__ == "__main__":
    sys.exit(main())
