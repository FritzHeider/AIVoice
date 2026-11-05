"""System prompt share of the model context (specs/10 §6 Decision P05-T09b)."""

from __future__ import annotations

from datetime import UTC, datetime

from va_llm.prompts import MEMORY_MAX_TOKENS, VoicePromptInputs, fit_voice_system, render_voice_system
from va_llm.tokens import default_counter

COUNTER = default_counter()
CHUNKS = tuple(
    f"Fact number {index}: the office is open on day {index} from nine to five." for index in range(8)
)
MEMORY = "The caller likes morning appointments and has two dogs. " * 120


def base(**overrides: object) -> VoicePromptInputs:
    values: dict[str, object] = {
        "now_local": datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        "timezone": "UTC",
        "direction": "web",
        "language_name": "English",
        "instructions": "Help.",
        "knowledge_chunks": CHUNKS,
    }
    values.update(overrides)
    return VoicePromptInputs(**values)  # type: ignore[arg-type]


def size(inputs: VoicePromptInputs) -> int:
    return COUNTER.text(render_voice_system(inputs))


def max_context_for(tokens: int) -> int:
    """The smallest ``max_context`` whose 60 % share holds ``tokens``."""
    return -(-tokens * 10 // 6) + 1


def test_under_budget_is_unchanged() -> None:
    inputs = base(memory_context=MEMORY)
    assert fit_voice_system(inputs, counter=COUNTER) is inputs


def test_lowest_ranked_chunks_drop_first() -> None:
    inputs = base()
    target = size(base(knowledge_chunks=CHUNKS[:5]))

    fitted = fit_voice_system(inputs, counter=COUNTER, max_context=max_context_for(target))

    assert fitted.knowledge_chunks == CHUNKS[:5]


def test_extra_tokens_count_against_the_share() -> None:
    target = size(base(knowledge_chunks=CHUNKS[:5]))

    fitted = fit_voice_system(
        base(), counter=COUNTER, max_context=max_context_for(target + 40), extra_tokens=40
    )

    assert fitted.knowledge_chunks == CHUNKS[:5]


def test_memory_is_truncated_after_all_chunks_are_gone() -> None:
    inputs = base(memory_context=MEMORY)
    truncated = COUNTER.truncate(MEMORY, MEMORY_MAX_TOKENS)
    target = size(base(knowledge_chunks=(), memory_context=truncated))

    fitted = fit_voice_system(inputs, counter=COUNTER, max_context=max_context_for(target))

    assert fitted.knowledge_chunks == ()
    assert fitted.memory_context == truncated
    assert COUNTER.text(truncated) == MEMORY_MAX_TOKENS


def test_instructions_are_never_cut() -> None:
    inputs = base(instructions="Long instructions. " * 200, memory_context="Short memory.")

    fitted = fit_voice_system(inputs, counter=COUNTER, max_context=100)

    assert fitted.instructions == inputs.instructions
    assert fitted.knowledge_chunks == ()
    assert fitted.memory_context == "Short memory."


def test_context_lines_are_never_cut_and_count_towards_the_size() -> None:
    lines = ("The customer's name is Ana.", "Their plan is Pro and renews every March.")
    inputs = base(memory_context=MEMORY, context_sections=lines)
    without_lines = size(base(knowledge_chunks=CHUNKS[:5]))

    fitted = fit_voice_system(inputs, counter=COUNTER, max_context=max_context_for(without_lines))
    starved = fit_voice_system(inputs, counter=COUNTER, max_context=10)

    assert fitted.context_sections == lines
    assert len(fitted.knowledge_chunks) < 5
    assert (starved.context_sections, starved.knowledge_chunks) == (lines, ())
    assert render_voice_system(starved).endswith("## Context\n" + "\n".join(lines))
