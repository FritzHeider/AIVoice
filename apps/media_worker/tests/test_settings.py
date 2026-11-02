"""Media settings (specs/02-repo-layout-and-conventions.md §6)."""

from __future__ import annotations

import secrets
import socket
from pathlib import Path

import pytest
from media_helpers import FakeMediaContainer, MediaKeys
from pydantic import ValidationError
from service_helpers import make_client, settings_of

from va_media.auth import MediaAuth
from va_media.main import create_app, media_auth
from va_media.settings import MediaSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(MediaSettings)
    assert settings.service_name == "media-worker"
    assert settings.media_drain_timeout_s == 2700
    assert settings.media_max_calls_per_pod == 60
    assert settings.media_vad_threads == 2
    assert settings.redis_url == "redis://localhost:6379/0"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_pod_id_defaults_to_hostname(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is not None:
        monkeypatch.setenv("POD_ID", value)
    assert settings_of(MediaSettings).pod_id == socket.gethostname()


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_ID", "media-7f9c")
    monkeypatch.setenv("MEDIA_MAX_CALLS_PER_POD", "2")
    monkeypatch.setenv("MEDIA_DRAIN_TIMEOUT_S", "30")
    monkeypatch.setenv("MEDIA_VAD_THREADS", "4")
    settings = settings_of(MediaSettings)
    assert (
        settings.pod_id,
        settings.media_max_calls_per_pod,
        settings.media_drain_timeout_s,
        settings.media_vad_threads,
    ) == ("media-7f9c", 2, 30, 4)


def test_vendor_and_llm_defaults_match_env_example() -> None:
    settings = settings_of(MediaSettings)
    assert (settings.elevenlabs_ws_base, settings.elevenlabs_api_key.get_secret_value()) == (
        "ws://localhost:9200",
        "sim-eleven-key",
    )
    assert (settings.elevenlabs_tts_model, settings.elevenlabs_stt_model) == (
        "eleven_flash_v2_5",
        "scribe_v2_realtime",
    )
    assert settings.elevenlabs_zero_retention_available is False
    assert (settings.elevenlabs_stt_session_rotate_s, settings.elevenlabs_stt_keyterms_encoding) == (
        1500,
        "repeated",
    )
    assert settings.llm_default_base_url == "http://localhost:9300/v1"
    assert settings.telnyx_l16_byte_order == "big"
    assert settings.local_master_key.get_secret_value() == ""


def test_vendor_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_WS_BASE", "wss://api.elevenlabs.io")
    monkeypatch.setenv("ELEVENLABS_ZERO_RETENTION_AVAILABLE", "true")
    monkeypatch.setenv("ELEVENLABS_STT_KEYTERMS_ENCODING", "json")
    monkeypatch.setenv("TELNYX_L16_BYTE_ORDER", "little")
    settings = settings_of(MediaSettings)
    assert (
        settings.elevenlabs_ws_base,
        settings.elevenlabs_zero_retention_available,
        settings.elevenlabs_stt_keyterms_encoding,
        settings.telnyx_l16_byte_order,
    ) == ("wss://api.elevenlabs.io", True, "json", "little")


@pytest.mark.parametrize(
    "overrides",
    [
        {"telnyx_l16_byte_order": "middle"},
        {"elevenlabs_stt_keyterms_encoding": "csv"},
        {"elevenlabs_stt_session_rotate_s": 0},
    ],
)
def test_vendor_settings_are_validated(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        settings_of(MediaSettings, **overrides)


def test_max_calls_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        settings_of(MediaSettings, media_max_calls_per_pod=0)


def test_vad_threads_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        settings_of(MediaSettings, media_vad_threads=0)


def test_media_public_host_and_scheme() -> None:
    assert settings_of(MediaSettings).media_public_host == "localhost:8200"
    assert settings_of(MediaSettings).media_ws_scheme == "ws"
    assert settings_of(MediaSettings, va_env="test").media_ws_scheme == "ws"
    assert settings_of(MediaSettings, va_env="staging").media_ws_scheme == "wss"


def test_stream_secret_validation_hides_the_value() -> None:
    value = "not-hex-" + secrets.token_hex(8)
    with pytest.raises(ValidationError) as caught:
        settings_of(MediaSettings, stream_token_secret=value)
    assert value not in str(caught.value)


def test_media_auth_requires_key_and_secret() -> None:
    keys = MediaKeys()
    with pytest.raises(RuntimeError, match="TELNYX_PUBLIC_KEY"):
        media_auth(settings_of(MediaSettings, stream_token_secret=keys.secret.hex()))
    with pytest.raises(RuntimeError, match="STREAM_TOKEN_SECRET"):
        media_auth(settings_of(MediaSettings, telnyx_public_key=keys.public_b64))
    with pytest.raises(ValueError, match="base64"):
        media_auth(settings_of(MediaSettings, telnyx_public_key="%%%", stream_token_secret=keys.secret.hex()))


def test_media_auth_reads_the_sim_key_file_in_local(tmp_path: Path) -> None:
    keys = MediaKeys()
    key_file = tmp_path / ".sim" / "telnyx_public_key"  # the isolated env chdirs to tmp_path
    key_file.parent.mkdir()
    key_file.write_text(keys.public_b64 + "\n", encoding="utf-8")
    assert isinstance(
        media_auth(settings_of(MediaSettings, stream_token_secret=keys.secret.hex())), MediaAuth
    )
    with pytest.raises(RuntimeError, match="TELNYX_PUBLIC_KEY"):
        media_auth(settings_of(MediaSettings, va_env="test", stream_token_secret=keys.secret.hex()))


async def test_lifespan_refuses_to_start_before_opening_anything(models_dir: Path) -> None:
    container = FakeMediaContainer()
    app = create_app(
        settings_of(MediaSettings),
        container_factory=container,
        init_observability=False,
        models_dir=models_dir,
    )
    with pytest.raises(RuntimeError, match="TELNYX_PUBLIC_KEY"):
        async with make_client(app):
            pass
    assert container.opened == 0
