"""Simulation pieces without a model: the caller's reply parsing, the caller's view of the conversation, and
the error item (specs/12 §9; Decision P07-T11d)."""

from __future__ import annotations

import pytest
from pathways_helpers import job

from va_pathways.simulations import (
    CALLER_SUFFIX,
    SILENCE,
    CallerReply,
    caller_messages,
    parse_caller_reply,
    simulation_error_item,
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Thanks, bye! [HANGUP]", CallerReply("Thanks, bye!", hangup=True)),
        ("Okay [HANGUP] and more", CallerReply("Okay", hangup=True)),
        ("[HANGUP]", CallerReply("", hangup=True)),
        ("  A large pizza please. ", CallerReply("A large pizza please.", hangup=False)),
        ("I will [hangup] soon", CallerReply("I will [hangup] soon", hangup=False)),
        ("   ", CallerReply("", hangup=False)),
        (None, CallerReply("", hangup=False)),
    ],
)
def test_parse_caller_reply(content: str | None, expected: CallerReply) -> None:
    assert parse_caller_reply(content) == expected


def test_the_caller_sees_the_agent_as_user_and_itself_as_assistant() -> None:
    messages = caller_messages(
        "You are impatient.",
        [("assistant", "Hi."), ("user", "Pizza."), ("assistant", ""), ("user", "Hello?")],
    )
    assert [(message.role, message.content) for message in messages] == [
        ("system", f"You are impatient.\n\n{CALLER_SUFFIX}"),
        ("user", "Hi."),
        ("assistant", "Pizza."),
        ("user", SILENCE),
        ("assistant", "Hello?"),
    ]
    assert CALLER_SUFFIX == (
        'Reply only with what you would say on the phone. Say "[HANGUP]" when you would hang up.'
    )


def test_the_error_item_keeps_the_identity_of_the_job() -> None:
    assert simulation_error_item(job("2-3"), "runner_error") == {
        "simulation_id": "2-3",
        "scenario": "Hungry caller",
        "run": 3,
        "goal": "Pizza size captured",
        "passed": False,
        "status": "error",
        "ended_by": "error",
        "turns": 0,
        "transcript": [],
        "variables": {},
        "grading": None,
        "actions": [],
        "duration_ms": 0,
        "error": "runner_error",
    }
