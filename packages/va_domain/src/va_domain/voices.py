"""Voice settings sent to ElevenLabs on context initialization (specs/15-voices.md §5)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["VoiceSettings"]


class VoiceSettings(BaseModel):
    """ElevenLabs voice settings; every field has the model default, so a partial object is valid."""

    model_config = ConfigDict(extra="forbid")

    stability: float = Field(default=0.5, ge=0, le=1, description="Lower values give a more varied delivery.")
    similarity_boost: float = Field(
        default=0.75, ge=0, le=1, description="How closely the output follows the original voice."
    )
    style: float = Field(default=0.0, ge=0, le=1, description="Style exaggeration of the speaker.")
    use_speaker_boost: bool = Field(default=True, description="Boost similarity to the original speaker.")
    speed: float = Field(default=1.0, ge=0.7, le=1.2, description="Speaking rate multiplier.")
