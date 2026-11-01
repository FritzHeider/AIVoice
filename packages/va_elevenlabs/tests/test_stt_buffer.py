"""Audio batching and the replay buffer (Decision P03-T11 rules 3 and 7)."""

from __future__ import annotations

from va_elevenlabs.stt_buffer import AudioBatcher, ReplayBuffer, pieces


def test_batcher_waits_for_batch_ms() -> None:
    batcher = AudioBatcher("ulaw_8000", 100)
    for _ in range(4):
        batcher.add(b"\x01" * 160)
        assert not batcher.ready
    batcher.add(b"\x01" * 160)
    assert batcher.ready
    assert batcher.take() == b"\x01" * 800
    assert batcher.take() == b""


def test_batcher_resizes() -> None:
    batcher = AudioBatcher("pcm_16000", 100)
    assert batcher.batch_bytes == 3200
    batcher.resize(200)
    assert (batcher.batch_ms, batcher.batch_bytes) == (200, 6400)
    batcher.resize(50)
    batcher.add(bytes(1600))
    assert batcher.ready


def test_pieces() -> None:
    assert list(pieces(b"abcdefg", 3)) == [b"abc", b"def", b"g"]
    assert list(pieces(b"", 3)) == []


def test_replay_marks_ack_and_split() -> None:
    replay = ReplayBuffer("ulaw_8000", 2.0)
    first = replay.append(b"a" * 100)
    second = replay.append(b"b" * 50)
    assert (first, second, replay.end, len(replay)) == (100, 150, 150, 150)
    assert replay.split(first) == (b"a" * 100, b"b" * 50)
    assert replay.split(None) == (b"", b"a" * 100 + b"b" * 50)
    replay.ack(first)
    assert replay.split(None) == (b"", b"b" * 50)
    assert replay.split(first) == (b"", b"b" * 50)
    assert replay.split(10_000) == (b"b" * 50, b"")
    replay.ack(10_000)
    assert len(replay) == 0
    assert replay.end == 150


def test_replay_caps_at_max_seconds() -> None:
    replay = ReplayBuffer("ulaw_8000", 0.1)
    replay.append(b"a" * 600)
    mark = replay.append(b"b" * 600)
    assert len(replay) == 800
    assert replay.dropped_bytes == 400
    assert replay.dropped_ms == 50
    assert replay.split(mark) == (b"a" * 200 + b"b" * 600, b"")
    replay.ack(100)
    assert len(replay) == 800
