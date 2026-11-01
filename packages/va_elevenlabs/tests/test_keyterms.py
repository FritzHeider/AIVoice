"""Keyterm parsing, validation, dedupe, ordering, cap, and the glossary line (specs/08 §6)."""

from __future__ import annotations

import pytest

from va_elevenlabs.keyterms import Keyterm, glossary_line, merge_keyterms, parse_keyword


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("Acme", Keyterm("Acme", 2)),
        ("Acme Corp:7", Keyterm("Acme Corp", 7)),
        ("  Blandy  :10 ", Keyterm("Blandy", 10)),
        ("ratio 3:2x", Keyterm("ratio 3:2x", 2)),
        ("one  two", Keyterm("one two", 2)),
    ],
)
def test_parse_keyword(item: str, expected: Keyterm) -> None:
    assert parse_keyword(item) == expected


@pytest.mark.parametrize(
    "item",
    [
        "",
        "   ",
        ":5",
        "word:0",
        "word:11",
        "word:-1",
        "x" * 50,
        "a b c d e f",
        "a<b",
        "{x}",
        "back\\slash",
        "[y]",
    ],
)
def test_invalid_terms(item: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        parse_keyword(item)


def test_merge_dedupes_sorts_and_warns() -> None:
    merged = merge_keyterms([["acme:3", "Blandy", "bad<term"], ["Karen:6", "ACME:8", "Zed:5"], ["blandy:1"]])
    assert merged.terms == ("acme", "Karen", "Zed", "Blandy")
    assert merged.glossary == ("acme", "Karen", "Zed")
    assert merged.warnings == ("keyterm 'bad<term' dropped: contains one of < > { } [ ] \\",)


@pytest.mark.parametrize("cap", [100, 1000])
def test_cap(cap: int) -> None:
    terms = [f"term{index}" for index in range(cap + 5)]
    merged = merge_keyterms([terms, ["vip:9"]], cap=cap)
    assert len(merged.terms) == cap
    assert merged.terms[0] == "vip"
    assert merged.terms[-1] == f"term{cap - 2}"
    assert merged.warnings == (f"6 keyterms over the limit of {cap} dropped",)


def test_default_cap_is_100() -> None:
    assert len(merge_keyterms([[f"t{index}" for index in range(150)]]).terms) == 100


def test_glossary_line() -> None:
    assert glossary_line(()) is None
    assert glossary_line(("Acme", "Blandy")) == "Important names and terms: Acme, Blandy"
