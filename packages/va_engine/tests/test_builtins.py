"""Built-in template variables of a call (specs/11 §8 Decision P05-T09a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from resolved_configs import resolved_config

from va_domain.call_context import CallContext
from va_engine.builtins import call_builtins, call_zone
from va_engine.templating import TemplateScope, render_text

NOW = datetime(2026, 11, 2, 3, 4, 5, 678_000, tzinfo=UTC)
CALL_ID = uuid.UUID("01900000-0000-7000-8000-000000000001")
BATCH_ID = uuid.UUID("01900000-0000-7000-8000-000000000002")


def _context(direction: str = "outbound", **config: Any) -> CallContext:
    return CallContext.model_validate(
        {
            "call_id": CALL_ID,
            "org_id": uuid.uuid4(),
            "direction": direction,
            "to_number": "+14155550100",
            "from_number": "+14155550190",
            "config": {**resolved_config(), **config},
            "created_at": NOW,
        }
    )


def test_outbound_builtins() -> None:
    context = _context(timezone="America/New_York", language="en-US", metadata={"k": "v"})

    values = call_builtins(context, now=NOW, last_user_message="hello", prev_node_prompt="greet")

    assert values == {
        "lastUserMessage": "hello",
        "prevNodePrompt": "greet",
        "now_utc": "2026-11-02T03:04:05+00:00",
        "now_local": "2026-11-01T22:04:05-05:00",
        "today": "2026-11-01",
        "from": "+14155550190",
        "to": "+14155550100",
        "call_id": str(CALL_ID),
        "voice_id": str(context.config.voice_id),
        "timezone": "America/New_York",
        "language": "en-US",
        "agent_number": "+14155550190",
        "user_number": "+14155550100",
        "batch_id": None,
        "metadata": {"k": "v"},
    }


def test_inbound_swaps_agent_and_user_numbers() -> None:
    context = _context("inbound").model_copy(update={"batch_id": BATCH_ID})

    values = call_builtins(context, now=NOW)

    assert values["agent_number"] == "+14155550100"
    assert values["user_number"] == "+14155550190"
    assert values["batch_id"] == str(BATCH_ID)
    assert values["lastUserMessage"] == ""


def test_web_calls_speak_from_the_from_number() -> None:
    values = call_builtins(_context("web"), now=NOW)

    assert values["agent_number"] == "+14155550190"


def test_unknown_timezone_falls_back_to_utc() -> None:
    context = _context(timezone="Mars/Olympus")

    assert call_zone(context).key == "UTC"
    assert call_builtins(context, now=NOW)["now_local"] == "2026-11-02T03:04:05+00:00"


def test_builtins_render_through_a_scope() -> None:
    context = _context(metadata={"campaign": "fall"})
    values = TemplateScope(builtins=call_builtins(context, now=NOW), timezone=context.config.timezone)

    assert (
        render_text("{{metadata.campaign}} {{user_number|phone_spoken}}", values)
        == "fall 4 1 5, 5 5 5, 0 1 0 0"
    )
