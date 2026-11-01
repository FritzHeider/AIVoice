"""Interruptibility levels, backchannels and effective level (specs/07-media-pipeline.md §7, §13)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_voice.interrupt import (
    BACKCHANNELS,
    DEFAULT_LEVEL,
    LEVEL_RULES,
    BackchannelClassifier,
    BackchannelMatch,
    InterruptDecision,
    InterruptPolicy,
    effective_level,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "07-media-pipeline.md"
POLICY = InterruptPolicy()
LEVELS = (0, 1, 2, 3, 4)


def decide(text: str, speech_ms: float, level: int) -> InterruptDecision:
    return POLICY.should_interrupt(speech_ms, None, text, level, agent_speaking=True)


def spec_backchannels() -> tuple[str, ...]:
    body = SPEC.read_text(encoding="utf-8")
    start = body.index("Backchannel set")
    paragraph = body[start : body.index("\n\n", start)]
    return tuple(re.findall(r"`([^`]+)`", paragraph))


def test_backchannel_set_matches_spec_verbatim() -> None:
    assert spec_backchannels() == BACKCHANNELS
    assert len(BACKCHANNELS) == 29


def test_level_rules_cover_levels_one_to_four() -> None:
    assert sorted(LEVEL_RULES) == [1, 2, 3, 4]
    assert DEFAULT_LEVEL == 2


# --- classifier -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "uh-huh",
        "Uh huh.",
        "mm-hmm",
        "Mm hmm",
        "mhm",
        "Okay okay!",
        "all right",
        "alright",
        "got it",
        "I see",
        "yes",
    ],
)
def test_all_backchannel(text: str) -> None:
    match = BackchannelClassifier().classify(text)
    assert match.all_backchannel
    assert match.content_words == 0
    assert BackchannelClassifier().is_backchannel(text)


@pytest.mark.parametrize(
    ("text", "total", "backchannel"),
    [
        ("I see the problem", 4, 2),
        ("yeah but wait", 3, 1),
        ("right now please", 3, 1),
        ("all right, go on", 4, 2),
        ("all", 1, 0),
        ("hold on I have a question", 6, 0),
        ("got", 1, 0),
        ("uh-huh uh huh mm", 5, 5),
        ("", 0, 0),
        ("  ...  ", 0, 0),
    ],
)
def test_counts(text: str, total: int, backchannel: int) -> None:
    match = BackchannelClassifier().classify(text)
    assert (match.total_words, match.backchannel_words, match.content_words) == (
        total,
        backchannel,
        total - backchannel,
    )


def test_empty_partial_is_not_backchannel() -> None:
    assert not BackchannelClassifier().classify("").all_backchannel
    assert not BackchannelClassifier().is_backchannel("")


def test_word_list_input_is_normalized() -> None:
    match = BackchannelClassifier().classify(["Uh-huh,", "I", "SEE", "Paris."])
    assert match == BackchannelMatch(("uh", "huh", "i", "see", "paris"), 4)


def test_custom_phrases() -> None:
    classifier = BackchannelClassifier(["sí", "claro que sí"])
    assert classifier.is_backchannel("¡Claro que sí!")
    assert classifier.classify("sí señor").content_words == 1
    assert not classifier.is_backchannel("okay")


@pytest.mark.parametrize("phrases", [["ok", "--"], []])
def test_invalid_phrase_sets(phrases: list[str]) -> None:
    with pytest.raises(ValueError, match=r"backchannel phrase|at least one"):
        BackchannelClassifier(phrases)


# --- level matrix ---------------------------------------------------------------------------------

F_INS = (False, "insufficient")
F_BLK = (False, "blocked")
T_WORDS = (True, "speech_and_words")
T_LONG = (True, "long_speech")
T_ANY = (True, "any_speech")

# scenario → (text, speech_ms, expected decision for levels 0..4), straight from the specs/07 §7 table.
MATRIX: dict[str, tuple[str, float, tuple[tuple[bool, str], ...]]] = {
    "backchannel_short": ("uh-huh", 400, (F_BLK, F_INS, F_INS, F_INS, T_ANY)),
    "backchannel_long": ("yeah", 1000, (F_BLK, F_INS, T_LONG, T_LONG, T_ANY)),
    "one_word": ("wait", 300, (F_BLK, F_INS, F_INS, T_WORDS, T_ANY)),
    "two_words": ("wait stop", 600, (F_BLK, F_INS, T_WORDS, T_LONG, T_ANY)),
    "two_words_short": ("wait stop", 450, (F_BLK, F_INS, F_INS, T_WORDS, T_ANY)),
    "sentence": ("hold on I have a question", 1300, (F_BLK, T_WORDS, T_LONG, T_LONG, T_ANY)),
    "three_words_long": ("hold on please", 1300, (F_BLK, F_INS, T_LONG, T_LONG, T_ANY)),
    "four_backchannels": ("okay okay yeah right", 1300, (F_BLK, F_INS, T_LONG, T_LONG, T_ANY)),
    "mixed_backchannel": ("yeah but wait", 550, (F_BLK, F_INS, T_WORDS, T_WORDS, T_ANY)),
    "one_content_word": ("yeah wait", 550, (F_BLK, F_INS, F_INS, T_WORDS, T_ANY)),
    "no_text_long": ("", 950, (F_BLK, F_INS, T_LONG, T_LONG, T_ANY)),
    "no_text_short": ("", 140, (F_BLK, F_INS, F_INS, F_INS, F_INS)),
}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("scenario", sorted(MATRIX))
def test_matrix(scenario: str, level: int) -> None:
    text, speech_ms, expected = MATRIX[scenario]
    decision = decide(text, speech_ms, level)
    assert (decision.interrupt, decision.reason) == expected[level]
    assert bool(decision) is expected[level][0]


@pytest.mark.parametrize(
    ("level", "text", "below", "at"),
    [
        (4, "", 149.999, 150),
        (3, "wait", 249.999, 250),
        (3, "", 599.999, 600),
        (2, "wait stop", 499.999, 500),
        (2, "", 899.999, 900),
        (1, "hold on a second", 1199.999, 1200),
    ],
)
def test_boundaries_are_inclusive(level: int, text: str, below: float, at: float) -> None:
    assert not decide(text, below, level)
    assert decide(text, at, level)


def test_level_one_needs_four_words() -> None:
    assert not decide("hold on please", 5000, 1)
    assert decide("hold on please now", 5000, 1)
    assert decide("yeah yeah yeah stop", 5000, 1)  # not all backchannel
    assert not decide("yeah yeah yeah yeah", 5000, 1)


@pytest.mark.parametrize("level", LEVELS)
def test_never_interrupts_when_agent_silent(level: int) -> None:
    decision = POLICY.should_interrupt(5000, None, "hold on I have a question", level, agent_speaking=False)
    assert decision == InterruptDecision(interrupt=False, reason="not_speaking")


def test_word_list_overrides_text() -> None:
    assert POLICY.should_interrupt(550, ["wait", "stop"], "uh-huh", 2, agent_speaking=True)
    assert not POLICY.should_interrupt(550, ["uh-huh"], "wait stop", 2, agent_speaking=True)


def test_custom_classifier_is_used() -> None:
    policy = InterruptPolicy(BackchannelClassifier(["wait"]))
    assert policy.classifier.is_backchannel("wait")
    assert not policy.should_interrupt(300, None, "wait", 3, agent_speaking=True)


@pytest.mark.parametrize(("level", "speech_ms"), [(5, 0), (-1, 0), (2, -1)])
def test_invalid_inputs(level: int, speech_ms: float) -> None:
    with pytest.raises(ValueError, match=r"interruptibility|speech_ms"):
        decide("wait", speech_ms, level)


def test_bool_level_rejected() -> None:
    with pytest.raises(ValueError, match="interruptibility"):
        decide("wait", 100, True)


WORDS = st.lists(
    st.sampled_from(["uh-huh", "yeah", "wait", "stop", "i", "see", "okay", "please", "now"]), max_size=6
)


@given(words=WORDS, speech=st.floats(0, 3000), extra=st.floats(0, 3000), level=st.integers(0, 4))
def test_monotonic_in_speech(words: list[str], speech: float, extra: float, level: int) -> None:
    text = " ".join(words)
    if decide(text, speech, level):
        assert decide(text, speech + extra, level)


@given(words=WORDS, speech=st.floats(0, 3000), level=st.integers(0, 3))
def test_monotonic_in_level(words: list[str], speech: float, level: int) -> None:
    text = " ".join(words)
    if decide(text, speech, level):
        assert decide(text, speech, level + 1)


# --- DTMF -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("speaking", [True, False])
def test_dtmf(level: int, speaking: bool) -> None:
    decision = POLICY.should_interrupt_dtmf(level, agent_speaking=speaking)
    if not speaking:
        assert decision.reason == "not_speaking"
    elif level == 0:
        assert decision.reason == "blocked"
    else:
        assert decision == InterruptDecision(interrupt=True, reason="dtmf")
    assert bool(decision) is (speaking and level >= 1)


def test_dtmf_invalid_level() -> None:
    with pytest.raises(ValueError, match="interruptibility"):
        POLICY.should_interrupt_dtmf(7, agent_speaking=True)


# --- effective level ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, 2),
        ({"call_level": 4}, 4),
        ({"call_level": 4, "node_level": 1}, 1),
        ({"call_level": 1, "node_level": 3}, 3),
        ({"call_level": 3, "node_level": 0}, 0),
        ({"call_level": 4, "node_level": 3, "block_interruptions": True}, 0),
        ({"call_level": 4, "interruptible": False}, 0),
        ({"call_level": 1, "node_level": 2, "interruptible": True}, 2),
        ({"call_level": 0, "interruptible": True}, 0),
    ],
)
def test_effective_level(kwargs: dict[str, int | bool], expected: int) -> None:
    assert effective_level(**kwargs) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"call_level": 5},
        {"call_level": -1},
        {"node_level": 4},
        {"node_level": -1},
        {"call_level": 2, "node_level": 9},
    ],
)
def test_effective_level_validation(kwargs: dict[str, int]) -> None:
    call_level = kwargs.get("call_level", DEFAULT_LEVEL)
    node_level = kwargs.get("node_level")
    with pytest.raises(ValueError, match="interruptibility"):
        effective_level(call_level=call_level, node_level=node_level)


def test_effective_level_validates_even_when_blocked() -> None:
    with pytest.raises(ValueError, match="node interruptibility"):
        effective_level(node_level=4, block_interruptions=True)
