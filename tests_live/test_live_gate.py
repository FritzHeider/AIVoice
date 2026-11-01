"""Offline checks of the live smoke suite (specs/30 §7 Decision P03-T16): the gate, the WER helper, the
cached STT fixture, and that the live tests are collected and skipped without ``LIVE_TESTS=1`` or keys."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import orjson
import pytest
from live_env import (
    ELEVENLABS_KEY,
    FROM_NUMBER,
    LLM_KEY,
    TELNYX_KEY,
    LiveEnv,
    live_enabled,
    missing_key_reason,
    require,
)
from live_fixture import (
    META_NAME,
    SAMPLE_RATE,
    STT_PHRASE,
    WAV_NAME,
    ensure_stt_fixture,
    load_fixture,
    save_fixture,
    wav_bytes,
)
from live_wer import normalise, wer

from va_sim.audio.codec import ulaw_decode

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
LIVE_MODULE: Final = "tests_live/test_vendor_clients.py"
LIVE_TEST_COUNT: Final = 7
ALL_KEYS: Final = (TELNYX_KEY, ELEVENLABS_KEY, LLM_KEY)


@pytest.mark.parametrize(("value", "enabled"), [("1", True), ("0", False), ("", False), ("true", False)])
def test_live_enabled_only_for_one(value: str, *, enabled: bool) -> None:
    assert live_enabled({"LIVE_TESTS": value}) is enabled


def test_live_disabled_when_unset() -> None:
    assert live_enabled({}) is False


def test_live_env_reads_the_variables_and_hides_keys() -> None:
    env = LiveEnv.from_environ(
        {TELNYX_KEY: " tk ", ELEVENLABS_KEY: "ek", LLM_KEY: "lk", FROM_NUMBER: "+15550100"}
    )
    assert (env.telnyx_api_key, env.elevenlabs_api_key, env.llm_api_key) == ("tk", "ek", "lk")
    assert env.from_number == "+15550100"
    assert "tk" not in repr(env)
    assert "ek" not in repr(env)
    assert "lk" not in repr(env)
    assert LiveEnv.from_environ({}) == LiveEnv("", "", "", "")


def test_require_skips_naming_the_variable() -> None:
    assert require("value", TELNYX_KEY) == "value"
    with pytest.raises(pytest.skip.Exception, match=TELNYX_KEY):
        require("", TELNYX_KEY)
    assert missing_key_reason(LLM_KEY) == f"{LLM_KEY} is not set"


def test_normalise_drops_case_and_punctuation() -> None:
    assert normalise("Hello, World! Don't-stop") == ["hello", "world", "dont", "stop"]
    assert normalise("  \n ") == []


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected"),
    [
        ("one two three", "One, two. Three!", 0.0),
        ("a b c d e f g h i j", "a b c d e f g h i x", 0.1),
        ("a b c d", "a b c", 0.25),
        ("a b c d", "a b c d e f", 0.5),
        ("a b", "c d e f", 2.0),
        ("", "", 0.0),
        ("", "words", 1.0),
    ],
)
def test_wer(reference: str, hypothesis: str, expected: float) -> None:
    assert wer(reference, hypothesis) == pytest.approx(expected)


def _tone(samples: int = 800) -> bytes:
    wave_form = (np.sin(np.arange(samples) / 4.0) * 8000).astype("<i2")
    return wave_form.tobytes()


def test_wav_round_trip_is_pcm16_mono_8k() -> None:
    pcm = _tone()
    with wave.open(io.BytesIO(wav_bytes(pcm)), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, SAMPLE_RATE)
        assert source.readframes(source.getnframes()) == pcm


def test_saved_fixture_loads_and_records_its_hash(tmp_path: Path) -> None:
    saved = save_fixture(tmp_path, text="hi there", voice_id="v1", model_id="m1", pcm16=_tone() + b"\x01")
    meta = orjson.loads((tmp_path / META_NAME).read_bytes())
    wav = (tmp_path / WAV_NAME).read_bytes()
    assert meta == {
        "text": "hi there",
        "voice_id": "v1",
        "model_id": "m1",
        "sha256": hashlib.sha256(wav).hexdigest(),
    }
    loaded = load_fixture(tmp_path)
    assert loaded == saved
    assert loaded.pcm16 == _tone()
    assert loaded.wav == wav
    decoded = ulaw_decode(loaded.ulaw)
    assert len(decoded) == len(_tone()) // 2
    assert np.max(np.abs(decoded.astype(np.int32) - np.frombuffer(_tone(), "<i2"))) < 300


def test_missing_fixture_is_none(tmp_path: Path) -> None:
    assert load_fixture(tmp_path) is None


def test_tampered_fixture_fails(tmp_path: Path) -> None:
    save_fixture(tmp_path, text="hi", voice_id="v", model_id="m", pcm16=_tone())
    wav_path = tmp_path / WAV_NAME
    wav_path.write_bytes(wav_path.read_bytes()[:-2] + b"\x00\x01")
    with pytest.raises(AssertionError, match="sha256 mismatch"):
        load_fixture(tmp_path)


def test_half_fixture_fails(tmp_path: Path) -> None:
    save_fixture(tmp_path, text="hi", voice_id="v", model_id="m", pcm16=_tone())
    (tmp_path / META_NAME).unlink()
    with pytest.raises(AssertionError, match="incomplete"):
        load_fixture(tmp_path)


def test_fixture_with_wrong_format_fails(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(_tone())
    wav = buffer.getvalue()
    (tmp_path / WAV_NAME).write_bytes(wav)
    meta = {"text": "x", "voice_id": "v", "model_id": "m", "sha256": hashlib.sha256(wav).hexdigest()}
    (tmp_path / META_NAME).write_bytes(orjson.dumps(meta))
    with pytest.raises(AssertionError, match="PCM16 mono 8000 Hz"):
        load_fixture(tmp_path)


@dataclass(frozen=True, slots=True)
class _Audio:
    audio: bytes


class _Synth:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def tts_convert(self, voice_id: str, text: str, *, model_id: str, output_format: str) -> _Audio:
        self.calls.append((voice_id, text, model_id, output_format))
        return _Audio(_tone())


async def test_ensure_synthesizes_once_then_reuses(tmp_path: Path) -> None:
    synth = _Synth()
    first = await ensure_stt_fixture(synth, voice_id="v9", model_id="m9", directory=tmp_path)
    second = await ensure_stt_fixture(synth, voice_id="v9", model_id="m9", directory=tmp_path)
    assert synth.calls == [("v9", STT_PHRASE, "m9", "pcm_8000")]
    assert first == second
    assert first.text == STT_PHRASE


def _run_live_module(**env: str) -> subprocess.CompletedProcess[str]:
    child_env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_", "COV_CORE_", "LIVE_")) and name != "COVERAGE_PROCESS_START"
    }
    child_env.update(env)
    return subprocess.run(  # noqa: S603 - fixed arguments, the current interpreter
        [sys.executable, "-m", "pytest", LIVE_MODULE, "-m", "live", "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_live_suite_is_collected_and_skipped_without_the_flag() -> None:
    result = _run_live_module(LIVE_TESTS="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{LIVE_TEST_COUNT} skipped" in result.stdout
    assert "LIVE_TESTS=1 is not set" in result.stdout


def test_live_suite_skips_each_test_without_its_key() -> None:
    result = _run_live_module(LIVE_TESTS="1", **dict.fromkeys(ALL_KEYS, ""))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{LIVE_TEST_COUNT} skipped" in result.stdout
    for name in ALL_KEYS:
        assert missing_key_reason(name) in result.stdout
