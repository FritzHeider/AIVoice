"""Paraphrase parsing and the variation / grading prompts (specs/12 §8 Decision P07-T11a)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from va_llm import Completion, LlmUnavailableError
from va_pathways.grading import GRADE_PROMPT, GradeVerdict, grade_messages
from va_pathways.variations import (
    VARIATION_PROMPT,
    Variations,
    first_json_array,
    paraphrases,
    parse_variations,
    variation_messages,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "12-pathways-api.md"


def _spec_prompt(opening: str) -> str:
    section = SPEC.read_text(encoding="utf-8").split("## 8. Unit tests", 1)[1].split("## 9.", 1)[0]
    match = re.search(r"`(" + re.escape(opening) + r"[^`]*)`", section)
    assert match is not None
    return re.sub(r"\s+", " ", match.group(1))


def test_prompts_match_the_spec() -> None:
    assert _spec_prompt("Given the conversation") == GRADE_PROMPT
    assert _spec_prompt("Rewrite the caller message") == VARIATION_PROMPT


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('Here: ["a", "b"] thanks', ["a", "b"]),
        ('[broken ["x"]', ["x"]),
        ("[1, 2] then [3]", [1, 2]),
        ("no array", None),
        ("", None),
        ("[", None),
    ],
)
def test_first_json_array(text: str, found: list[object] | None) -> None:
    assert first_json_array(text) == found


def test_parse_variations_filters_dedupes_and_counts() -> None:
    content = (
        '["  Order food ", "order FOOD", 3, null, "", "I want to order some food", "Food please", "More"]'
    )
    parsed = parse_variations(content, "I want to order some food", 3)
    assert parsed == Variations(("Order food", "Food please", "More"), 0)
    assert parse_variations(content, "x", 10) == Variations(
        ("Order food", "I want to order some food", "Food please", "More"), 6
    )
    assert parse_variations("sorry", "x", 2) == Variations((), 2)
    assert parse_variations(None, "x", 1) == Variations((), 1)


def test_messages() -> None:
    [system, user] = variation_messages("Hello", 4)
    assert (system.role, user.role, user.content) == ("system", "user", "Hello")
    assert system.content == VARIATION_PROMPT.replace("{n}", "4")
    transcript, prompt = grade_messages([("user", "Hi"), ("assistant", "Hello"), ("tool", "x")], "Greet {n}")
    assert transcript.content == "Conversation:\nCaller: Hi\nAgent: Hello\ntool: x"
    assert prompt.content == GRADE_PROMPT.replace("{goal}", "Greet {n}")


def test_verdict_reads_the_pass_key() -> None:
    verdict = GradeVerdict.model_validate({"pass": True, "rationale": "ok"})
    assert verdict.pass_ is True
    assert verdict.model_dump(by_alias=True) == {"pass": True, "rationale": "ok"}
    assert GradeVerdict.model_json_schema()["required"] == ["pass", "rationale"]


class _NeverCalled:
    calls = 0

    async def complete(self, *_args: object, **_kwargs: object) -> Completion:
        self.calls += 1
        raise LlmUnavailableError("exhausted", provider_id="p", model="m", last=None)


async def test_zero_variations_send_no_request() -> None:
    port = _NeverCalled()
    assert await paraphrases(port, org_id=uuid.uuid4(), message="Hi", n=0) == Variations((), 0)
    assert port.calls == 0
