"""soxr resampling (specs/07-media-pipeline.md §4: 8 k → 16 k for the VAD, length correctness)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from numpy.typing import NDArray

from va_voice.audio import Resampler, resample, rms_dbfs


def tone(freq_hz: float, rate: int, seconds: float, amplitude: float = 10_000.0) -> NDArray[np.int16]:
    t = np.arange(round(rate * seconds)) / rate
    return np.rint(amplitude * np.sin(2 * math.pi * freq_hz * t)).astype(np.int16)


def stream(resampler: Resampler, x: NDArray[np.int16], chunk: int) -> NDArray[np.int16]:
    parts = [resampler.process(x[i : i + chunk]) for i in range(0, x.size, chunk)]
    return np.concatenate([*parts, resampler.flush()])


def peak_hz(x: NDArray[np.int16], rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x.astype(np.float64)))
    return float(np.fft.rfftfreq(x.size, 1 / rate)[int(np.argmax(spectrum))])


def test_streaming_one_second_in_20_ms_frames_yields_exactly_16000_samples() -> None:
    resampler = Resampler()
    assert (resampler.in_rate, resampler.out_rate) == (8000, 16000)
    out = stream(resampler, tone(1000, 8000, 1.0), 160)
    assert out.dtype == np.int16
    assert out.size == 16_000


def test_streaming_holds_back_the_filter_delay_until_flush() -> None:
    resampler = Resampler()
    early = sum(resampler.process(np.zeros(160, dtype=np.int16)).size for _ in range(50))
    assert early < 16_000
    assert early + resampler.flush().size == 16_000


def test_tone_keeps_its_frequency_and_level() -> None:
    x = tone(1000, 8000, 1.0)
    out = stream(Resampler(), x, 160)
    body = out[1600:-1600]
    assert peak_hz(body, 16_000) == pytest.approx(1000, abs=2)
    assert rms_dbfs(body) == pytest.approx(rms_dbfs(x[800:-800]), abs=0.5)


def test_chunk_size_does_not_change_the_output_beyond_rounding() -> None:
    x = tone(440, 8000, 0.5)
    whole = stream(Resampler(), x, x.size)
    framed = stream(Resampler(), x, 160)
    odd = stream(Resampler(), x, 37)
    assert whole.size == framed.size == odd.size == 8000
    assert int(np.max(np.abs(whole.astype(np.int32) - framed))) <= 2
    assert int(np.max(np.abs(whole.astype(np.int32) - odd))) <= 2


def test_downsampling_16k_to_8k_streams_too() -> None:
    out = stream(Resampler(16_000, 8000), tone(500, 16_000, 1.0), 320)
    assert out.size == 8000
    assert peak_hz(out[800:-800], 8000) == pytest.approx(500, abs=2)


@pytest.mark.parametrize("method", ["process", "flush"])
def test_flushed_resampler_refuses_more_work(method: str) -> None:
    resampler = Resampler()
    resampler.flush()
    with pytest.raises(RuntimeError, match="already flushed"):
        if method == "process":
            resampler.process(np.zeros(160, dtype=np.int16))
        else:
            resampler.flush()


@pytest.mark.parametrize(
    ("n", "in_rate", "out_rate", "expected"),
    [
        (8000, 8000, 16000, 16000),
        (16000, 16000, 8000, 8000),
        (161, 8000, 16000, 322),
        (333, 16000, 8000, 166),
    ],
)
def test_one_shot_resample_length(n: int, in_rate: int, out_rate: int, expected: int) -> None:
    out = resample(tone(300, in_rate, n / in_rate), in_rate, out_rate)
    assert out.dtype == np.int16
    assert abs(out.size - expected) <= 1
