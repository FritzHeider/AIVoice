"""Voice settings sent to ElevenLabs on context initialization (specs/15-voices.md §5)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Speed", "UnitInterval", "VoiceSettings", "VoiceSettingsPatch"]

UnitInterval = Annotated[float, Field(ge=0, le=1)]
Speed = Annotated[float, Field(ge=0.7, le=1.2)]


class VoiceSettings(BaseModel):
    """ElevenLabs voice settings; every field has the model default, so a partial object is valid."""

    model_config = ConfigDict(extra="forbid")

    stability: UnitInterval = Field(default=0.5, description="Lower values give a more varied delivery.")
    similarity_boost: UnitInterval = Field(
        default=0.75, description="How closely the output follows the original voice."
    )
    style: UnitInterval = Field(default=0.0, description="Style exaggeration of the speaker.")
    use_speaker_boost: bool = Field(default=True, description="Boost similarity to the original speaker.")
    speed: Speed = Field(default=1.0, description="Speaking rate multiplier.")


class VoiceSettingsPatch(BaseModel):
    """Per-call overrides of a voice's stored settings (specs/05 §3.2): only the sent keys apply."""

    model_config = ConfigDict(extra="forbid")

    stability: UnitInterval | None = None
    similarity_boost: UnitInterval | None = None
    style: UnitInterval | None = None
    use_speaker_boost: bool | None = None
    speed: Speed | None = None
