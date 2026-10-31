"""Voice clone sample checks: WAV/MP3/FLAC durations, unmeasured containers, unreadable data, size cap."""

from __future__ import annotations

import pytest
from voice_helpers import id3_tag, mp3_sample, wav_sample

from va_sim.elevenlabs.voice_samples import MAX_SAMPLE_BYTES, SampleError, SampleInfo, measure

# MPEG-2 Layer III, no CRC | 64 kbps, 22.05 kHz, no padding | mono: 208-byte frames of 576 samples.
MPEG2_HEADER = b"\xff\xf3\x80\xc0"
MPEG2_FRAME = MPEG2_HEADER + bytes(208 - 4)


def flac(sample_rate: int, total_samples: int, *, block_type: int = 0x80, size: int = 42) -> bytes:
    packed = (sample_rate << 44) | (0 << 41) | (15 << 36) | total_samples
    streaminfo = bytes(10) + packed.to_bytes(8, "big") + bytes(16)
    return (b"fLaC" + bytes((block_type, 0, 0, 34)) + streaminfo)[:size]


def rejection(file_name: str, data: bytes) -> str:
    with pytest.raises(SampleError) as caught:
        measure(file_name, data)
    return caught.value.message


def test_wav_of_one_second_is_measured() -> None:
    assert measure("a.wav", wav_sample(1.0)) == SampleInfo("audio/wav", 1.0)


def test_wav_under_one_second_is_too_short() -> None:
    assert rejection("short.wav", wav_sample(0.99)) == "Sample 'short.wav' is shorter than 1 second"


def test_broken_riff_is_unreadable() -> None:
    data = b"RIFF" + bytes(4) + b"WAVE" + b"junkjunk"
    assert rejection("b.wav", data) == "Sample 'b.wav' could not be read as audio"


def test_wav_with_zero_sample_rate_is_unreadable() -> None:
    data = bytearray(wav_sample(1.0, rate=8000))
    data[24:28] = bytes(4)
    assert rejection("z.wav", bytes(data)) == "Sample 'z.wav' could not be read as audio"


def test_mpeg1_frames_are_summed() -> None:
    info = measure("a.mp3", mp3_sample(1.0))
    assert info.mime_type == "audio/mpeg"
    assert info.duration_s == pytest.approx(39 * 1152 / 44_100)


@pytest.mark.parametrize("footer", [False, True])
def test_an_id3_tag_is_skipped(footer: bool) -> None:
    info = measure("tagged.mp3", mp3_sample(2.0, tag=id3_tag(300, footer=footer)))
    assert info.duration_s == pytest.approx(77 * 1152 / 44_100)


def test_short_mp3_is_too_short() -> None:
    assert rejection("s.mp3", mp3_sample(0.5)) == "Sample 's.mp3' is shorter than 1 second"


def test_mpeg2_frames_and_a_truncated_tail() -> None:
    data = MPEG2_FRAME * 40 + MPEG2_HEADER + bytes(10)
    assert measure("m2.mp3", data).duration_s == pytest.approx(40 * 576 / 22_050)


def test_walk_stops_at_the_first_non_header() -> None:
    data = mp3_sample(1.5) + b"TAG" + bytes(125) + mp3_sample(5.0)
    assert measure("x.mp3", data).duration_s == pytest.approx(58 * 1152 / 44_100)


@pytest.mark.parametrize(
    "data",
    [
        id3_tag(20),
        b"ID3\x04",
        b"\xff\xfb\x00\xc0" + bytes(400),  # free-format bitrate
        b"\xff\xfb\xf0\xc0" + bytes(400),  # bad bitrate index
        b"\xff\xfb\x9c\xc0" + bytes(400),  # reserved sample rate
        b"\xff\xeb\x90\xc0" + bytes(400),  # reserved version
        b"\xff\xfd\x90\xc0" + bytes(400),  # layer II
    ],
)
def test_broken_mp3_is_unreadable(data: bytes) -> None:
    assert rejection("bad.mp3", data) == "Sample 'bad.mp3' could not be read as audio"


def test_flac_streaminfo_gives_the_duration() -> None:
    assert measure("a.flac", flac(16_000, 32_000)) == SampleInfo("audio/flac", 2.0)


def test_flac_without_a_total_is_unmeasured() -> None:
    assert measure("a.flac", flac(16_000, 0)) == SampleInfo("audio/flac", None)


@pytest.mark.parametrize(
    "data",
    [flac(0, 100), flac(16_000, 100, size=30), flac(16_000, 100, block_type=0x04)],
)
def test_broken_flac_is_unreadable(data: bytes) -> None:
    assert rejection("bad.flac", data) == "Sample 'bad.flac' could not be read as audio"


@pytest.mark.parametrize(
    ("data", "mime_type"),
    [
        (b"OggS" + bytes(60), "audio/ogg"),
        (bytes(4) + b"ftypM4A " + bytes(60), "audio/mp4"),
        (b"\x1a\x45\xdf\xa3" + bytes(60), "audio/webm"),
    ],
)
def test_other_containers_are_accepted_unmeasured(data: bytes, mime_type: str) -> None:
    assert measure("clip", data) == SampleInfo(mime_type, None)


def test_garbage_is_unreadable() -> None:
    assert (
        rejection("notes.txt", b"hello, this is not audio") == "Sample 'notes.txt' could not be read as audio"
    )


def test_samples_over_25_mb_are_rejected_before_parsing() -> None:
    data = wav_sample(1.0) + bytes(MAX_SAMPLE_BYTES)
    assert rejection("big.wav", data) == "Sample 'big.wav' is larger than 25 MB"
    assert measure("edge.ogg", b"OggS" + bytes(MAX_SAMPLE_BYTES - 4)).duration_s is None
