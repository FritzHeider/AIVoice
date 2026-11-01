"""Carrier event handler: applies the specs/06-telephony-telnyx.md §5 mapping to stored voice events (specs/06
§5 Decision P05-T02).

One :class:`CarrierEventHandler` serves every type of :data:`va_edge.mapping.VOICE_EVENT_TYPES`:

1. Resolve the call: the ``client_state`` the intake attributed (org and call id) or, failing that, the
   ``call_legs`` row of the ``call_control_id`` (system scope, specs/03 §2 ``CallLegLookupRepo``). An
   incoming ``call.initiated`` without a call goes to the inbound router; other unresolvable events are
   logged and done.
2. Snapshot the call (``tenant_session``) plus the ownership key and the transfer clock from Redis, plan the
   effects, and apply the leg and call changes in the same transaction.
3. After the commit: ``call:{<id>}:carrier_state`` (``HSET`` + ``EXPIRE``), the domain events (only when this
   processing applied the status change, or on a retry that finds the call already there), the
   ``call:{<id>}:carrier`` message, and the ``CallWorkflow`` signal.

Every step after the commit is idempotent or deduplicated downstream, so a Redis or Temporal failure simply
raises: the processor stores ``process_error`` and the reprocessor runs the event again.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, cast

from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_db.repositories import (
    CallLegLookupRepo,
    CallLegRepo,
    CallRepo,
    CallTransitionError,
    NotFoundError,
)
from va_db.session import Database
from va_domain.carrier import (
    CARRIER_STATE_TTL_S,
    CarrierEvent,
    CarrierLeg,
    carrier_channel,
    carrier_state_key,
    owner_key,
)
from va_edge.mapping import (
    AMD_DETECTION_TYPES,
    VOICE_EVENT_TYPES,
    CallSnapshot,
    CarrierEffects,
    plan_effects,
)
from va_edge.processor import EventHandler, StoredEvent
from va_edge.signals import CarrierSignaller
from va_events.model import new_event
from va_events.publisher import EventPublisher
from va_telnyx.client_state import ClientState, ClientStateError, decode
from va_telnyx.voice_events import CallInitiated, CallPayload, ConferencePayload
from va_telnyx.webhooks import EventPayload

__all__ = ["AMD_RESULTS_TOTAL", "CarrierEventHandler", "InboundRouter"]

AMD_RESULTS_TOTAL: Final = "va_amd_results_total"
_TRANSFER_STARTED_AT: Final = "transfer_started_at"

_log = get_logger(__name__)


class InboundRouter(Protocol):
    """Routes an incoming ``call.initiated`` that belongs to no call yet (specs/06 §7, P05-T12)."""

    async def route(self, event: StoredEvent, payload: CallInitiated) -> None: ...


@dataclass(frozen=True, slots=True)
class _Target:
    org_id: uuid.UUID
    call_id: uuid.UUID
    leg: CarrierLeg


@dataclass(frozen=True, slots=True)
class _Applied:
    effects: CarrierEffects
    emit: bool


def _client_state(value: str | None) -> ClientState | None:
    if not value:
        return None
    try:
        return decode(value)
    except ClientStateError:
        return None


def _text(value: bytes | str | None) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


class CarrierEventHandler:
    """Applies carrier events to calls, legs, Redis carrier state, the event bus, and ``CallWorkflow``."""

    def __init__(  # noqa: PLR0913 - collaborators plus keyword-only seams
        self,
        database: Database,
        redis: Redis,
        publisher: EventPublisher,
        signaller: CarrierSignaller,
        *,
        clock: Clock,
        metrics: ServiceMetrics,
        inbound: InboundRouter | None = None,
    ) -> None:
        self._database = database
        self._redis = redis
        self._publisher = publisher
        self._signaller = signaller
        self._clock = clock
        self._inbound = inbound
        self._amd_results = metrics.counter(AMD_RESULTS_TOTAL)

    def handlers(self) -> dict[str, EventHandler]:
        """The processor registry: :meth:`handle` for every voice event type of specs/06 §5."""
        return dict.fromkeys(sorted(VOICE_EVENT_TYPES), self.handle)

    async def handle(self, event: StoredEvent, payload: EventPayload) -> None:
        if not isinstance(payload, CallPayload | ConferencePayload):
            return
        target = await self._resolve(event, payload)
        if target is None:
            await self._unresolved(event, payload)
            return
        owner_present, transfer_started_at = await self._redis_snapshot(target.call_id)
        applied = await self._apply_db(event, payload, target, owner_present, transfer_started_at)
        if applied is None:
            _log.info(
                "telnyx.event_unattributed",
                event_type=event.event_type,
                telnyx_event_id=event.telnyx_event_id,
            )
            return
        await self._after_commit(event, target, applied)

    async def _resolve(self, event: StoredEvent, payload: CallPayload | ConferencePayload) -> _Target | None:
        state = _client_state(payload.client_state)
        if state is not None and event.call_id == state.call_id and event.org_id == state.org_id:
            return _Target(state.org_id, state.call_id, state.leg)
        if payload.call_control_id is None:
            return None
        # System scope (specs/03 §2): the leg must be attributed before any org context exists.
        async with self._database.system_session() as session:
            ref = await CallLegLookupRepo(session).by_call_control_id(payload.call_control_id)
        if ref is None or (event.org_id is not None and ref.org_id != event.org_id):
            return None
        return _Target(ref.org_id, ref.call_id, ref.role)

    async def _unresolved(self, event: StoredEvent, payload: CallPayload | ConferencePayload) -> None:
        if isinstance(payload, CallInitiated) and payload.direction == "incoming":
            if self._inbound is not None:
                await self._inbound.route(event, payload)
                return
            _log.info("telnyx.inbound_unrouted", telnyx_event_id=event.telnyx_event_id)
            return
        _log.info(
            "telnyx.event_unattributed", event_type=event.event_type, telnyx_event_id=event.telnyx_event_id
        )

    async def _redis_snapshot(self, call_id: uuid.UUID) -> tuple[bool, datetime | None]:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.exists(owner_key(call_id))
            pipe.hget(carrier_state_key(call_id), _TRANSFER_STARTED_AT)
            owners, started = await pipe.execute()
        started_text = _text(started)
        return bool(owners), datetime.fromisoformat(started_text) if started_text else None

    async def _apply_db(
        self,
        event: StoredEvent,
        payload: CallPayload | ConferencePayload,
        target: _Target,
        owner_present: bool,
        transfer_started_at: datetime | None,
    ) -> _Applied | None:
        async with self._database.tenant_session(target.org_id) as session:
            calls = CallRepo(session, target.org_id)
            try:
                call = await calls.get(target.call_id)
            except NotFoundError:
                return None
            snapshot = CallSnapshot(
                call_id=call.id,
                org_id=call.org_id,
                status=call.status,
                leg_role=target.leg,
                answered_at=call.answered_at,
                end_reason=call.end_reason,
                owner_present=owner_present,
                transfer_started_at=transfer_started_at,
            )
            effects = plan_effects(event.event_type, payload, snapshot, occurred_at=event.occurred_at)
            await self._apply_leg(session, event, payload, target, effects)
            emit = await self._apply_call(calls, event, target, effects)
        return _Applied(effects, emit)

    async def _apply_leg(
        self,
        session: AsyncSession,
        event: StoredEvent,
        payload: CallPayload | ConferencePayload,
        target: _Target,
        effects: CarrierEffects,
    ) -> None:
        ccid = payload.call_control_id
        if ccid is None:
            return
        legs = CallLegRepo(session, target.org_id)
        if effects.leg is not None:
            started_at = payload.start_time if isinstance(payload, CallPayload) else None
            await legs.record_initiated(
                target.call_id,
                target.leg,
                ccid,
                call_leg_id=payload.call_leg_id,
                call_session_id=payload.call_session_id,
                started_at=started_at or event.occurred_at,
            )
            if effects.leg.state != "initiated":
                await legs.advance(ccid, effects.leg.state, **effects.leg.values)
        if effects.amd_result is not None:
            await legs.set_amd(ccid, effects.amd_result)

    async def _apply_call(
        self, calls: CallRepo, event: StoredEvent, target: _Target, effects: CarrierEffects
    ) -> bool:
        """Apply the call change; whether the change's events should be published."""
        change = effects.call
        if change is None:
            return True
        if change.to is None:
            await calls.update_fields(target.call_id, **change.values)
            return True
        try:
            await calls.transition(target.call_id, change.to, **change.values)
        except CallTransitionError as conflict:
            _log.debug(
                "telnyx.transition_skipped",
                event_type=event.event_type,
                call_id=str(target.call_id),
                current=conflict.current,
                target=change.to,
            )
            return event.attempts > 0 and conflict.current == change.to
        return True

    async def _after_commit(self, event: StoredEvent, target: _Target, applied: _Applied) -> None:
        effects = applied.effects
        if effects.carrier_state:
            key = carrier_state_key(target.call_id)
            async with self._redis.pipeline(transaction=True) as pipe:
                # Mapping keys are invariant: dict[str, str] is a valid redis field mapping all the same.
                pipe.hset(key, mapping=cast("Mapping[FieldT, EncodableT]", effects.carrier_state))
                pipe.expire(key, CARRIER_STATE_TTL_S)
                await pipe.execute()
        if applied.emit:
            for planned in effects.events:
                domain_event = new_event(
                    planned.type,
                    org_id=target.org_id,
                    clock=self._clock,
                    call_id=target.call_id,
                    level=planned.level,
                    data=dict(planned.data),
                )
                await self._publisher.publish(domain_event)
        carrier = CarrierEvent(
            type=event.event_type,
            leg=target.leg,
            call_id=target.call_id,
            org_id=target.org_id,
            telnyx_event_id=event.telnyx_event_id,
            occurred_at=event.occurred_at,
            payload=dict(event.envelope.get("data", {}).get("payload", {})),
        )
        if effects.publish:
            await self._redis.publish(carrier_channel(target.call_id), carrier.model_dump_json())
        if effects.signal:
            await self._signaller.signal(carrier)
        if event.event_type in AMD_DETECTION_TYPES and effects.amd_result is not None:
            self._amd_results.labels(result=effects.amd_result).inc()
        if event.event_type.startswith("call.playback."):
            _log.debug("telnyx.playback", event_type=event.event_type, call_id=str(target.call_id))
