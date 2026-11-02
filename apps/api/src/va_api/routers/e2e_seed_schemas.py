"""Bodies of ``POST /internal/test/orgs``, the e2e harness seed endpoint (specs/30 §5 Decision P05-T17b).

The request describes a ready-to-call org: plan, starting credit, sparse org settings, org voices, and owned
numbers (each with an optional inbound configuration). The response carries the plaintext of the org's live
``*`` API key; the endpoint exists only under ``VA_ENV`` ``local``/``test``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

__all__ = [
    "MAX_SEED_NUMBERS",
    "MAX_SEED_VOICES",
    "SeedNumber",
    "SeedNumberCreated",
    "SeedOrgCreated",
    "SeedOrgRequest",
    "SeedVoice",
    "SeedVoiceCreated",
]

MAX_SEED_VOICES: Final = 10
MAX_SEED_NUMBERS: Final = 10

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

_REQUEST_EXAMPLE: Final[dict[str, JsonValue]] = {
    "name": "E2E org",
    "plan_id": "build",
    "dollars": "50",
    "settings": {"compliance": {"state_quiet_hours": False}},
    "voices": [{"name": "Karen", "provider_voice_id": "XrExE9yKIg1WjnnlVkGX"}],
    "numbers": [{"e164": "+16285550142", "capabilities": ["voice"], "inbound_config": {}}],
}
_CREATED_EXAMPLE: Final[dict[str, JsonValue]] = {
    "org_id": "0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e0f",
    "api_key": "va_live_3kQ9xT2mLpR7vB1nZc8dHf4sWq6yJa0e",
    "voices": [{"id": "0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e10", "name": "Karen"}],
    "numbers": [{"id": "0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e11", "e164": "+16285550142"}],
}


class SeedVoice(BaseModel):
    """An org voice row (provider ``elevenlabs``)."""

    model_config = ConfigDict(extra="forbid")

    name: _Name
    provider_voice_id: _Name


class SeedNumber(BaseModel):
    """An active number owned by the org on the platform Telnyx account."""

    model_config = ConfigDict(extra="forbid")

    e164: str = Field(min_length=2, max_length=20)
    telnyx_phone_number_id: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["voice"], min_length=1, max_length=3)
    inbound_config: dict[str, Any] = Field(default_factory=dict)


class SeedOrgRequest(BaseModel):
    """``POST /internal/test/orgs``."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_REQUEST_EXAMPLE]})

    name: _Name = "E2E org"
    plan_id: _Name = "build"
    dollars: Decimal = Field(default=Decimal(50), ge=0, max_digits=12, decimal_places=6)
    settings: dict[str, Any] = Field(default_factory=dict)
    voices: list[SeedVoice] = Field(min_length=1, max_length=MAX_SEED_VOICES)
    numbers: list[SeedNumber] = Field(default_factory=list, max_length=MAX_SEED_NUMBERS)


class SeedVoiceCreated(BaseModel):
    id: uuid.UUID
    name: str


class SeedNumberCreated(BaseModel):
    id: uuid.UUID
    e164: str


class SeedOrgCreated(BaseModel):
    """The seeded org, its live ``*`` key (plaintext), and the created voices and numbers."""

    model_config = ConfigDict(json_schema_extra={"examples": [_CREATED_EXAMPLE]})

    org_id: uuid.UUID
    api_key: str
    voices: list[SeedVoiceCreated]
    numbers: list[SeedNumberCreated]
