"""Conversation window trimming (specs/10 §6 Decision P05-T09b)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from va_llm.models import ChatMessage, ToolCall
from va_llm.prompts import context_update, interrupted_message
from va_llm.tokens import default_counter
from va_llm.window import OMITTED_NOTE, ConversationWindow, split_turns

COUNTER = default_counter()
NOTE = ChatMessage(role="system", content=OMITTED_NOTE)
OPENER = ChatMessage(role="assistant", content="Hi, this is Ava from Acme. How can I help?")


def user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def tool_turn(index: int) -> tuple[ChatMessage, ...]:
    call = ToolCall(id=f"call-{index}", name="lookup", arguments_json=f'{{"q":"order {index}"}}')
    return (
        user(f"Where is my order number {index}?"),
        ChatMessage(role="assistant", tool_calls=(call,)),
        ChatMessage(role="tool", content=f'{{"status":"shipped {index}"}}', tool_call_id=call.id),
        assistant(f"Order {index} has shipped and arrives tomorrow."),
    )


def history(turns: int) -> list[ChatMessage]:
    messages = [OPENER]
    for index in range(turns):
        messages.extend(tool_turn(index))
    return messages


def assert_no_orphan_tools(messages: tuple[ChatMessage, ...]) -> None:
    offered: set[str] = set()
    for message in messages:
        offered.update(call.id for call in message.tool_calls)
        if message.role == "tool":
            assert message.tool_call_id in offered


def test_under_budget_is_unchanged() -> None:
    messages = history(2)

    fit = ConversationWindow(6000, COUNTER).fit(messages)

    assert fit.messages == tuple(messages)
    assert fit.dropped == 0
    assert not fit.over_budget
    assert fit.tokens == COUNTER.messages(messages)


def test_over_budget_drops_oldest_whole_turns_and_keeps_the_opener() -> None:
    messages = history(6)
    turn_tokens = COUNTER.messages(tool_turn(5))
    budget = COUNTER.message(OPENER) + COUNTER.message(NOTE) + 2 * turn_tokens + 2

    fit = ConversationWindow(budget, COUNTER).fit(messages)

    assert fit.dropped == 4
    assert fit.messages == (OPENER, NOTE, *tool_turn(4), *tool_turn(5))
    assert fit.tokens == COUNTER.messages(fit.messages)
    assert fit.tokens <= budget
    assert not fit.over_budget
    assert_no_orphan_tools(fit.messages)


def test_without_opener_the_note_comes_first() -> None:
    messages = [message for index in range(4) for message in tool_turn(index)]
    budget = COUNTER.message(NOTE) + COUNTER.messages(tool_turn(3)) + 1

    fit = ConversationWindow(budget, COUNTER).fit(messages)

    assert fit.messages == (NOTE, *tool_turn(3))
    assert fit.dropped == 3


def test_current_turn_alone_over_budget_is_kept() -> None:
    messages = [OPENER, user("x " * 200)]

    fit = ConversationWindow(50, COUNTER).fit(messages)

    assert fit.messages == tuple(messages)
    assert fit.over_budget
    assert fit.dropped == 0


def test_minimum_window_still_over_budget_is_flagged() -> None:
    messages = [OPENER, user("hello"), assistant("hi"), user("x " * 200)]

    fit = ConversationWindow(100, COUNTER).fit(messages)

    assert fit.messages == (OPENER, NOTE, user("x " * 200))
    assert fit.over_budget
    assert fit.dropped == 1


def test_leading_context_updates_form_a_droppable_group() -> None:
    update = context_update("The caller is a premium member.")
    messages = [OPENER, update, *tool_turn(0), *tool_turn(1)]
    budget = COUNTER.messages([OPENER, NOTE, *tool_turn(1)]) + 1

    fit = ConversationWindow(budget, COUNTER).fit(messages)

    assert fit.messages == (OPENER, NOTE, *tool_turn(1))
    assert fit.dropped == 2


def test_context_updates_inside_a_turn_leave_with_it() -> None:
    opener, turns = split_turns(
        [OPENER, user("a"), context_update("x"), interrupted_message("Sure, I "), user("b")]
    )

    assert opener == OPENER
    assert turns == [
        (user("a"), context_update("x"), assistant("Sure, I—")),
        (user("b"),),
    ]


def test_assistant_with_tool_calls_first_is_not_an_opener() -> None:
    call = ToolCall(id="c", name="x", arguments_json="{}")
    first = ChatMessage(role="assistant", tool_calls=(call,))

    opener, turns = split_turns([first, ChatMessage(role="tool", content="{}", tool_call_id="c")])

    assert opener is None
    assert len(turns) == 1


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        ConversationWindow(0, COUNTER)
    assert ConversationWindow(10, COUNTER).budget_tokens == 10


@settings(max_examples=60, deadline=None)
@given(
    with_opener=st.booleans(),
    turn_sizes=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=8),
    budget=st.integers(min_value=20, max_value=400),
)
def test_result_is_opener_note_and_a_suffix_of_turns(
    with_opener: bool, turn_sizes: list[int], budget: int
) -> None:
    messages: list[ChatMessage] = [OPENER] if with_opener else []
    for index, size in enumerate(turn_sizes):
        messages.append(user(f"question {index}"))
        messages.extend(assistant(f"answer {index} part {part}") for part in range(size))
    opener, turns = split_turns(messages)

    fit = ConversationWindow(budget, COUNTER).fit(messages)

    head = () if opener is None else (opener,)
    kept = turns[fit.dropped :]
    tail = tuple(message for turn in kept for message in turn)
    expected = (*head, *tail) if fit.dropped == 0 else (*head, NOTE, *tail)
    assert fit.messages == expected
    assert fit.tokens == COUNTER.messages(fit.messages)
    assert fit.over_budget == (fit.tokens > budget)
    if fit.over_budget:
        assert len(kept) == 1
