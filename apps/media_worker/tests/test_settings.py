"""Media settings (specs/02-repo-layout-and-conventions.md §6)."""

from __future__ import annotations

import socket

import pytest
from pydantic import ValidationError
from service_helpers import settings_of

from va_media.settings import MediaSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(MediaSettings)
    assert settings.service_name == "media-worker"
    assert settings.media_drain_timeout_s == 2700
    assert settings.media_max_calls_per_pod == 60
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
    settings = settings_of(MediaSettings)
    assert (settings.pod_id, settings.media_max_calls_per_pod, settings.media_drain_timeout_s) == (
        "media-7f9c",
        2,
        30,
    )


def test_max_calls_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        settings_of(MediaSettings, media_max_calls_per_pod=0)
