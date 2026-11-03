"""What one pathway test-chat turn produced (specs/12 §7; Decision P07-T10 under specs/12 §7).

A :class:`ChatRecorder` sits on every side of the engine for one request: it is the
:class:`~va_engine.engine_events.EngineEvents` sink (decision and node events become ``logs`` items), it
wraps the real :class:`~va_engine.ports.HttpPort` (each request adds a ``tool`` item with masked headers and
body excerpts), it is the simulated :class:`~va_engine.ports.SmsPort` (nothing is sent; an ``sms`` action
is recorded), and :meth:`ChatRecorder.consume` turns the engine's actions into ``assistant_messages`` and
``actions``. Transfers, end call, press button and human takeover are never executed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from typing import Final

from pydantic import JsonValue

from va_common.json import dumps_str
from va_common.masking import is_secret_key
from va_engine.engine_events import CONDITION_EVENT, EXTRACTION_EVENT, ROUTE_EVENT
from va_engine.handlers.reports import redacted_url
from va_engine.models.results import (
    Action,
    EndCallAction,
    HumanTakeover,
    Say,
    SayStream,
    SendDtmfAction,
    SwitchGraph,
    TransferAction,
    Wait,
    is_terminal,
)
from va_engine.ports import HttpCall, HttpOutcome, HttpPort, SmsOutcome, SmsRequest

__all__ = [
    "EXCERPT_LIMIT",
    "MASK",
    "SIMULATED_MESSAGE_PREFIX",
    "ChatRecorder",
    "Item",
    "action_item",
    "excerpt",
    "log_item",
    "masked_headers",
]

EXCERPT_LIMIT: Final = 2_000
MASK: Final = "***"
SIMULATED_MESSAGE_PREFIX: Final = "sim-"
_ELLIPSIS: Final = "…"
_WEBHOOK_EVENT: Final = "call.webhook_node"
_SMS_EVENT: Final = "call.sms_sent"

type Item = dict[str, JsonValue]


def excerpt(value: JsonValue, limit: int = EXCERPT_LIMIT) -> str | None:
    """``value`` as text (JSON unless it is a string), at most ``limit`` characters (``…`` marks a cut)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else dumps_str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS


def masked_headers(headers: Mapping[str, str]) -> dict[str, JsonValue]:
    """``headers`` with every secret-named value (``Authorization``, ``X-Api-Key``, …) replaced by ``***``."""
    return {name: MASK if is_secret_key(name) else value for name, value in headers.items()}


def _route_item(data: Mapping[str, JsonValue]) -> Item:
    return {
        "type": "route",
        "from": data.get("from_node"),
        "to": data.get("to_node"),
        "label": data.get("label"),
        "alternatives": data.get("alternatives", []),
        "global": data.get("global", False),
    }


def _webhook_item(data: Mapping[str, JsonValue]) -> Item:
    return {
        "type": "webhook",
        "node_id": data.get("node_id"),
        "node_name": data.get("node_name"),
        "method": data.get("method"),
        "url": data.get("url"),
        "status": data.get("status_code"),
        "ms": data.get("duration_ms"),
        "outcome": data.get("outcome"),
    }


def log_item(event_type: str, data: Mapping[str, JsonValue]) -> Item | None:
    """The ``logs`` item of an engine event; ``None`` for event types the chat does not show."""
    if event_type == ROUTE_EVENT:
        return _route_item(data)
    if event_type == EXTRACTION_EVENT:
        return {"type": "extraction", "node_id": data.get("node_id"), "variables": data.get("variables", {})}
    if event_type == CONDITION_EVENT:
        return {
            "type": "loop_condition",
            "node_id": data.get("node_id"),
            "score": data.get("score"),
            "met": data.get("met"),
        }
    if event_type == _WEBHOOK_EVENT:
        return _webhook_item(data)
    if event_type == _SMS_EVENT:
        return {"type": "sms", "node_id": data.get("node_id"), "sent": data.get("sent")}
    return None


def action_item(action: Action) -> Item | None:  # noqa: PLR0911 - one return per simulated action type
    """The ``actions`` item of a non-speech action (``None`` for speech)."""
    if isinstance(action, Wait):
        return {"type": "wait", "seconds": action.seconds}
    if isinstance(action, TransferAction):
        return {
            "type": "transfer",
            "mode": action.mode,
            "target": action.target,
            "caller_id_mode": action.caller_id_mode,
        }
    if isinstance(action, EndCallAction):
        return {"type": "end_call", "reason": action.reason, "ended_by": action.ended_by}
    if isinstance(action, HumanTakeover):
        return {"type": "human_takeover"}
    if isinstance(action, SendDtmfAction):
        return {"type": "press_button", "digits": action.digits, "duration_ms": action.duration_ms}
    if isinstance(action, SwitchGraph):
        return {
            "type": "switch_pathway",
            "pathway_id": action.pathway_id,
            "version": action.version,
            "node_id": action.node_id,
        }
    return None


def _tool_item(call: HttpCall, outcome: HttpOutcome) -> Item:
    return {
        "type": "tool",
        "method": call.method,
        "url": redacted_url(call.url),
        "request_headers": masked_headers(call.headers),
        "request_body": excerpt(call.body),
        "status": outcome.status,
        "response_body": excerpt(outcome.body),
        "ms": outcome.duration_ms,
        "outcome": outcome.kind,
    }


@dataclass(slots=True)
class ChatRecorder:
    """Collects one request's messages, actions and logs; ``http`` = the real executor."""

    http: HttpPort
    messages: list[str] = field(default_factory=list)
    actions: list[Item] = field(default_factory=list)
    logs: list[Item] = field(default_factory=list)
    terminal: bool = False
    _sms_sent: int = 0

    def emit(self, event_type: str, data: dict[str, JsonValue]) -> None:
        item = log_item(event_type, data)
        if item is not None:
            self.logs.append(item)

    async def execute(self, call: HttpCall) -> HttpOutcome:
        """The real request (the executor never raises), recorded as a ``tool`` log item."""
        outcome = await self.http.execute(call)
        self.logs.append(_tool_item(call, outcome))
        return outcome

    async def send(self, request: SmsRequest) -> SmsOutcome:
        """A simulated SMS: recorded as an ``sms`` action, reported as sent."""
        self._sms_sent += 1
        message_id = f"{SIMULATED_MESSAGE_PREFIX}{self._sms_sent}"
        self.actions.append(
            {
                "type": "sms",
                "node_id": request.node_id,
                "to": request.to,
                "from": request.from_,
                "body": request.body,
                "message_id": message_id,
            }
        )
        return SmsOutcome(sent=True, message_id=message_id)

    async def consume(self, actions: AsyncGenerator[Action]) -> None:
        """Run ``actions`` to the end (every stream read before the generator resumes)."""
        async with contextlib.aclosing(actions) as running:
            async for action in running:
                await self._take(action)

    async def _take(self, action: Action) -> None:
        if isinstance(action, SayStream):
            text = "".join([delta async for delta in action.tokens])
            self._say(text)
            return
        if isinstance(action, Say):
            self._say(action.text)
            return
        item = action_item(action)
        if item is not None:
            self.actions.append(item)
        if is_terminal(action):
            self.terminal = True

    def _say(self, text: str) -> None:
        spoken = text.strip()
        if spoken:
            self.messages.append(spoken)
