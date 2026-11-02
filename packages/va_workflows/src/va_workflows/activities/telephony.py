"""``dial`` and ``hangup_call``: the carrier side of ``CallWorkflow`` (specs/06-telephony-telnyx.md §6,
Decision P05-T06b).

``dial`` is safe to retry: Telnyx deduplicates ``dial-{call_id}``, and the status guard turns a repeated
attempt into ``already_dialed`` once the primary leg is recorded. The call moves to ``dialing`` (committed)
before the request goes out, so a ``call.initiated`` webhook that overtakes the response finds ``dialing``.
A scheduled call is admitted here (compliance, limits, credit hold; specs/24 §6 Decision P05-T04b). A dial
Telnyx rejects ends the call as ``failed``/``carrier_error`` with its hold and concurrency slot given back.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Final

import phonenumbers
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from va_billing import holds
from va_billing.dispatch import Admitted, CallAdmission, admit_call, load_billing_org
from va_billing.limits import CallLimiter
from va_common.errors import AppError
from va_common.logging import bound_context, get_logger
from va_common.stream_tokens import mint_stream_token, stream_token_expiry
from va_common.time import Clock
from va_db.call_context import config_of
from va_db.call_states import END_REASONS, is_terminal
from va_db.models import Call, CallStatus, TelnyxAccount
from va_db.repositories import CallLegRepo, CallRepo, CallTransitionError, OrgRepo
from va_db.session import Database
from va_domain.calls import SendCallConfig
from va_domain.org import load_settings
from va_events import EventPublisher, EventType
from va_telnyx import (
    ClientState,
    DialResult,
    TelnyxApiError,
    TelnyxClient,
    TelnyxClientFactory,
    TelnyxRateLimited,
    TelnyxTransportError,
    UnknownTelnyxAccount,
    command_id,
)
from va_telnyx.client_state import encode
from va_workflows.activities.dial_request import DialSettings, build_dial_request
from va_workflows.activities.errors import (
    DISPATCH_UNAVAILABLE,
    HANGUP_REJECTED,
    TELNYX_UNAVAILABLE,
    retryable,
)
from va_workflows.activities.events import broadcast_events, call_event, ended_event, persist_events
from va_workflows.activities.lookup import load_call
from va_workflows.activities.telephony_inputs import (
    DialInput,
    DialOutcome,
    HangupInput,
    HangupResult,
    max_duration_seconds,
)

__all__ = [
    "ACCOUNT_UNAVAILABLE_MESSAGE",
    "CARRIER_REJECTED_PREFIX",
    "CarrierFailure",
    "TelephonyActivities",
    "carrier_rejection",
    "destination_region",
    "release_slot",
]

ACCOUNT_UNAVAILABLE_MESSAGE: Final = "Telnyx account is not available for dialing."
CARRIER_REJECTED_PREFIX: Final = "Carrier rejected the dial: "
_ERROR_MESSAGE_CHARS: Final = 500
_PAST_DIAL: Final[frozenset[CallStatus]] = frozenset(
    {"ringing", "in_progress", "transferring", "transferred"}
)
_RETRYABLE_DISPATCH: Final = "SERVICE_UNAVAILABLE"
_REQUEUE_CODE: Final = "CONCURRENCY_CAP_REACHED"
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR: Final = 500

_log = get_logger(__name__)


def destination_region(e164: str) -> str | None:
    """ISO region of an E.164 number (``None`` for non-geographic or unparsable numbers)."""
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    except phonenumbers.NumberParseException:
        return None
    return region if region and region != "001" else None


async def release_slot(limiter: CallLimiter, org_id: uuid.UUID, call_id: uuid.UUID) -> None:
    """Free the call's concurrency slot; a Redis failure is logged (the slot expires by its score)."""
    try:
        await limiter.release_call(org_id, call_id)
    except (RedisError, OSError) as exc:
        _log.warning("calls.counter_release_failed", error=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class _Dial:
    """What the dial request needs, read in the status-guard transaction."""

    call_id: uuid.UUID
    org_id: uuid.UUID
    to: str
    from_: str
    config: SendCallConfig
    connection_id: str | None
    timeout_secs: int
    credit_hold_id: uuid.UUID | None


class _Done(Exception):  # noqa: N818 - control flow carrying the outcome, rolls the transaction back
    def __init__(self, outcome: DialOutcome) -> None:
        super().__init__(outcome.result)
        self.outcome = outcome


class _Blocked(Exception):  # noqa: N818 - control flow: a dispatch re-check failed
    def __init__(self, call: Call, error: AppError) -> None:
        super().__init__(error.code)
        self.call_id = call.id
        self.org_id = call.org_id
        self.error = error


class CarrierFailure(Exception):  # noqa: N818 - the dial could not be placed (``message``, ``detail``)
    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def carrier_rejection(error: TelnyxApiError) -> CarrierFailure:
    """The carrier failure of a dial Telnyx rejected: its first error's text, its code as the detail."""
    first = error.errors[0] if error.errors else None
    if first is None:
        return CarrierFailure(CARRIER_REJECTED_PREFIX + "unknown", "unknown")
    text = first.title or first.detail or first.code or "unknown"
    return CarrierFailure(CARRIER_REJECTED_PREFIX + text, first.code or "unknown")


def _outcome(result: str, status: str, **extra: str | int | None) -> DialOutcome:
    return DialOutcome.model_validate({"result": result, "status": status, **extra})


class TelephonyActivities:
    """Activities that talk to Telnyx for one call; one instance per worker process."""

    def __init__(  # noqa: PLR0913 - the activity collaborators
        self,
        *,
        database: Database,
        telnyx: TelnyxClientFactory,
        limiter: CallLimiter,
        publisher: EventPublisher,
        settings: DialSettings,
        clock: Clock,
    ) -> None:
        self._database = database
        self._telnyx = telnyx
        self._limiter = limiter
        self._publisher = publisher
        self._settings = settings
        self._clock = clock

    @activity.defn(name="dial")
    async def dial(self, request: DialInput) -> DialOutcome:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            try:
                plan = await self._prepare(request)
            except _Done as done:
                _log.info("calls.dial_skipped", result=done.outcome.result, status=done.outcome.status)
                return done.outcome
            try:
                result = await self._send(plan)
            except CarrierFailure as failure:
                return await self._carrier_failure(plan, failure)
            return await self._record_dialed(plan, result)

    @activity.defn(name="hangup_call")
    async def hangup_call(self, request: HangupInput) -> HangupResult:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            async with self._database.tenant_session(request.org_id) as session:
                leg = await CallLegRepo(session, request.org_id).primary(request.call_id)
            if leg is None:
                _log.info("calls.hangup_skipped", reason=request.reason, result="no_leg")
                return HangupResult(sent=False, reason="no_leg")
            state = encode(ClientState(call_id=request.call_id, leg="primary", org_id=request.org_id))
            try:
                client = await self._telnyx.for_account(leg.telnyx_account_id)
                sent = await client.hangup(
                    leg.call_control_id,
                    command_id=command_id("hangup", request.call_id, "primary"),
                    client_state=state,
                )
            except (TelnyxRateLimited, TelnyxTransportError) as exc:
                raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
            except TelnyxApiError as exc:
                if exc.status == _TOO_MANY_REQUESTS or exc.status >= _SERVER_ERROR:
                    raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
                raise ApplicationError(str(exc), type=HANGUP_REJECTED, non_retryable=True) from exc
            except UnknownTelnyxAccount as exc:
                raise ApplicationError(str(exc), type=HANGUP_REJECTED, non_retryable=True) from exc
            _log.info("calls.hangup_sent", reason=request.reason, result="sent" if sent else "already_ended")
            return HangupResult(sent=sent, reason="sent" if sent else "already_ended")

    async def _prepare(self, request: DialInput) -> _Dial:
        """The status guard (and the admission of a scheduled call), committed before Telnyx is called."""
        try:
            return await self._prepare_in_session(request)
        except _Blocked as blocked:
            raise _Done(await self._block(blocked)) from blocked

    async def _prepare_in_session(self, request: DialInput) -> _Dial:
        admitted: Admitted | None = None
        try:
            async with self._database.tenant_session(request.org_id) as session:
                call = await load_call(session, request)
                await self._guard(session, call)
                if call.status == "scheduled":
                    admitted = await self._admit(session, request, call)
                    call = await self._transition(
                        session,
                        call,
                        "queued",
                        queue_status="queued",
                        credit_hold_id=None if admitted.hold is None else admitted.hold.id,
                    )
                if call.status == "queued":
                    call = await self._transition(session, call, "dialing", dial_started_at=self._clock.now())
                return await self._plan(session, call)
        except BaseException:
            if admitted is not None:
                await self._undo_counters(admitted)
            raise

    async def _guard(self, session: AsyncSession, call: Call) -> None:
        """Raise :class:`_Done` unless the dial still has to be sent."""
        if is_terminal(call.status):
            raise _Done(_outcome("skipped", call.status))
        if call.status in _PAST_DIAL or call.status == "dialing":
            leg = await CallLegRepo(session, call.org_id).primary(call.id)
            if leg is not None or call.status != "dialing":
                ccid = None if leg is None else leg.call_control_id
                raise _Done(
                    _outcome(
                        "already_dialed",
                        call.status,
                        call_control_id=ccid,
                        max_duration_s=max_duration_seconds(config_of(call).max_duration_min),
                    )
                )

    async def _transition(self, session: AsyncSession, call: Call, to: CallStatus, **values: object) -> Call:
        try:
            return await CallRepo(session, call.org_id).transition(call.id, to, from_={call.status}, **values)
        except CallTransitionError as exc:
            raise _Done(_outcome("skipped", exc.current)) from exc

    async def _admit(self, session: AsyncSession, request: DialInput, call: Call) -> Admitted:
        """Dispatch-time re-checks of a scheduled call; failures become ``requeue``/``blocked`` outcomes."""
        config = config_of(call)
        now = self._clock.now()
        destination = call.to_number or config.phone_number
        admission = CallAdmission(
            call_id=call.id,
            destination=destination,
            region=destination_region(destination),
            max_duration_min=math.ceil(config.max_duration_min),
            at=now,
            scheduled=False,
            allowed_countries=self._settings.allowed_countries,
        )
        try:
            org = await load_billing_org(session, call.org_id)
            return await admit_call(session, self._limiter, org, admission, now=now)
        except AppError as exc:
            if exc.code == _RETRYABLE_DISPATCH:
                raise retryable(exc.message, DISPATCH_UNAVAILABLE) from exc
            if exc.code == _REQUEUE_CODE and not request.final_attempt:
                raise _Done(_outcome("requeue", call.status, code=exc.code)) from exc
            raise _Blocked(call, exc) from exc

    async def _block(self, blocked: _Blocked) -> DialOutcome:
        """``failed``/``dispatch_blocked`` with its two events (the admission transaction has rolled back)."""
        error, org_id, call_id = blocked.error, blocked.org_id, blocked.call_id
        async with self._database.tenant_session(org_id) as session:
            try:
                await CallRepo(session, org_id).transition(
                    call_id,
                    "failed",
                    from_={"scheduled"},
                    end_reason="dispatch_blocked",
                    ended_by=END_REASONS["dispatch_blocked"],
                    error_message=error.message,
                    queue_status="call_error",
                    ended_at=self._clock.now(),
                )
            except CallTransitionError as exc:
                return _outcome("skipped", exc.current, code=error.code)
            events = await persist_events(
                session,
                self._publisher,
                [
                    call_event(
                        self._clock,
                        org_id,
                        call_id,
                        EventType.CALL_DISPATCH_BLOCKED,
                        level="warning",
                        message=error.message,
                        data={"reason": error.code, "message": error.message},
                    ),
                    ended_event(self._clock, org_id, call_id, "dispatch_blocked"),
                ],
            )
        await broadcast_events(self._publisher, events)
        _log.info("calls.dispatch_blocked", code=error.code)
        return _outcome("blocked", "failed", code=error.code)

    async def _plan(self, session: AsyncSession, call: Call) -> _Dial:
        config = config_of(call)
        org = await OrgRepo(session).get(call.org_id)
        timeout_secs = load_settings(org.settings if org is not None else {}).dial_timeout_s
        connection_id: str | None = self._settings.call_control_app_id
        if config.telnyx_account_id is not None:
            account = await session.get(TelnyxAccount, config.telnyx_account_id)
            connection_id = None if account is None else account.call_control_app_id
        return _Dial(
            call_id=call.id,
            org_id=call.org_id,
            to=call.to_number or config.phone_number,
            from_=call.from_number or config.from_number,
            config=config,
            connection_id=connection_id,
            timeout_secs=timeout_secs,
            credit_hold_id=call.credit_hold_id,
        )

    async def _send(self, plan: _Dial) -> DialResult:
        client, connection_id = await self._client(plan)
        expires_at = stream_token_expiry(self._clock.now(), plan.config.max_duration_min)
        request = build_dial_request(
            plan.call_id,
            plan.org_id,
            plan.config,
            to=plan.to,
            from_=plan.from_,
            connection_id=connection_id,
            timeout_secs=plan.timeout_secs,
            stream_token=mint_stream_token(plan.call_id, expires_at, self._settings.stream_secret),
            settings=self._settings,
        )
        try:
            return await client.dial(request)
        except (TelnyxRateLimited, TelnyxTransportError) as exc:
            _log.warning("calls.dial_retry", error=type(exc).__name__)
            raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
        except TelnyxApiError as exc:
            raise carrier_rejection(exc) from exc

    async def _client(self, plan: _Dial) -> tuple[TelnyxClient, str]:
        """The Telnyx client and Call Control App of the call's account (platform or BYO)."""
        if plan.connection_id is None:
            raise CarrierFailure(ACCOUNT_UNAVAILABLE_MESSAGE, "account_unavailable")
        try:
            client = await self._telnyx.for_account(plan.config.telnyx_account_id)
        except UnknownTelnyxAccount as exc:
            raise CarrierFailure(ACCOUNT_UNAVAILABLE_MESSAGE, "account_unavailable") from exc
        return client, plan.connection_id

    async def _record_dialed(self, plan: _Dial, result: DialResult) -> DialOutcome:
        async with self._database.tenant_session(plan.org_id) as session:
            await CallLegRepo(session, plan.org_id).record_initiated(
                plan.call_id,
                "primary",
                result.call_control_id,
                call_leg_id=result.call_leg_id or None,
                call_session_id=result.call_session_id or None,
                telnyx_account_id=plan.config.telnyx_account_id,
                started_at=self._clock.now(),
            )
            events = await persist_events(
                session,
                self._publisher,
                [
                    call_event(
                        self._clock,
                        plan.org_id,
                        plan.call_id,
                        EventType.CALL_DIALING,
                        message="Dialing.",
                        data={"call_control_id": result.call_control_id},
                    )
                ],
            )
        await broadcast_events(self._publisher, events)
        _log.info("calls.dialed", call_control_id=result.call_control_id)
        return _outcome(
            "dialing",
            "dialing",
            call_control_id=result.call_control_id,
            max_duration_s=max_duration_seconds(plan.config.max_duration_min),
        )

    async def _carrier_failure(self, plan: _Dial, failure: CarrierFailure) -> DialOutcome:
        message = failure.message[:_ERROR_MESSAGE_CHARS]
        async with self._database.tenant_session(plan.org_id) as session:
            try:
                await CallRepo(session, plan.org_id).transition(
                    plan.call_id,
                    "failed",
                    from_={"dialing"},
                    end_reason="carrier_error",
                    ended_by=END_REASONS["carrier_error"],
                    error_message=message,
                    queue_status="call_error",
                    ended_at=self._clock.now(),
                )
            except CallTransitionError as exc:
                _log.warning("calls.dial_failure_unrecorded", status=exc.current)
                return DialOutcome(result="skipped", status=exc.current, code=failure.detail)
            if plan.credit_hold_id is not None:
                await holds.release(session, plan.org_id, plan.credit_hold_id)
            events = await persist_events(
                session,
                self._publisher,
                [
                    call_event(
                        self._clock,
                        plan.org_id,
                        plan.call_id,
                        EventType.CALL_ERROR,
                        level="error",
                        message=message,
                        data={"code": "dial_failed", "detail": failure.detail},
                    ),
                    ended_event(self._clock, plan.org_id, plan.call_id, "carrier_error"),
                ],
            )
        await release_slot(self._limiter, plan.org_id, plan.call_id)
        await broadcast_events(self._publisher, events)
        _log.warning("calls.dial_failed", detail=failure.detail)
        return DialOutcome(result="failed", status="failed", code=failure.detail)

    async def _undo_counters(self, admitted: Admitted) -> None:
        """Give back the counters of an admission whose transaction rolled back (the hold went with it)."""
        if admitted.ticket is not None:  # pragma: no branch - an unscheduled admission always has one
            await admitted.ticket.rollback()
            await release_slot(self._limiter, admitted.org_id, admitted.call_id)
