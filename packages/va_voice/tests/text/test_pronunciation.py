"""Pronunciation guide applier (specs/09-tts-elevenlabs.md §6 and its Decision P04-T07)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from va_voice.text.pronunciation import (
    OffsetMap,
    PronunciationApplier,
    PronunciationRule,
    Replacement,
    spell_out,
)


def apply(text: str, *rules: PronunciationRule) -> str:
    return PronunciationApplier(rules).apply(text).text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SQL and sql and Sql", "sequel and sequel and sequel"),
        ("concatenate the cat", "concatenate the kitty"),
        ("the cat's toy", "the kitty's toy"),
        ("cat_x cat1 xcat cat.", "cat_x cat1 xcat kitty."),
        ("CAT", "kitty"),
    ],
)
def test_case_insensitive_whole_words_by_default(text: str, expected: str) -> None:
    rules = (PronunciationRule("sql", "sequel"), PronunciationRule("cat", "kitty"))
    assert apply(text, *rules) == expected


def test_case_sensitive_rule_replaces_only_the_exact_case() -> None:
    rule = PronunciationRule("US", "United States", case_sensitive=True)
    assert apply("US and us and Us", rule) == "United States and us and Us"


@pytest.mark.parametrize(
    ("word", "text", "expected"),
    [
        ("Dr.", "Ask Dr. Who", "Ask Doctor Who"),
        ("C++", "I like C++ a lot", "I like Doctor a lot"),
        ("#1", "We are #1!", "We are Doctor!"),
        ("a.b", "axb a.b", "axb Doctor"),
    ],
)
def test_words_with_punctuation_are_escaped(word: str, text: str, expected: str) -> None:
    assert apply(text, PronunciationRule(word, "Doctor")) == expected


def test_spaced_spells_out_the_pronunciation() -> None:
    assert (
        apply("Code ABC123 now", PronunciationRule("ABC123", "ABC123", spaced=True)) == "Code A B C 1 2 3 now"
    )


def test_spaced_drops_whitespace_from_the_pronunciation() -> None:
    assert apply("id XY", PronunciationRule("xy", "e x  y", spaced=True)) == "id e x y"


def test_spaced_with_empty_pronunciation_spells_the_matched_text_as_written() -> None:
    rule = PronunciationRule("abc", "", spaced=True)
    assert apply("abc Abc ABC", rule) == "a b c A b c A B C"


def test_spaced_with_whitespace_pronunciation_spells_the_matched_text() -> None:
    assert apply("go ok", PronunciationRule("ok", "   ", spaced=True)) == "go o k"


def test_ignored_rules() -> None:
    applier = PronunciationApplier(
        [
            PronunciationRule("", "x"),
            PronunciationRule("   ", "x"),
            PronunciationRule("cat", ""),
            PronunciationRule("dog", "hound"),
        ]
    )
    assert applier.rule_count == 1
    assert applier.replace("cat dog") == "cat hound"


def test_longest_word_first_regardless_of_input_order() -> None:
    rules = (PronunciationRule("New York", "N Y"), PronunciationRule("New York City", "N Y C"))
    assert apply("New York City and New York", *rules) == "N Y C and N Y"


def test_leftmost_match_wins_over_a_longer_later_match() -> None:
    rules = (PronunciationRule("York City", "Y C"), PronunciationRule("New York", "N Y"))
    assert apply("New York City", *rules) == "N Y City"


def test_duplicate_word_earlier_rule_wins() -> None:
    rules = (PronunciationRule("ai", "first"), PronunciationRule("AI", "second"))
    assert apply("AI", *rules) == "first"


def test_replacements_are_not_rematched() -> None:
    rules = (PronunciationRule("AI", "A I"), PronunciationRule("A", "ay"))
    assert apply("AI and A", *rules) == "A I and ay"


def test_case_flags_are_scoped_per_rule() -> None:
    rules = (PronunciationRule("US", "United States", case_sensitive=True), PronunciationRule("ok", "okay"))
    assert apply("us US OK", *rules) == "us United States okay"


def test_unicode_words_and_boundaries() -> None:
    rule = PronunciationRule("café", "cafay")
    assert apply("CAFÉ café écafé caféé", rule) == "cafay cafay écafé caféé"


def test_adjacent_matches() -> None:
    applier = PronunciationApplier([PronunciationRule("C++", "C plus plus")])
    assert applier.replace("C++C++") == "C++C plus plus"
    result = applier.apply("C++#C++")
    assert result.text == "C plus plus#C plus plus"
    assert result.offsets.to_original(11) == 3
    assert result.offsets.to_original(12) == 4
    assert result.offsets.to_original(len(result.text)) == 7


def test_offset_map_example() -> None:
    result = PronunciationApplier([PronunciationRule("sql", "sequel")]).apply("Call SQL now")
    assert result.text == "Call sequel now"
    assert result.original == "Call SQL now"
    offsets = result.offsets
    assert offsets.replacements == (Replacement(5, 11, 5, 8),)
    assert (offsets.sent_length, offsets.original_length) == (15, 12)
    assert [offsets.to_original(i) for i in (0, 4, 5, 6, 10, 11, 12, 15)] == [0, 4, 5, 5, 5, 8, 9, 12]
    assert [offsets.to_original_ceil(i) for i in (5, 6, 10, 11)] == [5, 8, 8, 8]
    for index in (-1, 16):
        with pytest.raises(ValueError, match="outside the sent text"):
            offsets.to_original(index)


def test_no_match_and_no_rules_give_identity() -> None:
    empty = PronunciationApplier([])
    assert not empty
    assert empty.rule_count == 0
    result = empty.apply("hello")
    assert (result.text, result.original, result.offsets.replacements) == ("hello", "hello", ())
    applier = PronunciationApplier([PronunciationRule("sql", "sequel")])
    assert applier
    unmatched = applier.apply("hello there")
    assert unmatched.text == "hello there"
    assert unmatched.offsets.replacements == ()
    assert OffsetMap.identity(3).to_original(3) == 3


def test_replace_equals_apply_text() -> None:
    applier = PronunciationApplier([PronunciationRule("sql", "sequel")])
    assert applier.replace("my SQL db") == applier.apply("my SQL db").text == "my sequel db"


def test_accepts_structural_rules() -> None:
    @dataclass(frozen=True)
    class ApiRule:
        word: str
        pronunciation: str
        case_sensitive: bool
        spaced: bool

    applier = PronunciationApplier([ApiRule("api", "A P I", case_sensitive=False, spaced=False)])
    assert applier.replace("the API") == "the A P I"


def test_two_hundred_rules_in_one_pattern() -> None:
    rules = [PronunciationRule(f"word{n}", f"w {n}") for n in range(200)]
    applier = PronunciationApplier(rules)
    clause = " ".join(f"word{n}" for n in range(0, 200, 9))
    assert applier.rule_count == 200
    assert applier.replace(clause) == " ".join(f"w {n}" for n in range(0, 200, 9))


def test_spell_out() -> None:
    assert spell_out("AB 1\t2") == "A B 1 2"
    assert spell_out(" ") == ""


# --- properties

WORDS = st.text(alphabet="abAB", min_size=1, max_size=3)
RULES = st.lists(
    st.builds(
        PronunciationRule,
        word=WORDS,
        pronunciation=st.text(alphabet="xyZ -", max_size=4),
        case_sensitive=st.booleans(),
        spaced=st.booleans(),
    ),
    max_size=5,
)
TEXT = st.lists(st.one_of(WORDS, st.text(alphabet="ab AB.-_é", max_size=3)), max_size=12).map("".join)


def is_word_char(text: str, index: int) -> bool:
    return 0 <= index < len(text) and re.match(r"\w", text[index]) is not None


def reference(text: str, rules: list[PronunciationRule]) -> str:
    """Naive left-to-right scan: at each position the longest (then earliest) matching rule replaces."""
    usable = [rule for rule in rules if rule.word.strip() and (rule.spaced or rule.pronunciation)]
    ordered = sorted(usable, key=lambda rule: -len(rule.word))
    out: list[str] = []
    index = 0
    while index < len(text):
        for rule in ordered:
            piece = text[index : index + len(rule.word)]
            same = piece == rule.word if rule.case_sensitive else piece.casefold() == rule.word.casefold()
            end = index + len(rule.word)
            bounded = not is_word_char(text, index - 1) and not is_word_char(text, end)
            if same and len(piece) == len(rule.word) and bounded:
                spelled = spell_out(rule.pronunciation) or spell_out(piece)
                out.append(spelled if rule.spaced else rule.pronunciation)
                index = end
                break
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


@settings(max_examples=300)
@given(text=TEXT, rules=RULES)
def test_matches_naive_reference(text: str, rules: list[PronunciationRule]) -> None:
    assert PronunciationApplier(rules).replace(text) == reference(text, rules)


@settings(max_examples=300)
@given(text=TEXT, rules=RULES)
def test_offset_map_properties(text: str, rules: list[PronunciationRule]) -> None:
    result = PronunciationApplier(rules).apply(text)
    offsets = result.offsets
    sent = result.text
    inside = {i for span in offsets.replacements for i in range(span.sent_start, span.sent_end)}
    for index in range(len(sent)):
        if index not in inside:
            assert sent[index] == text[offsets.to_original(index)]
    floors = [offsets.to_original(i) for i in range(len(sent) + 1)]
    ceils = [offsets.to_original_ceil(i) for i in range(len(sent) + 1)]
    assert floors == sorted(floors)
    assert ceils == sorted(ceils)
    assert all(low <= high for low, high in zip(floors, ceils, strict=True))
    assert floors[0] == 0
    assert floors[-1] == ceils[-1] == len(text)
    for span in offsets.replacements:
        assert text[span.original_start : span.original_end] != ""
        assert sent[span.sent_start : span.sent_end] != ""


@given(text=TEXT)
def test_no_rules_is_identity(text: str) -> None:
    result = PronunciationApplier([]).apply(text)
    assert result.text == text
    assert [result.offsets.to_original(i) for i in range(len(text) + 1)] == list(range(len(text) + 1))
