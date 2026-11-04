"""What one text run of a pathway produced (specs/12 §7 Decision P07-T10; specs/12 §8 Decision P07-T11a).

A :class:`TextRecorder` sits on every side of the engine for one test-chat request or unit-test case: it is
the :class:`~va_engine.engine_events.EngineEvents` sink (decision and node events become ``logs`` items), it
wraps the real :class:`~va_engine.ports.HttpPort` (each request adds a ``tool`` item with masked headers and
body excerpts), it is the simulated :class:`~va_engine.ports.SmsPort` (nothing is sent; an ``sms`` action
is recorded), and :meth:`TextRecorder.consume` turns the engine's actions into ``assistant_messages`` and
``actions``. Transfers, end call, press button and human takeover are never executed. A unit test's
``mock_tools`` (:class:`MockResponse` by :attr:`~va_engine.ports.HttpCall.source_id`) answer matching requests
instead of the executor; their ``tool`` items carry ``mocked: true``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
from va_engine.ports import HttpCall, HttpOutcome, HttpOutcomeKind, HttpPort, SmsOutcome, SmsRequest
from va_tools.jsonpath import extract_all

__all__ = [
    "EXCERPT_LIMIT",
    "MASK",
    "SIMULATED_MESSAGE_PREFIX",
    "Item",
    "MockResponse",
    "TextRecorder",
    "action_item",
    "excerpt",
    "log_item",
    "masked_headers",
    "mocked_outcome",
]

EXCERPT_LIMIT: Final = 2_000
MASK: Final = "***"
SIMULATED_MESSAGE_PREFIX: Final = "sim-"
_ELLIPSIS: Final = "…"
_WEBHOOK_EVENT: Final = "call.webhook_node"
_SMS_EVENT: Final = "call.sms_sent"
_OK_STATUS_CLASS: Final = 2

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


class MockResponse(BaseModel):
    """A unit test's canned answer for one request source (``expected.mock_tools`` value)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: int = Field(default=200, ge=100, le=599)
    body: JsonValue = None


def mocked_outcome(call: HttpCall, mock: MockResponse) -> HttpOutcome:
    """The outcome ``mock`` gives ``call``: 2xx is ``ok``, else ``http_error``; ``extract`` reads the body."""
    kind: HttpOutcomeKind = "ok" if mock.status // 100 == _OK_STATUS_CLASS else "http_error"
    extracted = extract_all(mock.body, call.extract) if mock.body is not None else {}
    return HttpOutcome(kind=kind, status=mock.status, body=mock.body, extracted=extracted)


def _tool_item(call: HttpCall, outcome: HttpOutcome, *, mocked: bool) -> Item:
    item: Item = {
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
    if mocked:
        item["mocked"] = True
    return item


@dataclass(slots=True)
class TextRecorder:
    """Collects one run's messages, actions and logs; ``http`` = the real executor, ``mocks`` = canned
    answers by request source."""

    http: HttpPort
    mocks: Mapping[str, MockResponse] = field(default_factory=dict)
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
        """The mocked answer of the call's source, else the real request (the executor never raises),
        recorded as a ``tool`` log item."""
        mock = None if call.source_id is None else self.mocks.get(call.source_id)
        if mock is not None:
            outcome = mocked_outcome(call, mock)
        else:
            outcome = await self.http.execute(call)
        self.logs.append(_tool_item(call, outcome, mocked=mock is not None))
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
