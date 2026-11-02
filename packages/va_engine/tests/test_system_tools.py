"""Built-in system tools: availability, schemas, arguments (specs/10 §7, specs/13 §4/§6 Decision P05-T09b)."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from jsonschema import Draft202012Validator
from resolved_configs import resolved_config

from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.system_tools import (
    RESERVED_TOOL_NAMES,
    EndCallArgs,
    InvalidToolCall,
    LookupKnowledgeArgs,
    PressButtonArgs,
    ScheduleCallbackArgs,
    SendSmsArgs,
    SystemToolFacts,
    TransferCallArgs,
    facts_for_call,
    is_reserved,
    parse_system_call,
    resolve_transfer,
    system_tools,
)
from va_llm.models import ChatMessage, ToolCall

SPEC_13 = Path(__file__).resolve().parents[3] / "specs" / "13-tools-and-dynamic-data.md"
TASK = SystemToolFacts(runner="task")
ALL = SystemToolFacts(
    runner="task",
    transfer_phone_number="+14155550123",
    transfer_list={"sales": "+14155550111", "billing": "+14155550122"},
    sms_capable=True,
    allow_schedule_callback=True,
    has_knowledge_bases=True,
)


def names(facts: SystemToolFacts) -> list[str]:
    return [tool.name for tool in system_tools(facts)]


def schema(facts: SystemToolFacts, name: str) -> dict[str, Any]:
    return next(tool.parameters for tool in system_tools(facts) if tool.name == name)


def call(name: str, arguments: object | str) -> ToolCall:
    text = arguments if isinstance(arguments, str) else orjson.dumps(arguments).decode()
    return ToolCall(id="call-1", name=name, arguments_json=text)


def test_reserved_names_match_the_spec() -> None:
    text = SPEC_13.read_text(encoding="utf-8")
    sentence = re.search(r"system tool names are reserved\s*\((.*?)\)", text, flags=re.DOTALL)
    assert sentence is not None
    assert set(re.findall(r"`(\w+)`", sentence.group(1))) == RESERVED_TOOL_NAMES
    assert is_reserved("book_slot")
    assert not is_reserved("lookup_order")


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (TASK, ["end_call", "press_button"]),
        (
            ALL,
            [
                "end_call",
                "transfer_call",
                "press_button",
                "send_sms",
                "schedule_callback",
                "lookup_knowledge",
            ],
        ),
        (
            dataclasses.replace(ALL, runner="pathway"),
            ["end_call", "press_button", "send_sms", "schedule_callback", "lookup_knowledge"],
        ),
        (dataclasses.replace(TASK, runner="pathway", allow_end_call_tool=False), ["press_button"]),
        (dataclasses.replace(TASK, allow_end_call_tool=False), ["end_call", "press_button"]),
        (
            dataclasses.replace(ALL, channel="web_voice"),
            ["end_call", "send_sms", "schedule_callback", "lookup_knowledge"],
        ),
        (
            dataclasses.replace(TASK, transfer_phone_number="+14155550123"),
            ["end_call", "transfer_call", "press_button"],
        ),
        (dataclasses.replace(TASK, sms_capable=True, allow_send_sms=False), ["end_call", "press_button"]),
        (dataclasses.replace(TASK, sms_capable=False, allow_send_sms=True), ["end_call", "press_button"]),
    ],
)
def test_availability(facts: SystemToolFacts, expected: list[str]) -> None:
    assert names(facts) == expected


def test_every_schema_is_valid_draft_2020_12() -> None:
    for facts in (
        ALL,
        dataclasses.replace(ALL, transfer_list={}),
        dataclasses.replace(ALL, transfer_phone_number=None),
    ):
        for tool in system_tools(facts):
            Draft202012Validator.check_schema(tool.parameters)
            assert tool.description


def test_fixed_schemas() -> None:
    assert schema(ALL, "end_call") == {
        "type": "object",
        "properties": {"reason": {"type": "string"}, "final_message": {"type": "string"}},
        "required": ["reason"],
    }
    assert schema(ALL, "press_button") == {
        "type": "object",
        "properties": {"digits": {"type": "string", "pattern": "^[0-9*#ABCDwW]{1,32}$"}},
        "required": ["digits"],
    }
    assert schema(ALL, "send_sms") == {
        "type": "object",
        "properties": {"message": {"type": "string", "maxLength": 1600}},
        "required": ["message"],
    }
    assert schema(ALL, "schedule_callback") == {
        "type": "object",
        "properties": {"datetime_iso": {"type": "string"}, "note": {"type": "string"}},
        "required": ["datetime_iso"],
    }
    assert schema(ALL, "lookup_knowledge") == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }


MESSAGE = {"type": "string", "description": "Short line to say before transferring"}


def test_transfer_schema_with_list_and_number() -> None:
    assert schema(ALL, "transfer_call") == {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": ["billing", "sales", "default"]},
            "message": MESSAGE,
        },
        "required": ["label"],
    }


def test_transfer_schema_with_list_only() -> None:
    facts = dataclasses.replace(ALL, transfer_phone_number=None)
    assert schema(facts, "transfer_call")["properties"]["label"]["enum"] == ["billing", "sales"]


def test_transfer_schema_with_number_only() -> None:
    facts = dataclasses.replace(ALL, transfer_list={})
    assert schema(facts, "transfer_call") == {
        "type": "object",
        "properties": {"message": MESSAGE},
        "required": [],
    }


def test_list_key_named_default_is_not_duplicated() -> None:
    facts = dataclasses.replace(ALL, transfer_list={"default": "+14155550199"})
    assert schema(facts, "transfer_call")["properties"]["label"]["enum"] == ["default"]
    assert resolve_transfer(TransferCallArgs(label="default"), facts) == "+14155550199"


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("end_call", {"reason": "done"}, EndCallArgs(reason="done")),
        ("end_call", {"reason": "done", "final_message": "Bye!"}, EndCallArgs("done", "Bye!")),
        (
            "transfer_call",
            {"label": "sales", "message": "Connecting you."},
            TransferCallArgs("sales", "Connecting you."),
        ),
        ("press_button", {"digits": "1w2#"}, PressButtonArgs("1w2#")),
        ("send_sms", {"message": "Your code is 1234."}, SendSmsArgs("Your code is 1234.")),
        ("schedule_callback", {"datetime_iso": "2026-11-03T10:00"}, ScheduleCallbackArgs("2026-11-03T10:00")),
        ("schedule_callback", {"datetime_iso": "x", "note": "n"}, ScheduleCallbackArgs("x", "n")),
        ("lookup_knowledge", {"query": "hours"}, LookupKnowledgeArgs("hours")),
    ],
)
def test_valid_calls_parse_to_typed_arguments(name: str, arguments: dict[str, Any], expected: object) -> None:
    assert parse_system_call(call(name, arguments), ALL) == expected


def test_empty_arguments_count_as_an_empty_object() -> None:
    facts = dataclasses.replace(ALL, transfer_list={})
    assert parse_system_call(call("transfer_call", ""), facts) == TransferCallArgs()


@pytest.mark.parametrize(
    ("name", "arguments", "detail"),
    [
        (
            "press_button",
            {"digits": "12x"},
            [{"path": "digits", "message": "'12x' does not match '^[0-9*#ABCDwW]{1,32}$'"}],
        ),
        ("end_call", {}, [{"path": "", "message": "'reason' is a required property"}]),
        (
            "transfer_call",
            {"label": "support", "message": 3},
            [
                {"path": "label", "message": "'support' is not one of ['billing', 'sales', 'default']"},
                {"path": "message", "message": "3 is not of type 'string'"},
            ],
        ),
        ("end_call", "[1]", [{"path": "", "message": "[1] is not of type 'object'"}]),
        ("end_call", "{not json", [{"path": "", "message": "arguments must be a JSON object"}]),
    ],
)
def test_invalid_arguments(name: str, arguments: object, detail: list[dict[str, str]]) -> None:
    result = parse_system_call(call(name, arguments), ALL)

    assert result == InvalidToolCall(name, detail)
    assert isinstance(result, InvalidToolCall)
    message = result.tool_message("call-1")
    assert message.role == "tool"
    assert message.tool_call_id == "call-1"
    assert orjson.loads(message.content or "") == {"error": "invalid_arguments", "detail": detail}


@pytest.mark.parametrize("name", ["transfer_call", "book_slot", "list_slots", "send_sms"])
def test_reserved_but_unavailable_names_are_unknown(name: str) -> None:
    result = parse_system_call(call(name, {}), TASK)

    assert result == InvalidToolCall(name, "unknown_tool")
    assert isinstance(result, InvalidToolCall)
    assert result.tool_message("c9") == ChatMessage(
        role="tool", content='{"error":"invalid_arguments","detail":"unknown_tool"}', tool_call_id="c9"
    )


def test_custom_tool_names_are_not_system_calls() -> None:
    assert parse_system_call(call("lookup_order", {"id": 1}), ALL) is None


@pytest.mark.parametrize(
    ("label", "number"),
    [("sales", "+14155550111"), ("default", "+14155550123"), (None, "+14155550123"), ("support", None)],
)
def test_resolve_transfer(label: str | None, number: str | None) -> None:
    assert resolve_transfer(TransferCallArgs(label=label), ALL) == number


def _context(**config: Any) -> CallContext:
    return CallContext.model_validate(
        {
            "call_id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "direction": "outbound",
            "to_number": "+14155550100",
            "from_number": "+14155550190",
            "config": {**resolved_config(), **config},
            "created_at": datetime(2026, 11, 2, tzinfo=UTC),
        }
    )


def test_facts_for_a_task_call() -> None:
    kb = uuid.uuid4()
    context = _context(
        transfer_phone_number="+14155550123",
        transfer_list={"sales": "+14155550111"},
        knowledge_base_ids=[str(kb)],
    )

    facts = facts_for_call(context, OrgSettings(), sms_capable=True)

    assert facts == SystemToolFacts(
        runner="task",
        transfer_phone_number="+14155550123",
        transfer_list={"sales": "+14155550111"},
        sms_capable=True,
        allow_send_sms=True,
        allow_schedule_callback=False,
        has_knowledge_bases=True,
    )


def test_facts_for_a_pathway_call_with_callbacks() -> None:
    context = _context(runner_type="pathway", task=None, pathway_id=str(uuid.uuid4()), pathway_version=1)
    settings = OrgSettings.model_validate(
        {"engine": {"allow_callbacks": True}, "tools": {"allow_send_sms": False}}
    )

    facts = facts_for_call(
        context, settings, sms_capable=False, channel="web_chat", allow_end_call_tool=False
    )

    assert facts.runner == "pathway"
    assert facts.channel == "web_chat"
    assert facts.allow_schedule_callback
    assert not facts.allow_send_sms
    assert not facts.allow_end_call_tool
    assert names(facts) == ["schedule_callback"]
