"""Live smoke gate (specs/30 §7 Decision P03-T16): ``LIVE_TESTS=1`` enables the suite, each vendor key
enables its tests. Keys come from the process environment only and are never shown in a repr."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import pytest

__all__ = [
    "ELEVENLABS_KEY",
    "FROM_NUMBER",
    "LIVE_FLAG",
    "LLM_KEY",
    "TELNYX_KEY",
    "LiveEnv",
    "live_enabled",
    "missing_key_reason",
    "require",
]

LIVE_FLAG: Final = "LIVE_TESTS"
TELNYX_KEY: Final = "LIVE_TELNYX_API_KEY"
ELEVENLABS_KEY: Final = "LIVE_ELEVENLABS_API_KEY"
LLM_KEY: Final = "LIVE_LLM_API_KEY"
FROM_NUMBER: Final = "LIVE_FROM_NUMBER"


def live_enabled(environ: Mapping[str, str]) -> bool:
    """True only for ``LIVE_TESTS=1`` exactly."""
    return environ.get(LIVE_FLAG) == "1"


def missing_key_reason(name: str) -> str:
    return f"{name} is not set"


def require(value: str, name: str) -> str:
    """``value``, or skip the test naming the empty variable."""
    if not value:
        pytest.skip(missing_key_reason(name))
    return value


@dataclass(frozen=True, slots=True)
class LiveEnv:
    telnyx_api_key: str = field(repr=False)
    elevenlabs_api_key: str = field(repr=False)
    llm_api_key: str = field(repr=False)
    from_number: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> LiveEnv:
        return cls(
            telnyx_api_key=environ.get(TELNYX_KEY, "").strip(),
            elevenlabs_api_key=environ.get(ELEVENLABS_KEY, "").strip(),
            llm_api_key=environ.get(LLM_KEY, "").strip(),
            from_number=environ.get(FROM_NUMBER, "").strip(),
        )
