"""Nested Send Call objects shared by the request (specs/05 §3) and the resolved config (§4.4), plus the
constrained field types of the request."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from va_domain.languages import lookup_language
from va_domain.org import E164

__all__ = [
    "WEBHOOK_EVENTS",
    "AnalysisSchema",
    "CountryHint",
    "DialingStrategy",
    "DispositionName",
    "ExternalId",
    "GuardRail",
    "GuardRailAction",
    "JsonObject",
    "Keyword",
    "LanguageCode",
    "PostCallEvals",
    "PronunciationRule",
    "RequestData",
    "RetryRequest",
    "StartTime",
    "TransferList",
    "TransferTarget",
    "VoicemailRequest",
    "VoicemailSms",
    "WarmTransferConfig",
    "WebhookEvent",
    "parse_start_time",
]

_MAX_OBJECT_BYTES: Final = 64 * 1024
_MAX_TRANSFER_LIST: Final = 20
_SIP_URI: Final = re.compile(r"^sip:[^@\s]+@\S+$")
_E164: Final = re.compile(r"^\+[1-9][0-9]{1,14}$")
_OFFSET_FORM: Final = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+([+-]\d{2}:\d{2})$")

WebhookEvent = Literal[
    "queue",
    "call",
    "latency",
    "webhook",
    "tool",
    "dynamic_data",
    "citations",
    "evals",
    "post_transfer_transcript",
]
WEBHOOK_EVENTS: Final = frozenset(
    {
        "queue",
        "call",
        "latency",
        "webhook",
        "tool",
        "dynamic_data",
        "citations",
        "evals",
        "post_transfer_transcript",
    }
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _serialized_size(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())
    if size > _MAX_OBJECT_BYTES:
        msg = f"object is {size} bytes serialized; the limit is {_MAX_OBJECT_BYTES}"
        raise ValueError(msg)
    return value


def _language(value: str) -> str:
    if lookup_language(value) is None:
        msg = f"unsupported language {value!r}"
        raise ValueError(msg)
    return value


def _country(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _transfer_target(value: str) -> str:
    if _E164.match(value) or _SIP_URI.match(value):
        return value
    msg = "must be an E.164 number or a sip: URI"
    raise ValueError(msg)


def _transfer_list(value: dict[str, str]) -> dict[str, str]:
    if value and "default" not in value:
        msg = 'a non-empty transfer_list must contain a "default" entry'
        raise ValueError(msg)
    return value


def _unique_events(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        msg = "webhook_events must not repeat a value"
        raise ValueError(msg)
    return value


def parse_start_time(value: object) -> object:
    """`YYYY-MM-DD HH:MM:SS ±HH:MM`, `YYYY-MM-DD HH:MM:SS` (naive) or ISO 8601 -> `datetime` (maybe naive).

    A naive value is localized later with the resolved timezone (specs/05 §3.3).
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        msg = "start_time must be a string"
        raise ValueError(msg)  # noqa: TRY004 - pydantic turns ValueError into a validation error
    text = value.strip()
    offset = _OFFSET_FORM.match(text)
    if offset is not None:
        text = f"{offset.group(1)}{offset.group(2)}"
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        msg = "start_time must be 'YYYY-MM-DD HH:MM:SS ±HH:MM' or ISO 8601"
        raise ValueError(msg) from error


JsonObject = Annotated[dict[str, JsonValue], AfterValidator(_serialized_size)]
RequestData = Annotated[
    dict[Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")], JsonValue],
    AfterValidator(_serialized_size),
]
LanguageCode = Annotated[str, StringConstraints(min_length=1, max_length=32), AfterValidator(_language)]
CountryHint = Annotated[str, BeforeValidator(_country), StringConstraints(pattern=r"^[A-Z]{2}$")]
TransferTarget = Annotated[str, StringConstraints(max_length=256), AfterValidator(_transfer_target)]
TransferList = Annotated[
    dict[Annotated[str, StringConstraints(min_length=1, max_length=64)], E164],
    Field(max_length=_MAX_TRANSFER_LIST),
    AfterValidator(_transfer_list),
]
DispositionName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
AnalysisSchema = Annotated[
    dict[
        Annotated[str, StringConstraints(min_length=1, max_length=64)],
        Annotated[str, StringConstraints(min_length=1, max_length=500)],
    ],
    Field(max_length=50),
]
Keyword = Annotated[str, StringConstraints(pattern=r"^[^:\s][^:]{0,99}(:(10|[1-9]))?$")]
ExternalId = Annotated[str, StringConstraints(pattern=r"^[\x20-\x7E]{1,256}$")]
StartTime = Annotated[datetime, BeforeValidator(parse_start_time)]
WebhookEvents = Annotated[list[WebhookEvent], AfterValidator(_unique_events)]


class PronunciationRule(_Strict):
    word: str = Field(min_length=1, max_length=100)
    pronunciation: str = Field(min_length=1, max_length=200)
    case_sensitive: bool = False
    spaced: bool = False


class VoicemailSms(_Strict):
    message: str = Field(min_length=1, max_length=1600)
    from_: E164 | None = Field(default=None, validation_alias="from", serialization_alias="from")

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class DialingStrategy(_Strict):
    type: Literal["default", "local", "custom_pooling"] = "default"
    pool_id: UUID | None = None

    @model_validator(mode="after")
    def _pool(self) -> DialingStrategy:
        if self.type == "custom_pooling" and self.pool_id is None:
            msg = "custom_pooling requires pool_id"
            raise ValueError(msg)
        if self.type != "custom_pooling" and self.pool_id is not None:
            msg = "pool_id is only allowed with custom_pooling"
            raise ValueError(msg)
        return self


class WarmTransferConfig(_Strict):
    enabled: bool = False
    briefing_prompt: str | None = Field(default=None, max_length=2000)
    hold_audio: Literal["music", "silence", "agent"] = "music"
    timeout_s: int = Field(default=45, ge=10, le=120)


class GuardRailAction(_Strict):
    type: Literal["end_call", "transfer", "move_to_node", "none"]
    phone_number: E164 | None = None
    node_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _target(self) -> GuardRailAction:
        if self.type == "transfer" and self.phone_number is None:
            msg = "a transfer action requires phone_number"
            raise ValueError(msg)
        if self.type == "move_to_node" and self.node_id is None:
            msg = "a move_to_node action requires node_id"
            raise ValueError(msg)
        return self


class GuardRail(_Strict):
    type: Literal[
        "tcpa:ai_disclosure", "tcpa:self_introduction", "tcpa:recording_disclosure", "tcpa:opt_out", "custom"
    ]
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    action: GuardRailAction | None = None

    @model_validator(mode="after")
    def _custom(self) -> GuardRail:
        if self.type == "custom" and (self.name is None or self.description is None):
            msg = "a custom guard rail requires name and description"
            raise ValueError(msg)
        return self


class PostCallEvals(_Strict):
    workbench_setup_id: UUID | None = None
    workbench_setup_version_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> PostCallEvals:
        if (self.workbench_setup_id is None) == (self.workbench_setup_version_id is None):
            msg = "set exactly one of workbench_setup_id and workbench_setup_version_id"
            raise ValueError(msg)
        return self


class VoicemailRequest(_Strict):
    """The request `voicemail` object: only the sent keys override lower layers (see `explicit_fields`)."""

    message: str | None = Field(
        default=None, max_length=1000, description="Message to leave; supports {{vars}}."
    )
    action: Literal["hangup", "leave_message", "leave_message_and_sms", "ignore"] | None = None
    sms: VoicemailSms | None = None
    sensitive: bool = False

    @model_validator(mode="after")
    def _sms(self) -> VoicemailRequest:
        if self.action == "leave_message_and_sms" and self.sms is None:
            msg = "leave_message_and_sms requires sms"
            raise ValueError(msg)
        return self

    def sent(self) -> dict[str, Any]:
        """The keys present in the body, with `action="leave_message"` implied by `message`."""
        values: dict[str, Any] = {name: getattr(self, name) for name in self.model_fields_set}
        if self.message is not None and "action" not in values:
            values["action"] = "leave_message"
        return values


class RetryRequest(_Strict):
    """`wait` is optional here so its absence maps to `RETRY_NOT_PROVIDED` instead of `INVALID_INPUT`."""

    wait: int | None = Field(default=None, ge=60, le=86_400, description="Seconds before the retry call.")
    voicemail_action: Literal["hangup", "leave_message", "ignore"] = "hangup"
    voicemail_message: str | None = Field(default=None, max_length=1000)
    max_attempts: int = Field(default=1, ge=1, le=5)
    on_no_answer: bool = False
