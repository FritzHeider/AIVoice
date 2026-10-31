"""FakeElevenLabs premade voices (specs/30 §3.3 REST; Decision P03-T05c rule 1).

Eleven seeded voices with fixed 20-character ids; the first is ``Karen``. Every voice carries the five label
keys ElevenLabs uses (``accent``, ``description``, ``age``, ``gender``, ``use_case``) and verifies English;
two also verify another language.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

__all__ = [
    "DEFAULT_VOICE_SETTINGS",
    "KAREN_VOICE_ID",
    "PREMADE_VOICES",
    "VERIFIED_MODEL_ID",
    "PremadeVoice",
    "VerifiedLanguage",
]

KAREN_VOICE_ID: Final = "tpWUyeGcnpEoOkg6XK3W"
VERIFIED_MODEL_ID: Final = "eleven_flash_v2_5"
DEFAULT_VOICE_SETTINGS: Final[Mapping[str, float | bool]] = MappingProxyType(
    {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True, "speed": 1.0}
)


@dataclass(frozen=True, slots=True)
class VerifiedLanguage:
    language: str
    accent: str
    locale: str

    def to_json(self) -> dict[str, object]:
        return {
            "language": self.language,
            "model_id": VERIFIED_MODEL_ID,
            "accent": self.accent,
            "locale": self.locale,
            "preview_url": None,
        }


@dataclass(frozen=True, slots=True)
class PremadeVoice:
    voice_id: str
    name: str
    description: str
    labels: Mapping[str, str]
    languages: tuple[VerifiedLanguage, ...]


def _voice(  # noqa: PLR0913 - one argument per catalog column
    voice_id: str,
    name: str,
    description: str,
    *,
    accent: str,
    tone: str,
    age: str,
    gender: str,
    use_case: str,
    locale: str,
    extra: tuple[VerifiedLanguage, ...] = (),
) -> PremadeVoice:
    labels = {"accent": accent, "description": tone, "age": age, "gender": gender, "use_case": use_case}
    english = VerifiedLanguage("en", accent, locale)
    return PremadeVoice(voice_id, name, description, MappingProxyType(labels), (english, *extra))


PREMADE_VOICES: Final[tuple[PremadeVoice, ...]] = (
    _voice(
        KAREN_VOICE_ID,
        "Karen",
        "A warm, friendly American voice for phone conversations.",
        accent="american",
        tone="warm",
        age="young",
        gender="female",
        use_case="conversational",
        locale="en-US",
    ),
    _voice(
        "WLcrofoKElX9y3QxZNpg",
        "Aria",
        "An expressive voice with a slightly husky tone.",
        accent="american",
        tone="expressive",
        age="middle_aged",
        gender="female",
        use_case="social_media",
        locale="en-US",
        extra=(VerifiedLanguage("es", "mexican", "es-MX"),),
    ),
    _voice(
        "cBkOAFIvDxyxPzxhJPql",
        "Roger",
        "A confident, easy-going male voice.",
        accent="american",
        tone="confident",
        age="middle_aged",
        gender="male",
        use_case="conversational",
        locale="en-US",
    ),
    _voice(
        "cBesyYhNp6QY5mihJ5m9",
        "Sarah",
        "A soft, reassuring voice for customer support.",
        accent="american",
        tone="soft",
        age="young",
        gender="female",
        use_case="news",
        locale="en-US",
    ),
    _voice(
        "fk9G2p3fVbp2OxzHJofv",
        "Laura",
        "A bright, upbeat voice with a quirky edge.",
        accent="american",
        tone="upbeat",
        age="young",
        gender="female",
        use_case="social_media",
        locale="en-US",
    ),
    _voice(
        "KIm9nDCOPJiGZKLd3ASj",
        "Charlie",
        "A natural, casual Australian voice.",
        accent="australian",
        tone="casual",
        age="middle_aged",
        gender="male",
        use_case="conversational",
        locale="en-AU",
    ),
    _voice(
        "dqc6dT6YgaUQes2mxp4U",
        "George",
        "A warm British storyteller.",
        accent="british",
        tone="warm",
        age="middle_aged",
        gender="male",
        use_case="narration",
        locale="en-GB",
    ),
    _voice(
        "0HwQXSsmtoPJvMu0lESD",
        "Callum",
        "An intense, gravelly voice for characters.",
        accent="transatlantic",
        tone="intense",
        age="middle_aged",
        gender="male",
        use_case="characters",
        locale="en-US",
    ),
    _voice(
        "Hddas7IWFXJKrej3KoJM",
        "River",
        "A relaxed, neutral voice.",
        accent="american",
        tone="calm",
        age="middle_aged",
        gender="non_binary",
        use_case="informative_educational",
        locale="en-US",
    ),
    _voice(
        "7YtfWJqArDne2KNqT6Mn",
        "Liam",
        "An articulate young narrator.",
        accent="american",
        tone="articulate",
        age="young",
        gender="male",
        use_case="narration",
        locale="en-US",
    ),
    _voice(
        "zZWkWe0TNZ0PWuFGrf1X",
        "Charlotte",
        "A smooth, poised voice with a Swedish accent.",
        accent="swedish",
        tone="smooth",
        age="young",
        gender="female",
        use_case="characters",
        locale="en-GB",
        extra=(VerifiedLanguage("de", "standard", "de-DE"),),
    ),
)
