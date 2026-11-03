"""What a node handler reports for the turn's events (specs/19 §2; Decision P07-T07 under specs/11 §4).

The turn orchestration emits one event per report, in order: :class:`ExtractionReport` → ``call.extraction``,
:class:`WebhookReport` → ``call.webhook_node``, :class:`SmsReport` → ``call.sms_sent``. Reports never carry
request/response bodies, headers, URL query strings or phone numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import JsonValue

if TYPE_CHECKING:
    from va_engine.extraction import ExtractionResult
    from va_engine.ports import HttpOutcomeKind

__all__ = ["ExtractionReport", "NodeReport", "SmsReport", "WebhookPurpose", "WebhookReport", "redacted_url"]

type WebhookPurpose = Literal["webhook", "availability", "booking"]

_NO_URL: Final = ""


def redacted_url(url: str) -> str:
    """``url`` without user info, query and fragment (``""`` when it cannot be parsed)."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return _NO_URL
    if not parts.scheme or not host:
        return _NO_URL
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookReport:
    """One HTTP request a node sent (Webhook node, Scheduling availability / booking)."""

    event_type: Literal["call.webhook_node"] = "call.webhook_node"
    node_id: str
    purpose: WebhookPurpose = "webhook"
    method: str
    url: str
    outcome: HttpOutcomeKind
    status_code: int | None
    attempts: int
    duration_ms: int
    variables_set: tuple[str, ...] = ()
    route_to: str | None = None

    def event_data(self) -> dict[str, JsonValue]:
        return {
            "node_id": self.node_id,
            "purpose": self.purpose,
            "method": self.method,
            "url": redacted_url(self.url),
            "outcome": self.outcome,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "variables_set": list(self.variables_set),
            "route_to": self.route_to,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SmsReport:
    """The SMS node's send result."""

    event_type: Literal["call.sms_sent"] = "call.sms_sent"
    node_id: str
    sent: bool
    message_id: str | None = None
    error: str | None = None

    def event_data(self) -> dict[str, JsonValue]:
        return {
            "node_id": self.node_id,
            "source": "sms_node",
            "sent": self.sent,
            "message_id": self.message_id,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionReport:
    """Variables a node extracted before its request (Webhook ``extractVars``)."""

    event_type: Literal["call.extraction"] = "call.extraction"
    node_id: str
    result: ExtractionResult

    def event_data(self) -> dict[str, JsonValue]:
        return {"node_id": self.node_id, **self.result.event_data()}


type NodeReport = WebhookReport | SmsReport | ExtractionReport
