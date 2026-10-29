"""`VoiceSettings` defaults and bounds (specs/15-voices.md §5)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import va_domain
from va_domain.voices import VoiceSettings


def test_defaults_follow_the_spec() -> None:
    assert VoiceSettings().model_dump() == {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
        "speed": 1.0,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"stability": 0, "similarity_boost": 1, "style": 1, "speed": 0.7},
        {"speed": 1.2},
        {"use_speaker_boost": False},
    ],
)
def test_bounds_are_inclusive(payload: dict[str, Any]) -> None:
    assert VoiceSettings.model_validate(payload).model_dump(exclude_unset=True) == payload


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"stability": -0.1}, "stability"),
        ({"stability": 1.1}, "stability"),
        ({"similarity_boost": 1.5}, "similarity_boost"),
        ({"style": -1}, "style"),
        ({"speed": 0.69}, "speed"),
        ({"speed": 1.21}, "speed"),
        ({"pitch": 0.5}, "pitch"),
    ],
)
def test_out_of_range_or_unknown_values_are_rejected(payload: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        VoiceSettings.model_validate(payload)
    assert [error["loc"] for error in caught.value.errors()] == [(field,)]


def test_package_version_is_exposed() -> None:
    assert va_domain.__version__ == "0.1.0"
