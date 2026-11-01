"""Background tracks and their loops (specs/07-media-pipeline.md §10.2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from va_voice.audio import FRAME_MS, PCM16LE_16000, ULAW_8000, AudioFormat, rms_dbfs, ulaw_decode
from va_voice.background import (
    STATIC_TRACK,
    BackgroundAssetError,
    BackgroundLibrary,
    BackgroundLoop,
    resolve_background,
)
from va_voice.outbound import FrameMixer

ASSETS = Path(__file__).resolve().parents[3] / "assets" / "audio"
FORMATS = (ULAW_8000, PCM16LE_16000)
LOOP_FRAMES = 60_000 // FRAME_MS
TARGET_DBFS = {None: -40.0, "phone_static": -40.0, "office": -22.0, "cafe": -22.0, "restaurant": -22.0}


@pytest.mark.parametrize(
    ("track", "asset"),
    [
        (None, STATIC_TRACK),
        ("phone_static", STATIC_TRACK),
        ("office", "office"),
        ("cafe", "cafe"),
        ("restaurant", "restaurant"),
        ("none", None),
    ],
)
def test_resolve_background(track: str | None, asset: str | None) -> None:
    assert resolve_background(track) == asset


@pytest.mark.parametrize("track", ["static", "jazz", "", "Office"])
def test_resolve_background_rejects_unknown_tracks(track: str) -> None:
    with pytest.raises(ValueError, match="unknown background_track"):
        resolve_background(track)


def test_loop_wraps_at_the_seam() -> None:
    samples = np.arange(7, dtype=np.int16)
    loop = BackgroundLoop(samples, 3)
    frames = [loop.next_frame() for _ in range(7)]
    assert np.array_equal(np.concatenate(frames), np.tile(samples, 3))
    assert loop.position == 0
    loop.next_frame()
    assert loop.position == 3
    loop.reset()
    assert loop.position == 0
    assert loop.frame_samples == 3
    assert loop.samples is samples


def test_loop_frames_longer_than_the_track_wrap_repeatedly() -> None:
    loop = BackgroundLoop(np.array([1, 2], dtype=np.int16), 5)
    assert loop.next_frame().tolist() == [1, 2, 1, 2, 1]
    assert loop.next_frame().tolist() == [2, 1, 2, 1, 2]


@pytest.mark.parametrize(
    ("samples", "frame"), [(np.zeros(0, dtype=np.int16), 160), (np.zeros(4, dtype=np.int16), 0)]
)
def test_loop_rejects_bad_arguments(samples: NDArray[np.int16], frame: int) -> None:
    with pytest.raises(ValueError, match=r"background loop needs samples|frame_samples"):
        BackgroundLoop(samples, frame)


@pytest.mark.parametrize("fmt", FORMATS, ids=lambda f: f.name)
async def test_library_shares_samples_but_not_positions(fmt: AudioFormat) -> None:
    library = BackgroundLibrary(ASSETS)
    first = await library.loop("office", fmt)
    second = await library.loop("office", fmt)
    assert first is not None
    assert second is not None
    assert first.samples is second.samples
    assert not first.samples.flags.writeable
    assert first.samples.size == 60 * fmt.sample_rate
    assert first.frame_samples == fmt.frame_samples
    first.next_frame()
    assert (first.position, second.position) == (fmt.frame_samples, 0)


async def test_library_returns_no_loop_for_none() -> None:
    assert await BackgroundLibrary(ASSETS).loop("none", ULAW_8000) is None


async def test_library_rejects_other_rates() -> None:
    with pytest.raises(ValueError, match="no background assets at 44100 Hz"):
        await BackgroundLibrary(ASSETS).samples("office", 44_100)


async def test_library_reports_bad_assets(tmp_path: Path) -> None:
    library = BackgroundLibrary(tmp_path)
    with pytest.raises(BackgroundAssetError, match="unreadable"):
        await library.loop("office", ULAW_8000)
    (tmp_path / "cafe.ulaw").write_bytes(b"")
    with pytest.raises(BackgroundAssetError, match="empty"):
        await library.loop("cafe", ULAW_8000)
    (tmp_path / "cafe.pcm16k").write_bytes(b"\x00\x01\x02")
    with pytest.raises(BackgroundAssetError, match="odd byte count"):
        await library.loop("cafe", PCM16LE_16000)


@pytest.mark.parametrize("fmt", FORMATS, ids=lambda f: f.name)
@pytest.mark.parametrize("track", list(TARGET_DBFS))
async def test_idle_frames_play_at_the_target_level(fmt: AudioFormat, track: str | None) -> None:
    loop = await BackgroundLibrary(ASSETS).loop(track, fmt)
    mixer = FrameMixer(fmt, background=loop, wire_byte_order="little")
    wire = b"".join(mixer.idle() for _ in range(LOOP_FRAMES))
    decoded = _decode(fmt, wire)
    assert decoded.size == 60 * fmt.sample_rate
    assert rms_dbfs(decoded) == pytest.approx(TARGET_DBFS[track], abs=1.0)


def _decode(fmt: AudioFormat, wire: bytes) -> NDArray[np.int16]:
    if fmt == ULAW_8000:
        return ulaw_decode(wire)
    return np.frombuffer(wire, dtype="<i2")
