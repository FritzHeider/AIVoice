"""`CallContext` shape, key, and TTL (specs/01 §2.1 step 2 Decision P05-T06a)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from resolved_configs import resolved_config

from va_domain.call_context import (
    CallContext,
    CallMemory,
    MemoryFact,
    MemoryMessage,
    MemoryOpenItem,
    call_context_key,
    call_context_ttl_s,
)
from va_domain.calls.config import SendCallConfig

AT = datetime(2026, 11, 1, 12, tzinfo=UTC)
MEMORY_ID = "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d73"


def _config(**over: Any) -> SendCallConfig:
    return SendCallConfig.model_validate({**resolved_config(), **over})


def _message(index: int) -> MemoryMessage:
    return MemoryMessage(role="user", channel="sms", text=f"message {index}", at=AT)


def _context(**over: Any) -> CallContext:
    values: dict[str, Any] = {
        "call_id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "direction": "outbound",
        "to_number": "+14155550100",
        "from_number": "+14155550190",
        "config": _config(),
        "created_at": AT,
        **over,
    }
    return CallContext.model_validate(values)


def test_the_key_uses_the_call_hash_tag() -> None:
    call_id = uuid.uuid4()

    assert call_context_key(call_id) == f"call:{{{call_id}}}:ctx"


@pytest.mark.parametrize(("minutes", "ttl"), [(30, 5400), (0.5, 3630), (5, 3900), (1.001, 3661)])
def test_ttl_is_the_maximum_duration_plus_an_hour(minutes: float, ttl: int) -> None:
    assert call_context_ttl_s(minutes) == ttl
    assert _context(config=_config(max_duration_min=minutes)).ttl_s() == ttl


def test_defaults_leave_the_producer_slots_empty() -> None:
    context = _context()

    assert context.version == 1
    assert (context.variables, context.context_sections, context.memory, context.trace_context) == (
        {},
        [],
        None,
        {},
    )
    assert (context.batch_id, context.contact_id, context.test_call) == (None, None, False)


def test_a_full_context_survives_a_json_round_trip() -> None:
    memory = CallMemory(
        memory_id=uuid.uuid4(),
        summary="Prefers mornings.",
        facts=[MemoryFact(key="name", value="Ana"), MemoryFact(key="kids", value=2)],
        open_items=[MemoryOpenItem(id="oi_1", text="Send the quote", opened_at=AT)],
        recent_messages=[_message(index) for index in range(10)],
    )
    context = _context(
        direction="inbound",
        batch_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        test_call=True,
        variables={"plan": {"tier": "gold"}, "count": 3},
        context_sections=["## Account\nGold"],
        turn_sections={2: ["Seats: 4."], 0: ["Their plan is gold.", "They owe 3."]},
        memory=memory,
        trace_context={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
    )

    restored = CallContext.model_validate_json(context.model_dump_json())

    assert restored == context
    assert restored.turn_sections == {2: ["Seats: 4."], 0: ["Their plan is gold.", "They owe 3."]}
    assert restored.turn_lines() == ["Their plan is gold.", "They owe 3.", "Seats: 4."]


def test_a_context_without_per_turn_data_has_no_turn_lines() -> None:
    context = _context()

    assert context.turn_sections == {}
    assert context.turn_lines() == []


def test_a_top_level_key_a_newer_writer_added_is_ignored() -> None:
    """A reader one build behind must load what a newer worker wrote, not rebuild it from the calls row."""
    context = _context(variables={"plan": "gold"}, context_sections=["Their plan is gold."])
    written = json.loads(context.model_dump_json())
    written["next_build_field"] = {"anything": [1, 2]}

    restored = CallContext.model_validate_json(json.dumps(written))

    assert restored == context
    assert "next_build_field" not in restored.model_dump()
    assert (restored.variables, restored.context_sections) == ({"plan": "gold"}, ["Their plan is gold."])


@pytest.mark.parametrize(
    "over",
    [
        {"version": 2},
        {"direction": "sideways"},
        {"created_at": datetime(2026, 11, 1, 12)},  # noqa: DTZ001 - a naive datetime is the invalid input
        {"config": {**resolved_config(), "unexpected": 1}},
        {"memory": {"memory_id": MEMORY_ID, "unexpected": 1}},
        {"memory": {"memory_id": MEMORY_ID, "facts": [{"key": "a", "value": 1, "unexpected": 1}]}},
    ],
    ids=["version", "direction", "naive-time", "config-extra-key", "memory-extra-key", "fact-extra-key"],
)
def test_invalid_contexts_are_rejected(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _context(**over)


def test_memory_keeps_at_most_ten_recent_messages() -> None:
    with pytest.raises(ValidationError, match="recent_messages"):
        CallMemory(memory_id=uuid.uuid4(), recent_messages=[_message(index) for index in range(11)])


def test_a_context_is_frozen() -> None:
    context = _context()

    with pytest.raises(ValidationError):
        context.to_number = "+14155550111"
