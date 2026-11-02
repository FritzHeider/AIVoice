"""`va_domain.control`: every specs/07 §17 example parses to its model; invalid messages are rejected."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from va_domain.control import (
    CONTROL_ACK_TTL_S,
    CONTROL_TYPES,
    ControlMessageError,
    CreditsExhaustedControl,
    DrainNoticeControl,
    InjectContextControl,
    SayControl,
    StopControl,
    TakeoverControl,
    TransferControl,
    control_ack_key,
    control_channel,
    parse_control,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "07-media-pipeline.md"
MESSAGE_ID = uuid.UUID("0190a5f2-7c1e-7b3a-9c4d-123456789abc")
CALL_ID = uuid.UUID("0190a5f2-0000-7000-8000-000000000001")

MODELS: dict[str, type[Any]] = {
    "stop": StopControl,
    "say": SayControl,
    "inject_context": InjectContextControl,
    "transfer": TransferControl,
    "takeover": TakeoverControl,
    "credits_exhausted": CreditsExhaustedControl,
    "drain_notice": DrainNoticeControl,
}


def _spec_examples() -> list[dict[str, Any]]:
    """The JSON lines of the specs/07 §17 block, with ``<uuid>`` replaced by a real id."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## 17. Control channel messages", 1)[1]
    block = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert block is not None
    lines = [line for line in block.group(1).splitlines() if line.strip()]
    return [json.loads(line.replace("<uuid>", str(MESSAGE_ID))) for line in lines]


SPEC_EXAMPLES = _spec_examples()


def test_spec_lists_every_type() -> None:
    assert tuple(example["type"] for example in SPEC_EXAMPLES) == CONTROL_TYPES
    assert set(MODELS) == set(CONTROL_TYPES)


@pytest.mark.parametrize("example", SPEC_EXAMPLES, ids=[e["type"] for e in SPEC_EXAMPLES])
def test_spec_example_parses_to_its_model(example: dict[str, Any]) -> None:
    message = parse_control(json.dumps(example))
    assert isinstance(message, MODELS[example["type"]])
    assert message.id == MESSAGE_ID
    assert message.model_dump(mode="json") == {**message.model_dump(mode="json"), **example}
    assert parse_control(message.model_dump_json()) == message


def test_defaults() -> None:
    stop = parse_control(json.dumps({"id": str(MESSAGE_ID), "type": "stop"}))
    assert isinstance(stop, StopControl)
    assert stop.reason == "api"
    transfer = parse_control(
        json.dumps({"id": str(MESSAGE_ID), "type": "transfer", "phone_number": "+14155550100"})
    )
    assert isinstance(transfer, TransferControl)
    assert transfer.mode == "cold"


def test_bytes_and_text_are_stripped() -> None:
    raw = json.dumps({"id": str(MESSAGE_ID), "type": "say", "text": "  hello  "}).encode()
    message = parse_control(raw)
    assert isinstance(message, SayControl)
    assert message.text == "hello"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"id": str(MESSAGE_ID), "type": "dance"}),
        json.dumps({"type": "stop"}),
        json.dumps({"id": "not-a-uuid", "type": "stop"}),
        json.dumps({"id": str(MESSAGE_ID), "type": "stop", "extra": 1}),
        json.dumps({"id": str(MESSAGE_ID), "type": "say", "text": "   "}),
        json.dumps({"id": str(MESSAGE_ID), "type": "inject_context"}),
        json.dumps({"id": str(MESSAGE_ID), "type": "transfer", "phone_number": "+1", "mode": "hot"}),
        json.dumps({"id": str(MESSAGE_ID), "type": "takeover"}),
    ],
)
def test_invalid_messages_are_rejected(raw: str) -> None:
    with pytest.raises(ControlMessageError) as caught:
        parse_control(raw)
    assert caught.value.errors >= 1
    assert raw not in str(caught.value)


def test_messages_are_frozen() -> None:
    message = StopControl(id=MESSAGE_ID)
    with pytest.raises(ValueError, match="frozen"):
        message.reason = "other"


def test_keys_use_the_call_hash_tag() -> None:
    assert control_channel(CALL_ID) == f"call:{{{CALL_ID}}}:control"
    assert control_ack_key(CALL_ID, MESSAGE_ID) == f"call:{{{CALL_ID}}}:control_ack:{MESSAGE_ID}"
    assert CONTROL_ACK_TTL_S == 30
