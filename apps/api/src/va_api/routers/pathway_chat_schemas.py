"""Request and response bodies of the pathway test chat (specs/12-pathways-api.md §7; Decision P07-T10)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from va_api.services.pathway_chat import DEFAULT_SIMULATE_FROM, DEFAULT_SIMULATE_TO, ChatTurn
from va_common.json import dumps
from va_domain.phone import InvalidPhoneNumberError, normalize_e164

__all__ = [
    "MAX_MESSAGE_LENGTH",
    "MAX_REQUEST_DATA_BYTES",
    "ChatCreate",
    "ChatCreated",
    "ChatDeleted",
    "ChatMessage",
    "ChatReply",
    "chat_reply",
]

MAX_MESSAGE_LENGTH: Final = 4_000
MAX_REQUEST_DATA_BYTES: Final = 65_536

_PATHWAY_ID: Final = "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70"
_CHAT_ID: Final = "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4e01"
_ROUTE: Final[dict[str, JsonValue]] = {
    "type": "route",
    "from": "start",
    "to": "node_order",
    "label": "Wants to order",
    "alternatives": [{"label": "Hours", "score": 1}],
    "global": False,
}

PositiveVersion = Annotated[int, Field(ge=1)]


def _phone(value: str) -> str:
    try:
        return normalize_e164(value)
    except InvalidPhoneNumberError as exc:
        msg = "must be a valid phone number in E.164 format"
        raise ValueError(msg) from exc


class ChatCreate(BaseModel):
    """``POST /v1/pathway/chat/create``: the version to simulate and the simulated call."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "pathway_id": _PATHWAY_ID,
                    "version": "draft",
                    "request_data": {"first_name": "Mike"},
                    "start_node_id": None,
                    "simulate_from": "+14155550123",
                    "simulate_to": "+14155550100",
                }
            ]
        },
    )

    pathway_id: uuid.UUID
    version: PositiveVersion | Literal["draft", "production", "staging"] = Field(
        default="draft", description="Version number, `draft` (default), `production`, or `staging`."
    )
    request_data: dict[str, JsonValue] = Field(
        default_factory=dict, description="`{{request_data.*}}` values (at most 64 KiB of JSON)."
    )
    start_node_id: str | None = Field(default=None, description="Start here instead of the start node.")
    simulate_from: str = Field(default=DEFAULT_SIMULATE_FROM, description="The simulated caller (E.164).")
    simulate_to: str = Field(default=DEFAULT_SIMULATE_TO, description="The simulated agent number (E.164).")

    @field_validator("request_data")
    @classmethod
    def _check_size(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(dumps(value)) > MAX_REQUEST_DATA_BYTES:
            msg = "must be at most 64 KiB of JSON"
            raise ValueError(msg)
        return value

    @field_validator("simulate_from", "simulate_to")
    @classmethod
    def _check_phone(cls, value: str) -> str:
        return _phone(value)


class ChatMessage(BaseModel):
    """``POST /v1/pathway/chat/{chat_id}``: the caller's message, optionally on a fork of an earlier turn."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"message": "I'd like to order a pizza", "branch_from_turn": None}]},
    )

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    branch_from_turn: Annotated[int, Field(ge=0)] | None = Field(
        default=None, description="Fork the chat as it was after this turn (0 = the opening) into a new chat."
    )

    @field_validator("message")
    @classmethod
    def _check_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "must not be blank"
            raise ValueError(msg)
        return stripped


class _TurnFields(BaseModel):
    status: Literal["success"] = "success"
    chat_id: uuid.UUID
    assistant_messages: list[str]
    current_node_id: str
    variables: dict[str, JsonValue]
    actions: list[dict[str, JsonValue]] = Field(
        description="Simulated side effects: transfer, end_call, press_button, sms, wait, human_takeover."
    )
    ended: bool
    logs: list[dict[str, JsonValue]] = Field(
        description="route, extraction, loop_condition, webhook, sms and tool entries in order."
    )


_OPENING: Final[dict[str, Any]] = {
    "status": "success",
    "chat_id": _CHAT_ID,
    "assistant_messages": ["Hi! Thanks for calling Bella Pasta. How can I help?"],
    "current_node_id": "start",
    "variables": {},
    "actions": [],
    "ended": False,
    "logs": [],
}


class ChatCreated(_TurnFields):
    """The new chat and its opening (empty messages when the start node waits for the caller)."""

    model_config = ConfigDict(json_schema_extra={"examples": [_OPENING]})


class ChatReply(_TurnFields):
    """The answer to a message; ``chat_id`` is the new chat after branching."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    **_OPENING,
                    "assistant_messages": ["Great! What size?"],
                    "current_node_id": "node_order",
                    "variables": {"first_name": "Mike"},
                    "logs": [
                        _ROUTE,
                        {"type": "extraction", "node_id": "start", "variables": {"first_name": "Mike"}},
                    ],
                }
            ]
        }
    )


class ChatDeleted(BaseModel):
    """``DELETE /v1/pathway/chat/{chat_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "message": "Chat deleted."}]}
    )

    status: Literal["success"] = "success"
    message: str


def chat_reply[T: _TurnFields](model: type[T], turn: ChatTurn) -> T:
    """``turn`` as the response ``model``."""
    return model(
        chat_id=turn.chat_id,
        assistant_messages=turn.assistant_messages,
        current_node_id=turn.current_node_id,
        variables=turn.variables,
        actions=turn.actions,
        ended=turn.ended,
        logs=turn.logs,
    )
