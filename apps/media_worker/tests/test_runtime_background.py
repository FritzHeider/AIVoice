"""The call's background bed in the outbound mix (specs/07-media-pipeline.md §10.2, Decision P05-T15)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from media_helpers import call_context
from runtime_rig import ASSETS_DIR
from service_helpers import json_log_stream, log_events

from va_media.runtime import BACKGROUND_ASSETS_DIR, background_mixer
from va_voice.audio import L16_16000, ULAW_8000, AudioFormat, l16_to_pcm, rms_dbfs, ulaw_decode
from va_voice.background import BackgroundLibrary
from va_voice.pipeline import PipelineConfig

FRAMES = 50


def _context(**config: Any) -> Any:
    return call_context(uuid.uuid4(), **config)


def _level(frames: list[bytes], fmt: AudioFormat) -> float:
    joined = b"".join(frames)
    pcm = ulaw_decode(joined) if fmt == ULAW_8000 else l16_to_pcm(joined, "little")
    return rms_dbfs(pcm)


def test_the_default_assets_dir_is_relative_to_the_working_directory() -> None:
    assert Path("assets") / "audio" == BACKGROUND_ASSETS_DIR
    assert not BACKGROUND_ASSETS_DIR.is_absolute()


@pytest.mark.parametrize(
    ("track", "level"), [(None, -40.0), ("office", -22.0), ("cafe", -22.0), ("restaurant", -22.0)]
)
async def test_a_track_plays_at_its_level(track: str | None, level: float) -> None:
    config = PipelineConfig()
    mixer = await background_mixer(BackgroundLibrary(ASSETS_DIR), _context(background_track=track), config)
    assert mixer.has_background
    assert mixer.format == ULAW_8000
    assert _level([mixer.idle() for _ in range(FRAMES)], ULAW_8000) == pytest.approx(level, abs=3.0)


async def test_none_means_no_bed() -> None:
    mixer = await background_mixer(
        BackgroundLibrary(ASSETS_DIR), _context(background_track="none"), PipelineConfig()
    )
    assert not mixer.has_background


async def test_16k_legs_read_the_pcm16k_asset() -> None:
    config = PipelineConfig(fmt=L16_16000, l16_byte_order="little")
    mixer = await background_mixer(BackgroundLibrary(ASSETS_DIR), _context(background_track="office"), config)
    frames = [mixer.idle() for _ in range(FRAMES)]
    assert {len(frame) for frame in frames} == {L16_16000.frame_bytes}
    assert _level(frames, L16_16000) == pytest.approx(-22.0, abs=3.0)


async def test_a_missing_asset_is_logged_and_the_call_goes_on_without_a_bed(tmp_path: Path) -> None:
    stream = json_log_stream()
    mixer = await background_mixer(
        BackgroundLibrary(tmp_path / "absent"), _context(background_track="office"), PipelineConfig()
    )
    assert not mixer.has_background
    assert mixer.speech(bytes(ULAW_8000.frame_bytes)) == bytes(ULAW_8000.frame_bytes)
    (event,) = [e for e in log_events(stream) if e["event"] == "media.background_unavailable"]
    assert (event["track"], event["detail"], event["level"]) == ("office", "BackgroundAssetError", "warning")
