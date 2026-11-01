"""Bland language values map to Scribe and TTS codes as the specs/08 §5 table says; detection locking."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from va_common.errors import AppError
from va_elevenlabs.languages import (
    LANGUAGE_MAP,
    TTS_SUPPORTED,
    LanguageLock,
    resolve_language,
    tts_language,
)

SPEC = Path(__file__).parents[3] / "specs" / "08-stt-elevenlabs.md"
DETECTED = {"babel", "auto", "fluent"}


def spec_rows() -> list[tuple[list[str], str | None, bool, str | None]]:
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("## 5.") : text.index("## 6.")]
    rows: list[tuple[list[str], str | None, bool, str | None]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or not cells[0].startswith("`"):
            continue
        aliases = re.findall(r"`([^`]+)`", cells[0])
        scribe = re.findall(r"`([^`]+)`", cells[1])
        tts = re.findall(r"`([^`]+)`", cells[3])
        fixed_tts = tts[0] if tts and aliases[0] not in DETECTED else None
        rows.append((aliases, scribe[0] if scribe else None, cells[2] == "on", fixed_tts))
    return rows


def test_every_spec_row_and_alias_maps() -> None:
    rows = spec_rows()
    assert len(rows) == 38
    aliases = [alias for row in rows for alias in row[0]]
    assert sorted(aliases) == sorted(LANGUAGE_MAP)
    for row_aliases, scribe, detect, tts in rows:
        for alias in row_aliases:
            mapping = resolve_language(alias)
            assert (mapping.scribe_code, mapping.detect, mapping.tts_code) == (scribe, detect, tts), alias


def test_lookup_is_case_insensitive() -> None:
    assert resolve_language(" EN-us ") is LANGUAGE_MAP["en-US"]


@pytest.mark.parametrize("value", ["klingon", "", "en_US"])
def test_unknown_values_are_invalid_input(value: str) -> None:
    with pytest.raises(AppError) as caught:
        resolve_language(value)
    assert (caught.value.code, caught.value.field, caught.value.http_status) == (
        "INVALID_INPUT",
        "language",
        400,
    )


def test_tts_supported_has_the_32_flash_languages() -> None:
    assert len(TTS_SUPPORTED) == 32
    assert {"th", "he", "ca"}.isdisjoint(TTS_SUPPORTED)


def test_tts_language_for_detected_modes() -> None:
    babel, auto, fluent = (resolve_language(name) for name in ("babel", "auto", "fluent"))
    assert tts_language(babel, "ja") == "ja"
    assert tts_language(babel, "th") == "en"
    assert tts_language(babel, None) == "en"
    assert tts_language(auto, "es") == "es"
    assert tts_language(auto, "de") == "en"
    assert tts_language(fluent, "de") == "de"
    assert tts_language(fluent, "ja") == "en"
    assert tts_language(resolve_language("th"), "th") == "en"
    assert tts_language(resolve_language("fr-CA"), "en") == "fr"


def lock_state(lock: LanguageLock) -> tuple[str | None, str | None]:
    """``(locked, current)``, read through a call so assertions do not narrow the properties."""
    return lock.locked, lock.current


def test_lock_when_the_first_two_agree() -> None:
    lock = LanguageLock()
    assert lock_state(lock) == (None, None)
    assert lock.observe(None) is None
    assert lock.observe("es") == "es"
    assert lock_state(lock) == (None, "es")
    assert lock.observe("es") == "es"
    assert lock_state(lock) == ("es", "es")
    assert lock.observe("en") == "es"


def test_disagreement_uses_the_latest_until_a_pair_agrees() -> None:
    lock = LanguageLock()
    lock.observe("en")
    assert lock.observe("es") == "es"
    assert lock_state(lock) == (None, "es")
    assert lock.observe(None) == "es"
    assert lock.observe("fr") == "fr"
    assert lock.observe("fr") == "fr"
    assert lock.locked == "fr"
