"""Bland ``language`` values mapped to Scribe and TTS language codes (specs/08-stt-elevenlabs.md §5).

The table is pure data shared by the Send Call validator (va_domain.calls) and the ElevenLabs clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_TTS_LANGUAGE",
    "LANGUAGE_MAP",
    "TTS_SUPPORTED",
    "LanguageMapping",
    "lookup_language",
]

DEFAULT_TTS_LANGUAGE: Final = "en"

# The 32 languages of eleven_flash_v2_5.
TTS_SUPPORTED: Final = frozenset(
    [
        "en",
        "ja",
        "zh",
        "de",
        "hi",
        "fr",
        "ko",
        "pt",
        "it",
        "es",
        "id",
        "nl",
        "tr",
        "fil",
        "pl",
        "sv",
        "bg",
        "ro",
        "ar",
        "cs",
        "el",
        "fi",
        "hr",
        "ms",
        "sk",
        "da",
        "ta",
        "uk",
        "ru",
        "hu",
        "no",
        "vi",
    ]
)


@dataclass(frozen=True, slots=True)
class LanguageMapping:
    """``scribe_code`` is sent as ``language_code`` (``None``: omitted, detection on). TTS speaks
    ``tts_code`` when fixed, else the detected language when it is in ``tts_choices`` (else English)."""

    scribe_code: str | None
    detect: bool
    tts_code: str | None
    tts_choices: frozenset[str] = frozenset()


def _fixed(code: str, tts_code: str | None = None) -> LanguageMapping:
    return LanguageMapping(code, detect=False, tts_code=tts_code or code)


def _detected(choices: frozenset[str]) -> LanguageMapping:
    return LanguageMapping(None, detect=True, tts_code=None, tts_choices=choices)


# One entry per specs/08 §5 row: the accepted values (aliases) and their mapping.
_ROWS: Final[tuple[tuple[tuple[str, ...], LanguageMapping], ...]] = (
    (("babel-en", "en", "en-US", "en-GB", "en-AU", "en-NZ", "en-IN"), _fixed("en")),
    (("babel",), _detected(TTS_SUPPORTED)),
    (("auto",), _detected(frozenset({"en", "es"}))),
    (("fluent",), _detected(frozenset({"en", "es", "de", "fr", "pt", "it"}))),
    (("es", "es-419", "es-ES", "es-MX", "babel-es"), _fixed("es")),
    (("fr", "fr-FR", "fr-CA", "babel-fr"), _fixed("fr")),
    (("de", "de-DE", "babel-de"), _fixed("de")),
    (("it", "babel-it"), _fixed("it")),
    (("pt", "pt-BR", "pt-PT", "babel-pt"), _fixed("pt")),
    (("nl", "babel-nl"), _fixed("nl")),
    (("ja", "babel-ja"), _fixed("ja")),
    (("zh", "zh-CN", "zh-Hans", "zh-TW", "zh-Hant", "babel-zh"), _fixed("zh")),
    (("ko", "babel-ko"), _fixed("ko")),
    (("hi", "hi-Latn", "babel-hi"), _fixed("hi")),
    (("ru", "babel-ru"), _fixed("ru")),
    (("pl", "babel-pl"), _fixed("pl")),
    (("tr", "babel-tr"), _fixed("tr")),
    (("sv", "babel-sv"), _fixed("sv")),
    (("da", "babel-da"), _fixed("da")),
    (("no", "nb", "babel-no"), _fixed("no")),
    (("fi", "babel-fi"), _fixed("fi")),
    (("uk", "babel-uk"), _fixed("uk")),
    (("cs", "babel-cs"), _fixed("cs")),
    (("el", "babel-el"), _fixed("el")),
    (("ro", "babel-ro"), _fixed("ro")),
    (("hu", "babel-hu"), _fixed("hu")),
    (("id", "babel-id"), _fixed("id")),
    (("ms", "babel-ms"), _fixed("ms")),
    (("vi", "babel-vi"), _fixed("vi")),
    (("th", "babel-th"), _fixed("th", DEFAULT_TTS_LANGUAGE)),
    (("ar", "babel-ar"), _fixed("ar")),
    (("he", "babel-he"), _fixed("he", DEFAULT_TTS_LANGUAGE)),
    (("ta", "babel-ta"), _fixed("ta")),
    (("bg", "babel-bg"), _fixed("bg")),
    (("hr", "babel-hr"), _fixed("hr")),
    (("sk", "babel-sk"), _fixed("sk")),
    (("fil", "tl", "babel-fil"), _fixed("fil")),
    (("ca", "babel-ca"), _fixed("ca", DEFAULT_TTS_LANGUAGE)),
)

LANGUAGE_MAP: Final[dict[str, LanguageMapping]] = {
    alias: mapping for aliases, mapping in _ROWS for alias in aliases
}
_FOLDED: Final = {alias.casefold(): mapping for alias, mapping in LANGUAGE_MAP.items()}


def lookup_language(value: str) -> LanguageMapping | None:
    """The mapping of a Bland ``language`` value (trimmed, case-insensitive), ``None`` when unknown."""
    return _FOLDED.get(value.strip().casefold())
