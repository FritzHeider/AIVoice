"""FakeElevenLabs voice and subscription bodies (Decision P03-T05c rules 2, 3, 8, 10).

Shared by the voices REST routes, the control routes, and TTS (``voice_not_found``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.responses import JSONResponse

from va_sim.elevenlabs.auth import detail_error

if TYPE_CHECKING:
    from va_sim.elevenlabs.store import ElevenLabsStore
    from va_sim.elevenlabs.voice_store import VoiceRecord

__all__ = [
    "PREMADE_BASE_MODELS",
    "preview_path",
    "subscription_json",
    "voice_json",
    "voice_not_editable",
    "voice_not_found",
    "voice_not_found_message",
]

PREMADE_BASE_MODELS: Final = ("eleven_flash_v2_5", "eleven_multilingual_v2")
_RESET_PERIOD_S: Final = 30 * 86_400


def voice_not_found_message(voice_id: str) -> str:
    return f"A voice with the voice_id {voice_id} was not found."


def voice_not_found(voice_id: str) -> JSONResponse:
    return detail_error(404, "voice_not_found", voice_not_found_message(voice_id))


def voice_not_editable() -> JSONResponse:
    return detail_error(400, "voice_not_editable", "Premade voices cannot be edited or deleted")


def preview_path(voice_id: str) -> str:
    """The preview path of a premade voice, relative to the simulator base URL."""
    return f"sim/elevenlabs/previews/{voice_id}.mp3"


def voice_json(record: VoiceRecord, base_url: str) -> dict[str, object]:
    """The ElevenLabs voice object; ``base_url`` ends with ``/``."""
    premade = record.category == "premade"
    return {
        "voice_id": record.voice_id,
        "name": record.name,
        "samples": None if record.samples is None else [sample.to_json() for sample in record.samples],
        "category": record.category,
        "labels": dict(record.labels),
        "description": record.description,
        "preview_url": f"{base_url}{preview_path(record.voice_id)}" if premade else None,
        "available_for_tiers": [],
        "settings": dict(record.settings),
        "sharing": None,
        "high_quality_base_model_ids": list(PREMADE_BASE_MODELS) if premade else [],
        "verified_languages": [language.to_json() for language in record.languages],
        "is_owner": not premade,
        "is_legacy": False,
        "is_mixed": False,
        "created_at_unix": record.created_at_unix,
    }


def subscription_json(store: ElevenLabsStore, now_unix: int) -> dict[str, object]:
    """``GET /v1/user/subscription`` with live usage counts."""
    voices = store.voices
    limits = voices.subscription
    return {
        "tier": limits.tier,
        "character_count": sum(record.characters for record in store.tts_requests),
        "character_limit": limits.character_limit,
        "can_extend_character_limit": False,
        "allowed_to_extend_character_limit": False,
        "next_character_count_reset_unix": now_unix + _RESET_PERIOD_S,
        "voice_slots_used": voices.cloned_count(),
        "professional_voice_slots_used": 0,
        "voice_limit": limits.voice_limit,
        "max_voice_add_edits": limits.max_voice_add_edits,
        "voice_add_edit_counter": voices.add_edit_counter,
        "professional_voice_limit": 0,
        "can_extend_voice_limit": False,
        "can_use_instant_voice_cloning": True,
        "can_use_professional_voice_cloning": False,
        "currency": "usd",
        "status": "active",
        "billing_period": "monthly_period",
        "character_refresh_period": "monthly_period",
    }
