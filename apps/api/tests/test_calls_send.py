"""``POST /v1/calls`` end to end over a real database (specs/05 §4.1 steps 1-12, §5; Decision P05-T05b)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from call_helpers import CallableOrg, bearer, business_hours_today, callable_org, send_call, task_body
from call_input_rows import Seed, random_e164
from event_helpers import stream_events
from identity_db import IdentityDb
from redis.exceptions import RedisError
from service_helpers import json_log_stream, log_events
from session_helpers import (
    SessionContainer,
    error_of,
    fund,
    session_app,
    session_client,
    set_org,
    store_api_key,
    sub_account,
)
from sqlalchemy import select

from va_api.services.call_starter import CallStartError
from va_billing import keys
from va_billing.dispatch import load_billing_org
from va_common.time import FrozenClock
from va_db.models import Call, CallEvent, CallStatus, CreditHold, DncEntry
from va_db.repositories import CallRepo
from va_domain.call_workflow import CallWorkflowInput
from va_events import EventPublisher, EventPublishError
from va_events.keys import stream_key

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

IDEMPOTENCY = "Idempotency-Key"


@pytest.fixture
def container(identity: IdentityDb) -> SessionContainer:
    """Frozen at 18:00 UTC today: inside the migration's ``call_events`` partitions, daytime in the US."""
    return SessionContainer(identity=identity, clock=FrozenClock(business_hours_today()))


@pytest.fixture
async def org(identity: IdentityDb, container: SessionContainer) -> CallableOrg:
    return await callable_org(identity, container)


async def calls_of(container: SessionContainer, org_id: uuid.UUID) -> list[Call]:
    async with container.database.tenant_session(org_id) as session:
        return list((await session.scalars(select(Call).order_by(Call.created_at))).all())


async def events_of(container: SessionContainer, org_id: uuid.UUID) -> list[CallEvent]:
    async with container.database.tenant_session(org_id) as session:
        return list((await session.scalars(select(CallEvent).order_by(CallEvent.seq))).all())


async def holds_of(container: SessionContainer, org_id: uuid.UUID) -> list[CreditHold]:
    async with container.database.tenant_session(org_id) as session:
        return list((await session.scalars(select(CreditHold))).all())


async def stream_types(container: SessionContainer) -> list[str]:
    return [event.type.value for event in await stream_events(container.redis, stream_key("calls"))]


async def daily_count(container: SessionContainer, org_id: uuid.UUID) -> int:
    value = await container.redis.get(keys.daily_key(org_id, container.clock.now()))
    return 0 if value is None else int(value)


async def assert_nothing_dispatched(container: SessionContainer, org: CallableOrg) -> None:
    assert await calls_of(container, org.org_id) == []
    assert await events_of(container, org.org_id) == []
    assert [h for h in await holds_of(container, org.org_id) if h.status == "held"] == []
    assert container.call_starter.started == []
    assert await daily_count(container, org.org_id) == 0
    assert await container.call_limiter.active_calls(org.org_id) == 0


# --- success ---


async def test_an_immediate_call_is_queued_held_recorded_and_started(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org, webhook="https://hooks.bellapasta.test/calls", max_duration=4.5)
    response = await send_call(client, org.key, body)
    assert response.status_code == 200, response.text
    payload = response.json()
    call_id = uuid.UUID(payload["call_id"])
    assert payload == {
        "status": "success",
        "message": "Call successfully queued.",
        "call_id": str(call_id),
        "batch_id": None,
    }

    [call] = await calls_of(container, org.org_id)
    assert (call.id, call.status, call.queue_status, call.direction) == (
        call_id,
        "queued",
        "queued",
        "outbound",
    )
    assert (call.to_number, call.from_number) == (body["phone_number"], org.from_number)
    assert (call.runner_type, call.voice_id, call.phone_number_id) == (
        "task",
        org.voice_id,
        org.from_number_id,
    )
    assert call.workflow_id == f"call-{call_id}"
    assert (call.config["phone_number"], call.config["max_duration_min"]) == (body["phone_number"], 4.5)
    [held] = await holds_of(container, org.org_id)
    assert (held.id, held.status, held.reference_id) == (call.credit_hold_id, "held", str(call_id))

    [event] = await events_of(container, org.org_id)
    assert (event.call_id, event.seq, event.category, event.level) == (call_id, 1, "queue", "info")
    assert event.message == "Call successfully queued."
    assert event.data == {"warnings": [], "status": "queued"}
    assert await stream_types(container) == ["call.queued"]

    assert container.call_starter.started == [CallWorkflowInput(call_id=call_id, org_id=org.org_id)]
    assert await daily_count(container, org.org_id) == 1
    assert await container.call_limiter.active_calls(org.org_id) == 1
    assert container.host_resolver.lookups == ["hooks.bellapasta.test"]


async def test_the_pricing_snapshot_is_stored_in_the_config(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    response = await send_call(client, org.key, task_body(org))
    assert response.status_code == 200, response.text
    [call] = await calls_of(container, org.org_id)
    async with container.database.tenant_session(org.org_id) as session:
        billing = await load_billing_org(session, org.org_id)
    assert call.config["pricing"] == billing.pricing.snapshot()


async def test_a_scheduled_call_is_scheduled_without_hold_or_counters(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    start = container.clock.now() + timedelta(days=1)
    body = task_body(org, start_time=start.strftime("%Y-%m-%d %H:%M:%S +00:00"))
    response = await send_call(client, org.key, body)
    assert response.status_code == 200, response.text
    payload = response.json()
    call_id = uuid.UUID(payload["call_id"])
    expected_for = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert payload == {
        "status": "success",
        "message": "Call successfully scheduled.",
        "call_id": str(call_id),
        "batch_id": None,
        "scheduled_for": expected_for,
    }
    [call] = await calls_of(container, org.org_id)
    assert (call.status, call.queue_status, call.start_time, call.credit_hold_id) == (
        "scheduled",
        "new",
        start,
        None,
    )
    assert await holds_of(container, org.org_id) == []
    assert await daily_count(container, org.org_id) == 0
    assert await container.call_limiter.active_calls(org.org_id) == 0
    [event] = await events_of(container, org.org_id)
    assert event.message == "Call successfully scheduled."
    assert event.data == {"warnings": [], "status": "scheduled", "scheduled_for": expected_for}
    assert await stream_types(container) == ["call.scheduled"]
    assert container.call_starter.started == [
        CallWorkflowInput(call_id=call_id, org_id=org.org_id, scheduled_for=start)
    ]


async def test_resolution_warnings_ride_on_the_queued_event(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    pathway_id = await org.seed.pathway()
    response = await send_call(client, org.key, task_body(org, pathway_id=str(pathway_id)))
    assert response.status_code == 200, response.text
    [call] = await calls_of(container, org.org_id)
    assert (call.runner_type, call.pathway_id, call.pathway_version) == ("pathway", pathway_id, 1)
    [event] = await events_of(container, org.org_id)
    assert event.data["warnings"] == [
        {
            "code": "TASK_IGNORED",
            "message": "task is ignored because the call runs a pathway.",
            "field": "task",
        }
    ]


async def test_a_parent_acting_on_a_sub_account_places_the_call_there(
    identity: IdentityDb, container: SessionContainer
) -> None:
    parent = await callable_org(identity, container, plan_id="scale")
    async with session_client(session_app(container)) as client:
        child_id, _ = await sub_account(client, **bearer(parent.key))
        child = Seed(identity.database, child_id)
        await child.voice("Karen")
        number_id, e164 = await child.number()
        await set_org(identity, child_id, created_at=container.clock.now() - timedelta(days=30))
        await fund(container, child_id, "20")
        body = {**task_body(parent), "from": e164}
        response = await send_call(client, parent.key, body, **{"X-VA-Org-Id": str(child_id)})
    assert response.status_code == 200, response.text
    [call] = await calls_of(container, child_id)
    assert (call.org_id, call.phone_number_id) == (child_id, number_id)
    assert await calls_of(container, parent.org_id) == []
    assert container.call_starter.started[0].org_id == child_id


# --- idempotency ---


async def test_an_idempotent_retry_replays_without_dispatching_again(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org)
    first = await send_call(client, org.key, body, **{IDEMPOTENCY: "send-1"})
    replay = await send_call(client, org.key, body, **{IDEMPOTENCY: "send-1"})
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert len(await calls_of(container, org.org_id)) == 1
    assert len(container.call_starter.started) == 1

    reused = await send_call(client, org.key, {**body, "task": "Something else."}, **{IDEMPOTENCY: "send-1"})
    assert error_of(reused)[:2] == (422, "IDEMPOTENCY_KEY_REUSED")


async def test_a_stored_client_error_is_replayed(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org, phone_number="12")
    first = await send_call(client, org.key, body, **{IDEMPOTENCY: "bad-1"})
    replay = await send_call(client, org.key, body, **{IDEMPOTENCY: "bad-1"})
    assert error_of(first)[:2] == (400, "INVALID_PHONE_NUMBER")
    assert replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    await assert_nothing_dispatched(container, org)


# --- client errors through the endpoint ---


async def test_missing_phone_number_is_invalid_input(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org)
    del body["phone_number"]
    assert error_of(await send_call(client, org.key, body)) == (400, "INVALID_INPUT", "phone_number")
    await assert_nothing_dispatched(container, org)


async def test_two_runners_conflict(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org, agent_id=str(uuid.uuid4()), pathway_id=str(uuid.uuid4()))
    status, code, _ = error_of(await send_call(client, org.key, body))
    assert (status, code) == (400, "RUNNER_CONFLICT")
    await assert_nothing_dispatched(container, org)


async def test_an_unknown_pathway_is_not_found(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org, pathway_id=str(uuid.uuid4()))
    assert error_of(await send_call(client, org.key, body)) == (404, "NOT_FOUND", "pathway_id")
    await assert_nothing_dispatched(container, org)


async def test_an_unknown_encrypted_key_is_rejected(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    response = await send_call(client, org.key, task_body(org), encrypted_key="enc_unknown")
    assert error_of(response)[:2] == (400, "INVALID_ENCRYPTED_KEY")
    await assert_nothing_dispatched(container, org)


async def test_an_invalid_destination_is_rejected(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    response = await send_call(client, org.key, task_body(org, phone_number="+1 555"))
    assert error_of(response) == (400, "INVALID_PHONE_NUMBER", "phone_number")
    await assert_nothing_dispatched(container, org)


async def test_a_webhook_resolving_to_a_private_address_is_rejected(
    identity: IdentityDb, container: SessionContainer
) -> None:
    org = await callable_org(identity, container)
    container.host_resolver.answers["hooks.internal.test"] = ["10.0.0.1"]
    async with session_client(session_app(container, webhook_allow_private=False)) as client:
        response = await send_call(client, org.key, task_body(org, webhook="https://hooks.internal.test/x"))
    assert error_of(response) == (400, "WEBHOOK_URL_REJECTED", "webhook")
    await assert_nothing_dispatched(container, org)


async def test_a_dnc_number_is_blocked(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    destination = random_e164()
    await org.seed.row(DncEntry, phone_e164=destination, source="api")
    response = await send_call(client, org.key, task_body(org, phone_number=destination))
    assert error_of(response) == (403, "NUMBER_BLOCKED", "phone_number")
    await assert_nothing_dispatched(container, org)


async def test_the_same_number_within_ten_seconds_is_refused(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    body = task_body(org)
    assert (await send_call(client, org.key, body)).status_code == 200
    second = await send_call(client, org.key, body)
    assert error_of(second)[:2] == (429, "SAME_NUMBER_TOO_SOON")
    assert len(await calls_of(container, org.org_id)) == 1
    assert len(container.call_starter.started) == 1
    assert await daily_count(container, org.org_id) == 1


async def test_an_unfunded_org_gives_back_its_counters(
    identity: IdentityDb, client: httpx.AsyncClient, container: SessionContainer
) -> None:
    org = await callable_org(identity, container, dollars=None)
    response = await send_call(client, org.key, task_body(org))
    assert error_of(response)[:2] == (402, "INSUFFICIENT_CREDITS")
    await assert_nothing_dispatched(container, org)


async def test_a_key_without_calls_write_is_forbidden(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    key, _ = await store_api_key(container, org.org_id, scopes=("calls:read",))
    response = await send_call(client, key, task_body(org))
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    await assert_nothing_dispatched(container, org)


# --- Temporal and Redis failures ---


async def test_a_failed_workflow_start_fails_the_call_and_gives_everything_back(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    container.call_starter.fail = CallStartError("temporal down")
    body = task_body(org)
    response = await send_call(client, org.key, body, **{IDEMPOTENCY: "start-1"})
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert response.json()["message"] == "The call could not be started. Please retry."

    [call] = await calls_of(container, org.org_id)
    assert (call.status, call.queue_status, call.end_reason, call.ended_by) == (
        "failed",
        "call_error",
        "error",
        "error",
    )
    assert call.error_message == "Call workflow could not be started."
    assert call.ended_at == container.clock.now()
    [held] = await holds_of(container, org.org_id)
    assert held.status == "released"
    assert await daily_count(container, org.org_id) == 0
    assert await container.call_limiter.active_calls(org.org_id) == 0
    queued, error = await events_of(container, org.org_id)
    assert (queued.seq, queued.message, error.seq, error.level, error.category) == (
        1,
        "Call successfully queued.",
        2,
        "error",
        "error",
    )
    assert error.data == {"code": "dispatch_failed", "detail": "workflow_start"}
    assert await stream_types(container) == ["call.queued", "call.error"]

    container.call_starter.fail = None
    retry = await send_call(client, org.key, body, **{IDEMPOTENCY: "start-1"})
    assert "Idempotent-Replayed" not in retry.headers
    # The claim was released, so the retry ran again (and met the first attempt's same-number entry).
    assert error_of(retry)[:2] == (429, "SAME_NUMBER_TOO_SOON")


async def test_a_seq_failure_rolls_everything_back(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_self: EventPublisher, event: Any) -> Any:
        raise EventPublishError(event, "seq")

    monkeypatch.setattr(EventPublisher, "sequence", refuse)
    response = await send_call(client, org.key, task_body(org))
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert response.json()["message"] == "Call events are temporarily unavailable."
    await assert_nothing_dispatched(container, org)
    assert await holds_of(container, org.org_id) == []


async def test_a_broadcast_failure_after_commit_still_succeeds(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_self: EventPublisher, event: Any) -> None:
        raise EventPublishError(event, "stream")

    monkeypatch.setattr(EventPublisher, "broadcast", refuse)
    stream = json_log_stream()
    response = await send_call(client, org.key, task_body(org))
    assert response.status_code == 200, response.text
    assert len(await calls_of(container, org.org_id)) == 1
    assert len(await events_of(container, org.org_id)) == 1
    assert len(container.call_starter.started) == 1
    failures = [e for e in log_events(stream) if e["event"] == "calls.event_broadcast_failed"]
    assert [(e["event_type"], e["call_id"]) for e in failures] == [
        ("call.queued", response.json()["call_id"])
    ]


async def assert_start_failed(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    """A call whose workflow start fails answers 503 and leaves a ``failed`` row."""
    response = await send_call(client, org.key, task_body(org))
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    [call] = await calls_of(container, org.org_id)
    assert call.status == "failed"


async def test_the_start_failure_is_recorded_even_when_redis_refuses_the_error_event(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    container.call_starter.fail = CallStartError("temporal down")
    original = EventPublisher.sequence

    async def refuse_errors(self: EventPublisher, event: Any) -> Any:
        if event.type.value == "call.error":
            raise EventPublishError(event, "seq")
        return await original(self, event)

    monkeypatch.setattr(EventPublisher, "sequence", refuse_errors)
    await assert_start_failed(client, container, org)
    assert [e.message for e in await events_of(container, org.org_id)] == ["Call successfully queued."]
    assert await stream_types(container) == ["call.queued"]


async def test_unknown_body_fields_are_ignored(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    response = await send_call(client, org.key, task_body(org, not_a_parameter=1))
    assert response.status_code == 200, response.text
    assert len(await calls_of(container, org.org_id)) == 1


async def test_a_scheduled_call_whose_start_fails_is_failed(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg
) -> None:
    container.call_starter.fail = CallStartError("temporal down")
    start = container.clock.now() + timedelta(days=1)
    body = task_body(org, start_time=start.strftime("%Y-%m-%d %H:%M:%S +00:00"))
    assert error_of(await send_call(client, org.key, body))[:2] == (503, "SERVICE_UNAVAILABLE")
    [call] = await calls_of(container, org.org_id)
    assert (call.status, call.queue_status, call.credit_hold_id) == ("failed", "call_error", None)
    assert await holds_of(container, org.org_id) == []
    assert [e.seq for e in await events_of(container, org.org_id)] == [1, 2]


@dataclass
class MoveOnThenFail:
    """Moves the call on (as a started workflow or a concurrent stop would) and then fails the start."""

    container: SessionContainer
    status: CallStatus

    async def start(self, call: CallWorkflowInput) -> None:
        async with self.container.database.tenant_session(call.org_id) as session:
            await CallRepo(session, call.org_id).transition(call.call_id, self.status)
        raise CallStartError("start timed out")  # noqa: TRY003 - test double


@pytest.mark.parametrize("status", ["dialing", "canceled"])
async def test_a_start_failure_on_a_call_that_moved_on_is_left_to_its_owner(
    client: httpx.AsyncClient,
    container: SessionContainer,
    org: CallableOrg,
    monkeypatch: pytest.MonkeyPatch,
    status: CallStatus,
) -> None:
    monkeypatch.setattr(container, "call_starter", MoveOnThenFail(container, status))
    stream = json_log_stream()
    response = await send_call(client, org.key, task_body(org), **{IDEMPOTENCY: f"moved-{status}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["message"]) == ("success", "Call successfully queued.")
    [call] = await calls_of(container, org.org_id)
    assert (str(call.id), call.status, call.error_message) == (body["call_id"], status, None)
    [held] = await holds_of(container, org.org_id)
    assert held.status == "held"
    assert await daily_count(container, org.org_id) == 1
    assert await container.call_limiter.active_calls(org.org_id) == 1
    assert [e.message for e in await events_of(container, org.org_id)] == ["Call successfully queued."]
    assert await stream_types(container) == ["call.queued"]
    unrecorded = [e for e in log_events(stream) if e["event"] == "calls.start_failure_unrecorded"]
    assert [(e["status"], e["call_id"]) for e in unrecorded] == [(status, body["call_id"])]


async def test_a_redis_outage_during_the_undo_keeps_the_service_unavailable_answer(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_self: EventPublisher, event: Any) -> Any:
        raise EventPublishError(event, "seq")

    async def redis_down(_org_id: uuid.UUID, _call_id: uuid.UUID) -> None:
        raise RedisError("connection refused")  # noqa: TRY003 - test double

    monkeypatch.setattr(EventPublisher, "sequence", refuse)
    monkeypatch.setattr(container.call_limiter, "release_call", redis_down)
    stream = json_log_stream()
    response = await send_call(client, org.key, task_body(org))
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert response.json()["message"] == "Call events are temporarily unavailable."
    assert await calls_of(container, org.org_id) == []
    failures = [e for e in log_events(stream) if e["event"] == "calls.counter_release_failed"]
    assert [e["error"] for e in failures] == ["RedisError"]


async def test_a_redis_outage_during_the_start_failure_undo_still_fails_the_call(
    client: httpx.AsyncClient, container: SessionContainer, org: CallableOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def redis_down(_org_id: uuid.UUID, _call_id: uuid.UUID) -> None:
        raise OSError("connection reset")  # noqa: TRY003 - test double

    container.call_starter.fail = CallStartError("temporal down")
    monkeypatch.setattr(container.call_limiter, "release_call", redis_down)
    stream = json_log_stream()
    await assert_start_failed(client, container, org)
    failures = [e for e in log_events(stream) if e["event"] == "calls.counter_release_failed"]
    assert [e["error"] for e in failures] == ["OSError"]


async def test_a_key_of_an_org_without_a_row_is_not_found(
    client: httpx.AsyncClient, container: SessionContainer
) -> None:
    key, _ = container.store.issue()
    body = {"phone_number": random_e164(), "task": "Hello.", "voice": "Karen"}
    assert error_of(await send_call(client, key, body))[:2] == (404, "NOT_FOUND")
