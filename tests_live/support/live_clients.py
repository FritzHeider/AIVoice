"""Production endpoints and model defaults for the live smoke suite (specs/30 §7 Decision P03-T16 rule 3)."""

from __future__ import annotations

from typing import Final

from va_llm.settings import LlmSettings

__all__ = [
    "BATCH_STT_MODEL",
    "ELEVENLABS_API_BASE",
    "ELEVENLABS_WS_BASE",
    "LLM_BASE_URL",
    "TELNYX_API_BASE",
    "embeddings_dimensions",
    "llm_default",
]

# specs/02 §6 "real:" values.
TELNYX_API_BASE: Final = "https://api.telnyx.com/v2"
ELEVENLABS_API_BASE: Final = "https://api.elevenlabs.io"
ELEVENLABS_WS_BASE: Final = "wss://api.elevenlabs.io"
LLM_BASE_URL: Final = "https://api.telnyx.com/v2/ai/openai"
# specs/02 §6 ELEVENLABS_BATCH_STT_MODEL default; no client constant exists until the worker settings land.
BATCH_STT_MODEL: Final = "scribe_v2"


def llm_default(name: str) -> str:
    """A specs/02 §6 model default as declared on :class:`LlmSettings` (e.g. ``llm_dialogue_model``)."""
    value = LlmSettings.model_fields[name].default
    if not isinstance(value, str):
        msg = f"LlmSettings.{name} has no string default"
        raise TypeError(msg)
    return value


def embeddings_dimensions() -> int:
    value = LlmSettings.model_fields["embeddings_dimensions"].default
    if not isinstance(value, int):
        msg = "LlmSettings.embeddings_dimensions has no integer default"
        raise TypeError(msg)
    return value
