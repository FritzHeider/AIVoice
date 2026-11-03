"""Scheduling node handler (specs/11 §4 "Scheduling" row, §3.2.7; Decision P07-T07 under specs/11 §4).

``on_enter`` queues the rendered ``speech`` (voice channels), then GETs the rendered ``availabilityUrl``
(rendered headers, 10 s timeout, one retry). The ``{"slots": [{"start", "end"}]}`` answer is filtered to slots
starting within ``[now, now + lookaheadDays)`` (naive times are in the node's rendered ``timezone``, else the
call's), sorted, capped at :data:`MAX_SLOTS`, and stored in ``variables["available_slots"]`` as
``[{"start", "end"}]`` ISO 8601 strings in that timezone (a missing/invalid ``end`` = start +
``slotDurationMinutes``). A failed request, an invalid body or no slot routes to ``onFailureNodeId`` (problem
``scheduling_unavailable`` when it is unset or unknown).

``respond`` streams the dialogue reply; the dialogue port offers :data:`SCHEDULING_FUNCTIONS`. Their calls run
through :func:`list_slots` and :func:`book_slot`: a booking must name an offered slot start (compared as an
instant), POSTs ``{start, end, name, phone, email, notes, call_id}`` to ``bookingUrl`` and stores the
``booking_id``, ``booking_start`` and ``booking_end`` variables; a failed booking routes to
``onFailureNodeId``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from typing import Final

from pydantic import JsonValue

from va_common.logging import get_logger
from va_engine.handlers.base import HandlerResult, TurnContext, speak_node
from va_engine.handlers.reports import NodeReport, WebhookReport
from va_engine.handlers.webhook import HTTP_UNAVAILABLE, queue_speech, route_target, send_request
from va_engine.models.nodes import PathwayNode, SchedulingConfig
from va_engine.models.results import Action
from va_engine.ports import HttpCall, HttpOutcome
from va_engine.templating import zone_named

__all__ = [
    "AVAILABLE_SLOTS",
    "MAX_SLOTS",
    "REQUEST_TIMEOUT_MS",
    "SCHEDULING_FUNCTIONS",
    "SCHEDULING_UNAVAILABLE",
    "BookingOutcome",
    "SchedulingHandler",
    "Slot",
    "book_slot",
    "list_slots",
    "parse_slots",
]

AVAILABLE_SLOTS: Final = "available_slots"
MAX_SLOTS: Final = 20
REQUEST_TIMEOUT_MS: Final = 10_000
AVAILABILITY_RETRIES: Final = 1
SCHEDULING_UNAVAILABLE: Final = "scheduling_unavailable"

SCHEDULING_FUNCTIONS: Final[tuple[dict[str, JsonValue], ...]] = (
    {
        "type": "function",
        "function": {
            "name": "list_slots",
            "description": "List the appointment slots that are available to book.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_slot",
            "description": "Book the appointment slot that starts at `start` once the caller agreed to it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "The slot start exactly as listed by list_slots (ISO 8601).",
                    }
                },
                "required": ["start"],
                "additionalProperties": False,
            },
        },
    },
)

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Slot:
    """An available appointment slot (timezone-aware)."""

    start: datetime
    end: datetime

    def as_json(self) -> dict[str, JsonValue]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True, kw_only=True)
class BookingOutcome:
    """A ``book_slot`` call's result: the tool message for the model and where to go after a failure."""

    ok: bool
    tool_message: dict[str, JsonValue]
    route_to: str | None = None
    report: WebhookReport | None = None


def _config(ctx: TurnContext) -> SchedulingConfig:
    return ctx.node.data.scheduling or SchedulingConfig()


def _zone(ctx: TurnContext, config: SchedulingConfig) -> tzinfo:
    name = ctx.render(config.timezone, field_name="scheduling.timezone").strip() if config.timezone else ""
    return zone_named(name or ctx.timezone)


def _moment(value: JsonValue, zone: tzinfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=zone)


def _slot(raw: JsonValue, zone: tzinfo, duration: timedelta) -> Slot | None:
    if not isinstance(raw, dict):
        return None
    start = _moment(raw.get("start"), zone)
    if start is None:
        return None
    end = _moment(raw.get("end"), zone)
    if end is None or end <= start:
        end = start + duration
    return Slot(start.astimezone(zone), end.astimezone(zone))


def parse_slots(
    body: JsonValue, *, now: datetime, zone: tzinfo, lookahead_days: int, slot_minutes: int
) -> list[Slot] | None:
    """The bookable slots of an availability body; ``None`` when the body breaks the contract."""
    raw_slots = body.get("slots") if isinstance(body, dict) else None
    if not isinstance(raw_slots, list):
        return None
    horizon = now + timedelta(days=lookahead_days)
    duration = timedelta(minutes=slot_minutes)
    slots = [
        slot
        for raw in raw_slots
        if (slot := _slot(raw, zone, duration)) is not None and now <= slot.start < horizon
    ]
    slots.sort(key=lambda slot: slot.start)
    return slots[:MAX_SLOTS]


def _headers(ctx: TurnContext, config: SchedulingConfig) -> dict[str, str]:
    return {name: ctx.render(value, field_name=f"headers.{name}") for name, value in config.headers.items()}


def _failure(
    ctx: TurnContext, config: SchedulingConfig, actions: tuple[Action, ...], reports: tuple[NodeReport, ...]
) -> HandlerResult:
    ctx.state.variables[AVAILABLE_SLOTS] = []
    route_to = route_target(ctx, config.on_failure_node_id, source="onFailureNodeId")
    problem = None if route_to is not None else SCHEDULING_UNAVAILABLE
    _log.warning("engine.scheduling_unavailable", node_id=ctx.node.id, route_to=route_to)
    return HandlerResult(actions, problem=problem, route_to=route_to, reports=reports)


def _offered(ctx: TurnContext, zone: tzinfo) -> list[Slot]:
    stored = ctx.state.variables.get(AVAILABLE_SLOTS)
    if not isinstance(stored, list):
        return []
    duration = timedelta(minutes=_config(ctx).slot_duration_minutes)
    return [slot for raw in stored if (slot := _slot(raw, zone, duration)) is not None]


def list_slots(ctx: TurnContext) -> dict[str, JsonValue]:
    """The ``list_slots`` tool message: the stored available slots."""
    zone = _zone(ctx, _config(ctx))
    return {"slots": [slot.as_json() for slot in _offered(ctx, zone)]}


def _caller_name(variables: dict[str, JsonValue]) -> str | None:
    name = variables.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    parts = [variables.get("first_name"), variables.get("last_name")]
    joined = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return joined or None


def _text_variable(variables: dict[str, JsonValue], name: str) -> str | None:
    value = variables.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _builtin_text(ctx: TurnContext, name: str) -> str | None:
    value = ctx.builtins.get(name)
    return value if isinstance(value, str) else None


def _booking_body(ctx: TurnContext, slot: Slot) -> dict[str, JsonValue]:
    variables = ctx.state.variables
    return {
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
        "name": _caller_name(variables),
        "phone": _builtin_text(ctx, "user_number"),
        "email": _text_variable(variables, "email"),
        "notes": _text_variable(variables, "notes"),
        "call_id": _builtin_text(ctx, "call_id"),
    }


def _booked(ctx: TurnContext, slot: Slot, outcome: HttpOutcome, zone: tzinfo) -> dict[str, JsonValue] | None:
    body = outcome.body
    if not outcome.succeeded or not isinstance(body, dict):
        return None
    booking_id = body.get("booking_id")
    if booking_id is None or isinstance(booking_id, dict | list | bool):
        return None
    start = _moment(body.get("start"), zone) or slot.start
    end = _moment(body.get("end"), zone) or slot.end
    booking = {
        "booking_id": str(booking_id),
        "booking_start": start.astimezone(zone).isoformat(),
        "booking_end": end.astimezone(zone).isoformat(),
    }
    ctx.state.variables.update(booking)
    return {"status": "booked", **booking}


async def book_slot(ctx: TurnContext, start: str) -> BookingOutcome:
    """Run the model's ``book_slot(start)`` call."""
    config = _config(ctx)
    zone = _zone(ctx, config)
    wanted = _moment(start, zone)
    slot = next((slot for slot in _offered(ctx, zone) if slot.start == wanted), None)
    if slot is None:
        return BookingOutcome(ok=False, tool_message={"error": "slot_unavailable"})
    call = HttpCall(
        method="POST",
        url=ctx.render_url(config.booking_url, field_name="scheduling.bookingUrl").strip(),
        headers=_headers(ctx, config),
        body=_booking_body(ctx, slot),
        timeout_ms=REQUEST_TIMEOUT_MS,
        retries=0,
    )
    sent = await send_request(ctx, call, purpose="booking")
    if sent is None:
        route_to = route_target(ctx, config.on_failure_node_id, source="onFailureNodeId")
        return BookingOutcome(ok=False, tool_message={"error": HTTP_UNAVAILABLE}, route_to=route_to)
    outcome, report = sent
    message = _booked(ctx, slot, outcome, zone)
    if message is not None:
        names = ("booking_id", "booking_start", "booking_end")
        return BookingOutcome(ok=True, tool_message=message, report=replace(report, variables_set=names))
    route_to = route_target(ctx, config.on_failure_node_id, source="onFailureNodeId")
    _log.warning("engine.booking_failed", node_id=ctx.node.id, outcome=outcome.kind, status=outcome.status)
    failure: dict[str, JsonValue] = {"error": "booking_failed", "status": outcome.status or 0}
    return BookingOutcome(
        ok=False, tool_message=failure, route_to=route_to, report=replace(report, route_to=route_to)
    )


class SchedulingHandler:
    """Scheduling nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Speech, then the availability request; the slots go to ``available_slots``."""
        config = _config(ctx)
        speech = queue_speech(ctx, config.speech, field_name="scheduling.speech")
        call = HttpCall(
            method="GET",
            url=ctx.render_url(config.availability_url, field_name="scheduling.availabilityUrl").strip(),
            headers=_headers(ctx, config),
            timeout_ms=REQUEST_TIMEOUT_MS,
            retries=AVAILABILITY_RETRIES,
        )
        sent = await send_request(ctx, call, purpose="availability")
        if sent is None:
            _log.warning("engine.http_unavailable", node_id=ctx.node.id)
            return _failure(ctx, config, speech, ())
        outcome, report = sent
        slots = None
        if outcome.succeeded:
            slots = parse_slots(
                outcome.body,
                now=ctx.now(),
                zone=_zone(ctx, config),
                lookahead_days=config.lookahead_days,
                slot_minutes=config.slot_duration_minutes,
            )
        if not slots:
            failed = _failure(ctx, config, speech, ())
            return replace(failed, reports=(replace(report, route_to=failed.route_to),))
        ctx.state.variables[AVAILABLE_SLOTS] = [slot.as_json() for slot in slots]
        report = replace(report, variables_set=(AVAILABLE_SLOTS,))
        return HandlerResult(speech, reports=(report,))

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """The dialogue reply (``text`` when set); the dialogue port offers the scheduling functions."""
        return speak_node(ctx, prompt_required=False)

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Always: the slot is negotiated with the caller."""
        del node
        return True
