"""Inbound reorder buffer (specs/07-media-pipeline.md §5.1)."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_common.metrics import Service, build_metrics, new_registry
from va_voice.audio import L16_16000, ULAW_8000, AudioFormat
from va_voice.reorder import NULL_OBSERVER, OrderedFrame, ReorderBuffer
from va_voice.reorder_metrics import CatalogReorderMetrics


def payload(chunk: int) -> bytes:
    """A 160-byte frame whose content identifies its chunk."""
    return chunk.to_bytes(4, "big") * 40


def feed(buffer: ReorderBuffer, chunks: Iterable[int]) -> list[OrderedFrame]:
    out: list[OrderedFrame] = []
    for chunk in chunks:
        out.extend(buffer.push(chunk, payload(chunk)))
    return out


def numbers(frames: Iterable[OrderedFrame]) -> list[int]:
    return [frame.chunk for frame in frames]


def fills(frames: Iterable[OrderedFrame]) -> list[int]:
    return [frame.chunk for frame in frames if frame.filled]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def reordered(self, count: int) -> None:
        self.calls.append(("reordered", count))

    def gap_filled(self, count: int) -> None:
        self.calls.append(("gap_filled", count))

    def duplicate(self, count: int) -> None:
        self.calls.append(("duplicate", count))


def test_in_order_frames_pass_straight_through() -> None:
    buffer = ReorderBuffer()
    for chunk in range(1, 11):
        assert buffer.push(chunk, payload(chunk)) == [OrderedFrame(chunk, payload(chunk), filled=False)]
        assert buffer.held == 0
    assert buffer.expected == 11
    stats = buffer.stats
    assert (stats.received, stats.emitted, stats.reordered, stats.gap_frames, stats.duplicates) == (
        10,
        10,
        0,
        0,
        0,
    )


def test_pairwise_jitter_is_restored_without_gaps() -> None:
    buffer = ReorderBuffer()
    arrivals = [c for pair in range(1, 21, 2) for c in (pair + 1, pair)]
    out = feed(buffer, arrivals)
    assert numbers(out) == list(range(1, 21))
    assert [frame.payload for frame in out] == [payload(c) for c in range(1, 21)]
    assert fills(out) == []
    assert buffer.stats.reordered == 10
    assert buffer.held == 0


def test_first_chunk_out_of_order_waits_for_chunk_one() -> None:
    buffer = ReorderBuffer()
    assert buffer.push(2, payload(2)) == []
    assert buffer.held == 1
    assert numbers(buffer.push(1, payload(1))) == [1, 2]


def test_missing_chunk_is_filled_once_window_later_chunks_are_held() -> None:
    recorder = Recorder()
    buffer = ReorderBuffer(observer=recorder)
    assert numbers(feed(buffer, [1])) == [1]
    assert feed(buffer, [3, 4, 5, 6]) == []
    assert buffer.held == 4
    out = buffer.push(7, payload(7))
    assert out[0] == OrderedFrame(2, b"\xff" * 160, filled=True)
    assert numbers(out) == [2, 3, 4, 5, 6, 7]
    assert buffer.stats.gap_frames == 1
    assert ("gap_filled", 1) in recorder.calls

    assert buffer.push(2, payload(2)) == []
    assert buffer.stats.duplicates == 1
    assert recorder.calls[-1] == ("duplicate", 1)


def test_two_missing_chunks_each_get_one_silence_frame() -> None:
    buffer = ReorderBuffer()
    feed(buffer, [1])
    assert feed(buffer, [4, 5, 6, 7]) == []
    out = buffer.push(8, payload(8))
    assert numbers(out) == [2, 3, 4, 5, 6, 7, 8]
    assert fills(out) == [2, 3]
    assert buffer.stats.gap_frames == 2


def test_second_hole_waits_for_its_own_window() -> None:
    buffer = ReorderBuffer()
    feed(buffer, [1])
    out = feed(buffer, [3, 5, 6, 7, 8])
    assert numbers(out) == [2, 3]
    assert buffer.expected == 4
    assert buffer.held == 4
    assert numbers(buffer.push(4, payload(4))) == [4, 5, 6, 7, 8]
    assert buffer.stats.gap_frames == 1


def test_duplicates_are_dropped_whether_held_or_already_emitted() -> None:
    recorder = Recorder()
    buffer = ReorderBuffer(observer=recorder)
    feed(buffer, [1, 3])
    assert buffer.push(3, payload(3)) == []
    assert buffer.push(1, payload(1)) == []
    assert buffer.push(0, payload(0)) == []
    assert numbers(buffer.push(2, payload(2))) == [2, 3]
    assert buffer.stats.duplicates == 3
    assert buffer.stats.received == 6
    assert recorder.calls.count(("duplicate", 1)) == 3
    assert recorder.calls.count(("reordered", 1)) == 1


def test_custom_first_chunk_drops_earlier_numbers() -> None:
    buffer = ReorderBuffer(first_chunk=100)
    assert buffer.expected == 100
    assert buffer.push(99, payload(99)) == []
    assert numbers(feed(buffer, [101, 100])) == [100, 101]
    assert buffer.stats.duplicates == 1


@pytest.mark.parametrize(("fmt", "silence"), [(ULAW_8000, b"\xff" * 160), (L16_16000, b"\x00" * 640)])
def test_silence_fill_matches_the_audio_format(fmt: AudioFormat, silence: bytes) -> None:
    buffer = ReorderBuffer(fmt, window=1)
    out = buffer.push(2, payload(2))
    assert out[0] == OrderedFrame(1, silence, filled=True)
    assert numbers(out) == [1, 2]


def test_wide_jump_resyncs_without_a_silence_flood() -> None:
    buffer = ReorderBuffer()
    feed(buffer, [1])
    out = feed(buffer, range(500, 505))
    assert numbers(out) == list(range(500, 505))
    assert fills(out) == []
    assert buffer.stats.resyncs == 1
    assert buffer.stats.gap_frames == 0


def test_gap_of_exactly_max_gap_is_still_filled() -> None:
    buffer = ReorderBuffer(window=1, max_gap=3)
    out = buffer.push(4, payload(4))
    assert fills(out) == [1, 2, 3]
    assert buffer.stats.resyncs == 0
    out = buffer.push(9, payload(9))
    assert numbers(out) == [9]
    assert buffer.stats.resyncs == 1


def test_drain_emits_held_frames_and_fills_interior_gaps() -> None:
    buffer = ReorderBuffer()
    feed(buffer, [1, 3, 5])
    out = buffer.drain()
    assert numbers(out) == [2, 3, 4, 5]
    assert fills(out) == [2, 4]
    assert buffer.held == 0
    assert buffer.expected == 6
    assert numbers(buffer.push(6, payload(6))) == [6]
    assert buffer.drain() == []


def test_drain_on_an_empty_buffer_returns_nothing() -> None:
    buffer = ReorderBuffer()
    assert buffer.drain() == []
    assert buffer.expected == 1


@pytest.mark.parametrize(("window", "max_gap"), [(0, 50), (5, 4)])
def test_rejects_invalid_configuration(window: int, max_gap: int) -> None:
    with pytest.raises(ValueError, match="must be at least"):
        ReorderBuffer(window=window, max_gap=max_gap)


def test_null_observer_ignores_everything() -> None:
    NULL_OBSERVER.reordered(1)
    NULL_OBSERVER.gap_filled(1)
    NULL_OBSERVER.duplicate(1)
    buffer = ReorderBuffer(window=1)
    assert fills(buffer.push(3, payload(3))) == [1, 2]


def test_catalog_metrics_mirror_the_stats() -> None:
    registry = new_registry(process_collectors=False)
    buffer = ReorderBuffer(observer=CatalogReorderMetrics(build_metrics(Service.MEDIA_WORKER, registry)))
    feed(buffer, [2, 1, 4, 3, 5, 7, 8, 9, 10, 11, 7])
    stats = buffer.stats
    assert (stats.reordered, stats.gap_frames, stats.duplicates) == (7, 1, 1)
    assert registry.get_sample_value("va_audio_reorder_total") == stats.reordered
    assert registry.get_sample_value("va_audio_gap_frames_total") == stats.gap_frames


@st.composite
def jittered(draw: st.DrawFn, *, window: int = 5) -> list[int]:
    """Chunks 1..n where every frame is displaced by fewer than ``window`` positions."""
    n = draw(st.integers(min_value=1, max_value=150))
    offsets = draw(st.lists(st.integers(min_value=0, max_value=window - 1), min_size=n, max_size=n))
    return [
        chunk
        for _, chunk in sorted(
            (chunk + offset, chunk) for chunk, offset in zip(range(1, n + 1), offsets, strict=True)
        )
    ]


@given(jittered())
def test_bounded_jitter_never_fills_and_preserves_payloads(arrivals: list[int]) -> None:
    buffer = ReorderBuffer()
    out = feed(buffer, arrivals)
    out += buffer.drain()
    assert numbers(out) == list(range(1, len(arrivals) + 1))
    assert [frame.payload for frame in out] == [payload(c) for c in range(1, len(arrivals) + 1)]
    assert fills(out) == []
    assert buffer.stats.gap_frames == 0


@given(jittered(), st.data())
def test_dropped_chunks_become_silence_exactly_where_they_are_missing(
    arrivals: list[int], data: st.DataObject
) -> None:
    dropped = data.draw(st.sets(st.sampled_from(arrivals)))
    received = [c for c in arrivals if c not in dropped]
    buffer = ReorderBuffer(max_gap=len(arrivals) + 5)
    out = feed(buffer, received)
    out += buffer.drain()
    last = max(received, default=0)
    assert numbers(out) == list(range(1, last + 1))
    assert set(fills(out)) == {c for c in dropped if c < last}
    assert all(len(frame.payload) == ULAW_8000.frame_bytes for frame in out)
    assert buffer.stats.duplicates == 0


@given(jittered(), st.data())
def test_injected_repeats_never_reach_the_output(arrivals: list[int], data: st.DataObject) -> None:
    stream = list(arrivals)
    repeats = data.draw(st.integers(min_value=0, max_value=20))
    for _ in range(repeats):
        source = data.draw(st.integers(min_value=0, max_value=len(stream) - 1))
        target = data.draw(st.integers(min_value=source + 1, max_value=len(stream)))
        stream.insert(target, stream[source])
    buffer = ReorderBuffer()
    out: list[OrderedFrame] = []
    for chunk in stream:
        step = buffer.push(chunk, payload(chunk))
        if out and step:
            assert step[0].chunk == out[-1].chunk + 1
        out += step
    out += buffer.drain()
    assert numbers(out) == list(range(1, len(arrivals) + 1))
    assert buffer.stats.duplicates == repeats
