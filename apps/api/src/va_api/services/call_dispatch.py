"""Send Call dispatch (specs/05-calls-api.md §4.1 steps 4-12, §5; Decision P05-T05b).

:func:`dispatch_call` reads the org settings (the reader opens its own session), then runs, in one
``tenant_session(ctx.org_id)``: input loading (steps 4-8), resolution, the check of the resolved ``webhook``,
:func:`va_billing.dispatch.admit_call` (steps 9-11), the ``calls`` row (with the price snapshot in
``config.pricing``), and the ``call.queued`` / ``call.scheduled`` event: its ``seq`` is assigned in Redis
before the commit and its ``call_events`` row is written in the same transaction. When anything fails before
the commit the transaction rolls back (row, event row, hold) and the Redis counters are given back.

After the commit the event is broadcast (best effort: the row is durable) and ``CallWorkflow`` is started. If
the start fails, the call moves to ``failed``, the hold is released, a ``call.error`` event
(``dispatch_failed``) is recorded, the counters are given back, and ``SERVICE_UNAVAILABLE`` is raised (the
idempotency claim is released for 5xx, so a retry runs again). When the row has already moved on (the
start reached Temporal despite the error, or the call was stopped), nothing is compensated and the call is
reported as created.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from fastapi import Request
from pydantic import JsonValue
from redis.exceptions import RedisError

from va_api.auth.context import RequestContext
from va_api.services.call_event_rows import EVENTS_UNAVAILABLE_MESSAGE, broadcast_event, persist_event
from va_api.services.call_inputs import CallInputs, load_call_inputs, resolve_inputs
from va_api.services.call_starter import CallStarter, CallStartError
from va_api.services.webhook_policy import check_call_webhook
from va_api.settings import ApiSettings
from va_billing import holds
from va_billing.dispatch import Admitted, CallAdmission, admit_call, load_billing_org
from va_billing.limits import CallLimiter
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.net import HostResolver
from va_common.time import Clock
from va_db.models import Call
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import CallRepo, CallTransitionError
from va_db.session import Database
from va_domain.call_resolution import CallResolution, PlatformModels, ResolutionWarning
from va_domain.call_workflow import CallWorkflowInput, call_workflow_id
from va_domain.calls import SendCallConfig, SendCallRequest
from va_domain.org import OrgSettings
from va_events import DomainEvent, EventPublisher, EventType, new_event

__all__ = [
    "EVENTS_UNAVAILABLE_MESSAGE",
    "ORG_NOT_FOUND_MESSAGE",
    "QUEUED_MESSAGE",
    "SCHEDULED_MESSAGE",
    "START_FAILED_ERROR",
    "START_FAILED_MESSAGE",
    "DispatchDeps",
    "DispatchedCall",
    "deps_of",
    "dispatch_call",
]

QUEUED_MESSAGE: Final = "Call successfully queued."
SCHEDULED_MESSAGE: Final = "Call successfully scheduled."
START_FAILED_MESSAGE: Final = "The call could not be started. Please retry."
START_FAILED_ERROR: Final = "Call workflow could not be started."
ORG_NOT_FOUND_MESSAGE: Final = "Organization not found."

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchDeps:
    """What a dispatch needs from the container."""

    database: Database
    limiter: CallLimiter
    publisher: EventPublisher
    starter: CallStarter
    org_settings: OrgSettingsReader
    settings: ApiSettings
    clock: Clock
    resolver: HostResolver


@dataclass(frozen=True, slots=True)
class DispatchedCall:
    """The created call; ``scheduled_for`` (UTC) is set for scheduled calls."""

    call_id: UUID
    message: str
    scheduled_for: datetime | None


@dataclass(frozen=True, slots=True)
class _Created:
    config: SendCallConfig
    admitted: Admitted
    event: DomainEvent


def deps_of(request: Request) -> DispatchDeps:
    """The dispatch dependencies of the running app."""
    container = request.app.state.container
    return DispatchDeps(
        database=container.database,
        limiter=container.call_limiter,
        publisher=container.event_publisher,
        starter=container.call_starter,
        org_settings=container.org_settings,
        settings=request.app.state.settings,
        clock=container.clock,
        resolver=container.host_resolver,
    )


async def dispatch_call(
    deps: DispatchDeps, ctx: RequestContext, request: SendCallRequest, *, encrypted_key: str | None
) -> DispatchedCall:
    """Validate, admit, persist, and start one call; raises the first failing step's ``AppError``."""
    org = await deps.org_settings.get(ctx.org_id)
    if org is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    call_id = new_id()
    created = await _create(deps, ctx, request, call_id, org_settings=org, encrypted_key=encrypted_key)
    await broadcast_event(deps.publisher, created.event)
    scheduled_for = _utc(created.config.start_time)
    try:
        await deps.starter.start(
            CallWorkflowInput(call_id=call_id, org_id=ctx.org_id, scheduled_for=scheduled_for)
        )
    except CallStartError as exc:
        _log.warning("calls.workflow_start_failed", call_id=str(call_id), error=str(exc.__cause__ or exc))
        if await _record_start_failure(deps, ctx.org_id, created.admitted):
            raise AppError("SERVICE_UNAVAILABLE", START_FAILED_MESSAGE) from exc
    message = QUEUED_MESSAGE if scheduled_for is None else SCHEDULED_MESSAGE
    return DispatchedCall(call_id=call_id, message=message, scheduled_for=scheduled_for)


async def _create(  # noqa: PLR0913 - the request's parts, keyword-only extras
    deps: DispatchDeps,
    ctx: RequestContext,
    request: SendCallRequest,
    call_id: UUID,
    *,
    org_settings: OrgSettings,
    encrypted_key: str | None,
) -> _Created:
    admitted: Admitted | None = None
    try:
        async with deps.database.tenant_session(ctx.org_id) as session:
            inputs = await load_call_inputs(
                session,
                ctx,
                request,
                org=org_settings,
                encrypted_key=encrypted_key,
                models=PlatformModels(deps.settings.llm_dialogue_model, deps.settings.llm_router_model),
                va_env=deps.settings.va_env,
            )
            now = deps.clock.now()
            resolution = resolve_inputs(inputs, request, now)
            config = resolution.config
            await check_call_webhook(
                config.webhook, org=inputs.org, settings=deps.settings, resolver=deps.resolver
            )
            billing = await load_billing_org(session, ctx.org_id)
            admitted = await admit_call(
                session,
                deps.limiter,
                billing,
                _admission(call_id, config, inputs, deps.settings, now),
                now=now,
            )
            session.add(_call_row(call_id, ctx.org_id, request, config, admitted))
            await session.flush()
            event = await persist_event(
                session, deps.publisher, _created_event(deps.clock, call_id, ctx, resolution)
            )
    except BaseException:
        if admitted is not None:
            await _undo_counters(admitted, deps.limiter)
        raise
    return _Created(config=config, admitted=admitted, event=event)


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _admission(
    call_id: UUID, config: SendCallConfig, inputs: CallInputs, settings: ApiSettings, now: datetime
) -> CallAdmission:
    return CallAdmission(
        call_id=call_id,
        destination=config.phone_number,
        region=inputs.country,
        max_duration_min=math.ceil(config.max_duration_min),
        at=config.start_time or now,
        scheduled=config.start_time is not None,
        allowed_countries=settings.allowed_destination_countries(),
    )


def _call_row(
    call_id: UUID, org_id: UUID, request: SendCallRequest, config: SendCallConfig, admitted: Admitted
) -> Call:
    scheduled = config.start_time is not None
    return Call(
        id=call_id,
        org_id=org_id,
        direction="outbound",
        to_number=config.phone_number,
        from_number=config.from_number,
        status="scheduled" if scheduled else "queued",
        queue_status="new" if scheduled else "queued",
        runner_type=config.runner_type,
        pathway_id=config.pathway_id,
        pathway_version=config.pathway_version,
        persona_id=config.persona_id,
        persona_version=config.persona_version,
        agent_id=config.agent_id,
        agent_version_id=config.agent_version_id,
        experiment_id=config.experiment_id,
        experiment_arm_id=config.experiment_arm_id,
        voice_id=config.voice_id,
        phone_number_id=config.phone_number_id,
        memory_id=config.memory_id,
        external_id=config.external_id,
        start_time=config.start_time,
        test_call=request.test_call,
        request_data=config.request_data,
        metadata_=config.metadata,
        config={**config.model_dump(mode="json"), "pricing": admitted.pricing.snapshot()},
        credit_hold_id=None if admitted.hold is None else admitted.hold.id,
        workflow_id=call_workflow_id(call_id),
    )


def _warnings(warnings: Sequence[ResolutionWarning]) -> list[JsonValue]:
    items: list[JsonValue] = []
    for warning in warnings:
        item: dict[str, JsonValue] = {"code": warning.code, "message": warning.message}
        if warning.field is not None:
            item["field"] = warning.field
        items.append(item)
    return items


def _created_event(
    clock: Clock, call_id: UUID, ctx: RequestContext, resolution: CallResolution
) -> DomainEvent:
    start_time = _utc(resolution.config.start_time)
    data: dict[str, JsonValue] = {"warnings": _warnings(resolution.warnings)}
    if start_time is None:
        data["status"] = "queued"
        return new_event(
            EventType.CALL_QUEUED,
            org_id=ctx.org_id,
            clock=clock,
            call_id=call_id,
            message=QUEUED_MESSAGE,
            data=data,
        )
    data["status"] = "scheduled"
    data["scheduled_for"] = start_time.isoformat().replace("+00:00", "Z")
    return new_event(
        EventType.CALL_SCHEDULED,
        org_id=ctx.org_id,
        clock=clock,
        call_id=call_id,
        message=SCHEDULED_MESSAGE,
        data=data,
    )


async def _undo_counters(admitted: Admitted, limiter: CallLimiter) -> None:
    """Give back the Redis counters and the concurrency slot (the hold row goes with the transaction).

    A Redis failure is logged and never replaces the error being handled: the slot expires by its score.
    """
    if admitted.ticket is not None:
        await admitted.ticket.rollback()
        try:
            await limiter.release_call(admitted.org_id, admitted.call_id)
        except (RedisError, OSError) as exc:
            _log.warning(
                "calls.counter_release_failed", call_id=str(admitted.call_id), error=type(exc).__name__
            )


async def _record_start_failure(deps: DispatchDeps, org_id: UUID, admitted: Admitted) -> bool:
    """Mark the call failed, release its hold, record ``call.error``, and give back the counters.

    Returns ``False`` when the row is no longer ``queued``/``scheduled``: a start that timed out may still
    have reached Temporal (the workflow moved the row on) or the call was stopped meanwhile. Then the call is
    not the dispatcher's to fail, so nothing is compensated (the workflow or the stop path owns the hold and
    the slot).
    """
    call_id = admitted.call_id
    error = new_event(
        EventType.CALL_ERROR,
        org_id=org_id,
        clock=deps.clock,
        call_id=call_id,
        level="error",
        message=START_FAILED_ERROR,
        data={"code": "dispatch_failed", "detail": "workflow_start"},
    )
    event: DomainEvent | None
    moved_on = False
    try:
        async with deps.database.tenant_session(org_id) as session:
            try:
                await CallRepo(session, org_id).transition(
                    call_id,
                    "failed",
                    from_={"queued", "scheduled"},
                    error_message=START_FAILED_ERROR,
                    end_reason="error",
                    ended_by="error",
                    ended_at=deps.clock.now(),
                    queue_status="call_error",
                )
            except CallTransitionError as exc:
                _log.warning("calls.start_failure_unrecorded", call_id=str(call_id), status=exc.current)
                moved_on = True
                return False
            if admitted.hold is not None:
                await holds.release(session, org_id, admitted.hold.id)
            try:
                event = await persist_event(session, deps.publisher, error)
            except AppError:
                event = None  # the failure itself must still be recorded; the event is lost with Redis
    finally:
        if not moved_on:  # pragma: no branch - moved_on is only set right before the return
            await _undo_counters(admitted, deps.limiter)
    if event is not None:
        await broadcast_event(deps.publisher, event)
    return True
