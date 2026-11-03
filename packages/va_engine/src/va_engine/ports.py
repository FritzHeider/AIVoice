"""Side-effect ports of the pathway node handlers (specs/11 §4, Decision P07-T07).

The engine stays transport-agnostic: Webhook and Scheduling nodes send their fully rendered requests through
an :class:`HttpPort` (implemented by ``va_tools.http.HttpExecutor``, which owns the SSRF guard, retries and
JSONPath extraction), SMS nodes send through an :class:`SmsPort`, variable extraction runs through an
:class:`ExtractionPort`, and speech that must start before a handler finishes goes to an :class:`ActionSink`.
Every port method reports failures in its return value and never raises.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import JsonValue

if TYPE_CHECKING:
    from va_engine.extraction import ExtractionResult
    from va_engine.handlers.base import TurnContext
    from va_engine.models.results import Action

__all__ = [
    "FAILURE_KINDS",
    "ActionSink",
    "ExtractionPort",
    "HttpCall",
    "HttpMethod",
    "HttpOutcome",
    "HttpOutcomeKind",
    "HttpPort",
    "SmsOutcome",
    "SmsPort",
    "SmsRequest",
    "StatusClass",
]

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type HttpOutcomeKind = Literal["ok", "http_error", "timeout", "connection", "blocked", "invalid_url"]
type StatusClass = Literal["2xx", "3xx", "4xx", "5xx", "timeout"]

FAILURE_KINDS: Final[frozenset[str]] = frozenset({"timeout", "connection", "blocked", "invalid_url"})
"""Outcomes without an HTTP response; they all route like ``routeOnStatus.timeout``."""

_STATUS_CLASSES: Final[Mapping[int, StatusClass]] = {2: "2xx", 3: "3xx", 4: "4xx", 5: "5xx"}


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpCall:
    """A fully rendered request; ``extract`` lists ``(variable name, JSONPath)`` pairs applied to the body."""

    method: HttpMethod
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: JsonValue = None
    timeout_ms: int = 10_000
    retries: int = 0
    extract: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpOutcome:
    """What a request produced.

    ``kind`` ``ok`` = a 2xx response, ``http_error`` = any other response, the rest = no response. ``body`` is
    the parsed JSON or the text (``None`` without a response); ``extracted`` maps each requested name to its
    JSONPath value (``None`` = no match or no JSON body).
    """

    kind: HttpOutcomeKind
    status: int | None = None
    body: JsonValue = None
    truncated: bool = False
    extracted: Mapping[str, JsonValue] = field(default_factory=dict)
    attempts: int = 1
    duration_ms: int = 0

    @property
    def succeeded(self) -> bool:
        """Whether a 2xx response arrived."""
        return self.kind == "ok"

    def status_class(self) -> StatusClass | None:
        """The ``routeOnStatus`` key: ``2xx``…``5xx`` by status, ``timeout`` for every failure without a
        response, ``None`` for a status outside 200-599."""
        if self.kind in FAILURE_KINDS or self.status is None:
            return "timeout"
        return _STATUS_CLASSES.get(self.status // 100)


class HttpPort(Protocol):
    """Sends one rendered request (SSRF guard, retries, size cap, extraction); never raises."""

    async def execute(self, call: HttpCall) -> HttpOutcome: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SmsRequest:
    """One SMS from an SMS node: ``to`` is E.164, ``from_`` ``None`` = the call's agent (or org default)
    number."""

    to: str
    from_: str | None
    body: str
    node_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SmsOutcome:
    """Whether the SMS was accepted for sending; ``error`` is a short machine reason when not."""

    sent: bool
    message_id: str | None = None
    error: str | None = None


class SmsPort(Protocol):
    """Sends an SMS during a conversation (va_telnyx messaging in voice calls); never raises."""

    async def send(self, request: SmsRequest) -> SmsOutcome: ...


class ExtractionPort(Protocol):
    """Runs the node's ``extractVars`` extraction (specs/11 §5.3); ``None`` = nothing to extract."""

    async def extract(self, ctx: TurnContext) -> ExtractionResult | None: ...


class ActionSink(Protocol):
    """Takes actions that must start at once (speech while a request runs); non-blocking."""

    def queue(self, action: Action) -> None: ...
