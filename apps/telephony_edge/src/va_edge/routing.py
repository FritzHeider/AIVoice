"""Inbound routing decisions (specs/06-telephony-telnyx.md §7 steps 1-7, Decision P05-T12b).

An incoming ``call.initiated`` that belongs to no call reaches :class:`InboundRouter` through the carrier
handler. The router looks the called number up (system scope), loads the facts of its org in one tenant
session and lets the pure :func:`decide` choose: refuse the call (Telnyx ``reject`` with a cause, or a
``transfer`` to the number's ``fallback_number``) or accept it. An accepted call takes a concurrency slot
under a pre-allocated call id and is handed to the :class:`InboundAcceptor` (specs/06 §7 steps 8-11).

A refusal ends the event: after the command succeeded the router raises
:class:`va_edge.processor.EventSettled`, so the processor stores the reason in ``telnyx_events.process_error``
without ever retrying the event. Failures before the command (database, Redis, Telnyx) raise normally and the
event is retried; the commands are idempotent through their ``command_id``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, NoReturn, Protocol

from va_billing.dispatch import BillingOrg, load_billing_org
from va_billing.holds import available_balance
from va_billing.limits import CallLimiter
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_compliance.dnc import find_block
from va_db.call_inputs.caller_id import inbound_destination
from va_db.models import PhoneNumber
from va_db.repositories import PhoneNumberRoutingRepo
from va_db.session import Database
from va_domain.numbers import InboundConfig, InboundConfigError, load_inbound_config, maintenance_active
from va_domain.phone import InvalidPhoneNumberError, normalize_e164
from va_edge.processor import EventSettled, StoredEvent
from va_telnyx.client import TelnyxClient
from va_telnyx.errors import CallAlreadyEnded
from va_telnyx.factory import TelnyxClientFactory
from va_telnyx.models import TransferRequest
from va_telnyx.voice_events import CallInitiated

__all__ = [
    "FALLBACK_TIMEOUT_S",
    "FALLBACK_TIME_LIMIT_S",
    "INBOUND_CALLS_TOTAL",
    "Accept",
    "Fallback",
    "InboundAcceptor",
    "InboundCall",
    "InboundRouter",
    "Reject",
    "RejectCause",
    "RoutedNumber",
    "RoutingDecision",
    "RoutingFacts",
    "decide",
    "fallback_of",
]

INBOUND_CALLS_TOTAL: Final = "va_inbound_calls_total"
FALLBACK_TIME_LIMIT_S: Final = 14_400
FALLBACK_TIMEOUT_S: Final = 30

type RejectCause = Literal["CALL_REJECTED", "USER_BUSY"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Reject:
    """Telnyx ``reject`` with ``cause``; ``reason`` is stored in ``telnyx_events.process_error``."""

    cause: RejectCause
    reason: str


@dataclass(frozen=True, slots=True)
class Fallback:
    """Telnyx ``transfer`` of the ringing call to ``to`` (the number's ``fallback_number``)."""

    to: str
    reason: str


@dataclass(frozen=True, slots=True)
class Accept:
    """Answer the call under ``config``; ``maintenance_message``: play the maintenance message and hang up
    (P05-T12c)."""

    config: InboundConfig
    maintenance_message: bool = False


type RoutingDecision = Reject | Fallback | Accept


@dataclass(frozen=True, slots=True)
class RoutingFacts:
    """What :func:`decide` needs about one incoming call.

    ``config`` is ``None`` when the stored ``inbound_config`` is invalid; ``fallback_number`` is then the
    stored value when it is still a valid E.164 number. ``caller`` is the caller normalized with the called
    number's region (``None``: not a dialable number, e.g. ``anonymous``). ``available_micros`` = balance -
    open holds + credit limit.
    """

    config: InboundConfig | None
    fallback_number: str | None
    caller: str | None
    org_status: str
    available_micros: int
    min_attempt_micros: int
    caller_dnc: bool
    now: datetime


def _refuse(facts: RoutingFacts, reason: str, cause: RejectCause = "CALL_REJECTED") -> Reject | Fallback:
    """Transfer to the fallback number when there is one, else reject with ``cause``."""
    if facts.fallback_number is not None:
        return Fallback(facts.fallback_number, reason)
    return Reject(cause, reason)


def _caller_refusal(facts: RoutingFacts, config: InboundConfig) -> RoutingDecision | None:
    caller = facts.caller
    if caller is not None and caller in config.blocked_callers:
        return Reject("CALL_REJECTED", "caller_blocked")
    if config.allowed_callers and caller not in config.allowed_callers:
        return Reject("CALL_REJECTED", "caller_not_allowed")
    return None


def _account_refusal(facts: RoutingFacts, _config: InboundConfig) -> RoutingDecision | None:
    if facts.org_status != "active":
        return _refuse(facts, f"org_{facts.org_status}")
    if facts.available_micros < facts.min_attempt_micros:
        return _refuse(facts, "insufficient_credits")
    return None


def _dnc_refusal(facts: RoutingFacts, config: InboundConfig) -> RoutingDecision | None:
    if config.block_dnc_callers and facts.caller_dnc:
        return Reject("CALL_REJECTED", "caller_dnc")
    return None


def _maintenance(facts: RoutingFacts, config: InboundConfig) -> RoutingDecision | None:
    if not maintenance_active(config.maintenance, facts.now):
        return None
    if facts.fallback_number is not None:
        return Fallback(facts.fallback_number, "maintenance")
    if config.maintenance.message is not None:
        return Accept(config, maintenance_message=True)
    return Reject("CALL_REJECTED", "maintenance")


_STEPS: Final = (_caller_refusal, _account_refusal, _dnc_refusal, _maintenance)


def decide(facts: RoutingFacts) -> RoutingDecision:
    """The specs/06 §7 order with the specs/16 §4 caller lists after the number lookup (first match wins):
    caller lists, org status, credits, DNC, maintenance.

    Concurrency (step 7) needs Redis and is checked by the router after an :class:`Accept`.
    """
    config = facts.config
    if config is None:
        return _refuse(facts, "invalid_inbound_config")
    for step in _STEPS:
        decision = step(facts, config)
        if decision is not None:
            return decision
    return Accept(config)


def fallback_of(config: InboundConfig | None, stored: Mapping[str, Any]) -> str | None:
    """The fallback number: the parsed one, or for an invalid config the stored value if it is E.164."""
    if config is not None:
        return config.fallback_number
    raw = stored.get("fallback_number")
    if not isinstance(raw, str):
        return None
    try:
        return raw if normalize_e164(raw) == raw else None
    except InvalidPhoneNumberError:
        return None


@dataclass(frozen=True, slots=True)
class RoutedNumber:
    """The called number's routing columns, detached from the system session."""

    phone_number_id: uuid.UUID
    org_id: uuid.UUID
    e164: str
    telnyx_account_id: uuid.UUID | None
    inbound_config: dict[str, Any]

    @classmethod
    def from_row(cls, row: PhoneNumber) -> RoutedNumber:
        return cls(
            phone_number_id=row.id,
            org_id=row.org_id,
            e164=row.e164,
            telnyx_account_id=row.telnyx_account_id,
            inbound_config=dict(row.inbound_config),
        )


@dataclass(frozen=True, slots=True)
class InboundCall:
    """An accepted incoming call holding a concurrency slot under ``call_id`` (specs/06 §7 steps 8-11)."""

    call_id: uuid.UUID
    event: StoredEvent
    payload: CallInitiated
    number: RoutedNumber
    config: InboundConfig
    caller: str
    maintenance_message: bool


class InboundAcceptor(Protocol):
    """Creates the call and answers it (P05-T12c); raising refuses the call through the fallback."""

    async def accept(self, call: InboundCall) -> None: ...


@dataclass(frozen=True, slots=True)
class _Loaded:
    facts: RoutingFacts
    org: BillingOrg


class InboundRouter:
    """Applies :func:`decide` to incoming calls: Telnyx commands, the concurrency slot, the acceptor."""

    def __init__(  # noqa: PLR0913 - collaborators plus keyword-only seams
        self,
        database: Database,
        limiter: CallLimiter,
        telnyx: TelnyxClientFactory,
        *,
        clock: Clock,
        metrics: ServiceMetrics,
        acceptor: InboundAcceptor | None = None,
    ) -> None:
        self._database = database
        self._limiter = limiter
        self._telnyx = telnyx
        self._clock = clock
        self._acceptor = acceptor
        self._calls = metrics.counter(INBOUND_CALLS_TOTAL)

    async def route(self, event: StoredEvent, payload: CallInitiated) -> None:
        number = await self._number(payload.to)
        if number is None:
            unknown = Reject("CALL_REJECTED", "unknown_number")
            await self._refuse(event, payload, self._telnyx.platform, unknown, called=payload.to or "")
        caller = inbound_destination(payload.from_ or "", number.e164).e164
        loaded = await self._load(number, caller)
        decision = decide(loaded.facts)
        if isinstance(decision, Accept):
            refusal = await self._accept(
                event, payload, number=number, loaded=loaded, caller=caller, decision=decision
            )
            if refusal is None:
                self._calls.labels(result="answered").inc()
                return
            decision = refusal
        client = await self._telnyx.for_account(number.telnyx_account_id)
        await self._refuse(event, payload, client, decision, called=number.e164)

    async def _number(self, called: str | None) -> RoutedNumber | None:
        if not called:
            return None
        # System scope (specs/03 §2): the org is unknown until the called number is found.
        async with self._database.system_session() as session:
            row = await PhoneNumberRoutingRepo(session).get_active(called)
            return None if row is None else RoutedNumber.from_row(row)

    def _config(self, number: RoutedNumber) -> InboundConfig | None:
        try:
            return load_inbound_config(number.inbound_config)
        except InboundConfigError as exc:
            _log.warning(
                "telnyx.inbound_config_invalid",
                phone_number_id=str(number.phone_number_id),
                org_id=str(number.org_id),
                field=exc.field,
            )
            return None

    async def _load(self, number: RoutedNumber, caller: str) -> _Loaded:
        config = self._config(number)
        dialable = caller if caller.startswith("+") else None
        now = self._clock.now()
        async with self._database.tenant_session(number.org_id) as session:
            org = await load_billing_org(session, number.org_id)
            available = await available_balance(session, number.org_id)
            dnc = (
                config is not None
                and config.block_dnc_callers
                and dialable is not None
                and await find_block(session, number.org_id, dialable, now=now) is not None
            )
        facts = RoutingFacts(
            config=config,
            fallback_number=fallback_of(config, number.inbound_config),
            caller=dialable,
            org_status=org.status,
            available_micros=available,
            min_attempt_micros=org.pricing.min_attempt_micros,
            caller_dnc=dnc,
            now=now,
        )
        return _Loaded(facts, org)

    async def _accept(  # noqa: PLR0913 - one accepted call's parts
        self,
        event: StoredEvent,
        payload: CallInitiated,
        *,
        number: RoutedNumber,
        loaded: _Loaded,
        caller: str,
        decision: Accept,
    ) -> Reject | Fallback | None:
        """Take the slot and hand the call over; the refusal to apply instead, or ``None`` when accepted."""
        config = decision.config
        call_id = new_id()
        taken = await self._limiter.acquire_inbound(
            number.org_id,
            call_id,
            limits=loaded.org.limits,
            org_created_at=loaded.org.created_at,
            max_duration_min=math.ceil(config.max_duration),
        )
        if not taken:
            return _refuse(loaded.facts, "concurrency_cap", "USER_BUSY")
        if self._acceptor is None:
            await self._limiter.release_call(number.org_id, call_id)
            return _refuse(loaded.facts, "accept_unavailable")
        call = InboundCall(
            call_id=call_id,
            event=event,
            payload=payload,
            number=number,
            config=config,
            caller=caller,
            maintenance_message=decision.maintenance_message,
        )
        try:
            await self._acceptor.accept(call)
        except Exception as exc:  # any accept failure falls back (specs/16 §4 "pipeline failure")
            _log.error(
                "telnyx.inbound_accept_failed",
                call_id=str(call_id),
                org_id=str(number.org_id),
                error_type=type(exc).__name__,
            )
            await self._limiter.release_call(number.org_id, call_id)
            return _refuse(loaded.facts, "accept_failed")
        return None

    async def _refuse(
        self,
        event: StoredEvent,
        payload: CallInitiated,
        client: TelnyxClient,
        decision: Reject | Fallback,
        *,
        called: str,
    ) -> NoReturn:
        """Send the refusal command, then settle the event with the decision's reason."""
        ccid = payload.call_control_id
        kind = "fallback" if isinstance(decision, Fallback) else "rejected"
        try:
            if isinstance(decision, Fallback):
                await client.transfer(ccid, _fallback_transfer(decision.to, called, ccid))
            else:
                await client.reject(ccid, decision.cause, command_id=f"reject-{ccid}")
        except CallAlreadyEnded:
            _log.info(
                "telnyx.inbound_caller_gone", telnyx_event_id=event.telnyx_event_id, reason=decision.reason
            )
        self._calls.labels(result=kind).inc()
        _log.info(
            "telnyx.inbound_refused",
            telnyx_event_id=event.telnyx_event_id,
            decision=kind,
            reason=decision.reason,
        )
        raise EventSettled(decision.reason)


def _fallback_transfer(to: str, called: str, ccid: str) -> TransferRequest:
    """The fallback transfer: caller ID = the called number, Telnyx's maximum time limit, no client state."""
    return TransferRequest(
        to=to,
        from_=called,
        time_limit_secs=FALLBACK_TIME_LIMIT_S,
        timeout_secs=FALLBACK_TIMEOUT_S,
        command_id=f"fallback-{ccid}",
    )
