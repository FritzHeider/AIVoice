"""The shared Bland language table (specs/08 §5)."""

from __future__ import annotations

import pytest

from va_domain.languages import (
    DEFAULT_TTS_LANGUAGE,
    LANGUAGE_MAP,
    TTS_SUPPORTED,
    UNKNOWN_LANGUAGE_NAME,
    language_name,
    lookup_language,
)


@pytest.mark.parametrize("alias", sorted(LANGUAGE_MAP))
def test_every_alias_resolves_in_any_case(alias: str) -> None:
    assert lookup_language(alias) is LANGUAGE_MAP[alias]
    assert lookup_language(f" {alias.upper()} ") is LANGUAGE_MAP[alias]
    assert lookup_language(alias.lower()) is LANGUAGE_MAP[alias]


@pytest.mark.parametrize("value", ["xx", "", "english", "babel-xx"])
def test_unknown_values_are_none(value: str) -> None:
    assert lookup_language(value) is None


def test_table_shape() -> None:
    assert DEFAULT_TTS_LANGUAGE == "en"
    assert len(TTS_SUPPORTED) == 32
    assert LANGUAGE_MAP["babel-en"].scribe_code == "en"
    assert LANGUAGE_MAP["babel"].detect
    assert LANGUAGE_MAP["th"].tts_code == DEFAULT_TTS_LANGUAGE


@pytest.mark.parametrize("alias", sorted(LANGUAGE_MAP))
def test_every_fixed_language_has_a_name(alias: str) -> None:
    name = language_name(alias)
    if LANGUAGE_MAP[alias].detect:
        assert name == UNKNOWN_LANGUAGE_NAME
    else:
        assert name != UNKNOWN_LANGUAGE_NAME
        assert name[0].isupper()


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("babel-en", "English"),
        (" es-MX ", "Spanish"),
        ("nb", "Norwegian"),
        ("tl", "Filipino"),
        ("auto", UNKNOWN_LANGUAGE_NAME),
        ("xx", UNKNOWN_LANGUAGE_NAME),
    ],
)
def test_language_names(value: str, name: str) -> None:
    assert language_name(value) == name
