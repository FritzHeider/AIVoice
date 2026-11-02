"""``POST /v1/calls/{call_id}/stop`` and ``POST /v1/calls/active/stop`` over a real database (specs/05 §7.2,
§7.3; Decision P05-T11c)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from call_helpers import bearer, business_hours_today
from call_rows import SeededCall, load_call, seed_call
from identity_db import IdentityDb
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from service_helpers import json_log_stream, log_events
from session_helpers import (
    SessionContainer,
    audit_rows,
    error_of,
    hold,
    session_app,
    session_client,
    signed_in,
    store_api_key,
    write_headers,
)
from sqlalchemy import select

from va_api.routers.calls_schemas import MAX_STOP_CALL_IDS
from va_api.services.call_stop import (
    CANCELED_MESSAGE,
    NOT_ACTIVE_MESSAGE,
    STOP_ALL_ROLE_MESSAGE,
    STOPPED_MESSAGE,
    CallCancelError,
)
from va_billing import keys
from va_common.ids import new_id
from va_common.time import FrozenClock
from va_db.models import CallEvent, CallStatus, CreditHold
from va_domain.control import StopControl, control_channel, parse_control
from va_events import ENDED_MESSAGE, DomainEvent, EventPublisher, EventPublishError

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

STOP_ALL = "/v1/calls/active/stop"
DOWN = "redis down"
CANCELABLE: tuple[CallStatus, ...] = ("scheduled", "queued", "dialing", "ringing")
ANSWERED: tuple[CallStatus, ...] = ("in_progress", "transferring")
ENDED: tuple[CallStatus, ...] = ("transferred", "completed", "failed", "canceled", "no_answer", "busy")


def stop_path(call_id: uuid.UUID | str) -> str:
    return f"/v1/calls/{call_id}/stop"


@pytest.fixture
def container(identity: IdentityDb) -> SessionContainer:
    """Frozen at 18:00 UTC today: inside the migration's ``call_events`` partitions."""
    return SessionContainer(identity=identity, clock=FrozenClock(business_hours_today()))


@pytest.fixture
async def client(container: SessionContainer) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(session_app(container)) as opened:
        yield opened


@pytest.fixture
async def call_org(container: SessionContainer) -> tuple[uuid.UUID, str]:
    """A fresh org (via a seeded call) and a ``*`` key of it."""
    seeded = await seed_call(container.database, status="completed")
    key, _ = await store_api_key(container, seeded.org_id)
    return seeded.org_id, key


async def a_call(
    container: SessionContainer, org_id: uuid.UUID, status: CallStatus, **values: Any
) -> SeededCall:
    return await seed_call(container.database, status=status, org_id=org_id, **values)


async def stop(client: httpx.AsyncClient, key: str, call_id: uuid.UUID | str) -> httpx.Response:
    return await client.post(stop_path(call_id), headers=bearer(key))


type StopIn = Callable[[CallStatus], Awaitable[tuple[SeededCall, httpx.Response]]]


@pytest.fixture
def stop_in(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> StopIn:
    """Seed a call of ``call_org`` in a status and stop it with the org's key."""

    async def run(status: CallStatus) -> tuple[SeededCall, httpx.Response]:
        call = await a_call(container, call_org[0], status)
        return call, await stop(client, call_org[1], call.call_id)

    return run


async def subscribed(container: SessionContainer, call_id: uuid.UUID) -> PubSub:
    pubsub = container.redis.pubsub()
    await pubsub.subscribe(control_channel(call_id))
    assert await pubsub.get_message(timeout=1.0) is not None  # the subscribe confirmation
    return pubsub


async def published(pubsub: PubSub) -> list[StopControl]:
    messages: list[StopControl] = []
    while (message := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)) is not None:
        control = parse_control(message["data"])
        assert isinstance(control, StopControl)
        messages.append(control)
    await pubsub.aclose()  # type: ignore[no-untyped-call]
    return messages


async def ended_events(container: SessionContainer, org_id: uuid.UUID, call_id: uuid.UUID) -> list[CallEvent]:
    async with container.database.tenant_session(org_id) as session:
        rows = await session.scalars(
            select(CallEvent).where(CallEvent.call_id == call_id, CallEvent.message == ENDED_MESSAGE)
        )
        return list(rows)


async def hold_status(container: SessionContainer, org_id: uuid.UUID, hold_id: uuid.UUID) -> str:
    async with container.database.tenant_session(org_id) as session:
        return (await session.get_one(CreditHold, hold_id)).status


@pytest.mark.parametrize("status", CANCELABLE)
async def test_a_call_not_answered_yet_is_canceled_through_its_workflow(
    container: SessionContainer, stop_in: StopIn, status: CallStatus
) -> None:
    call, response = await stop_in(status)
    org_id = call.org_id
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success", "message": CANCELED_MESSAGE}
    assert container.call_canceller.cancelled == [call.call_id]
    assert (await load_call(container.database, call)).status == status  # the workflow records the cancel
    rows = await audit_rows(container.identity, org_id, "call.stop")
    assert [(row.resource_id, row.changes) for row in rows] == [
        (str(call.call_id), {"before": {"status": status}, "after": {"action": "cancel"}})
    ]


@pytest.mark.parametrize("status", ANSWERED)
async def test_an_answered_call_gets_a_stop_control_message(
    container: SessionContainer,
    client: httpx.AsyncClient,
    call_org: tuple[uuid.UUID, str],
    status: CallStatus,
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, status)
    pubsub = await subscribed(container, call.call_id)
    response = await stop(client, key, call.call_id)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success", "message": STOPPED_MESSAGE}
    messages = await published(pubsub)
    assert [(m.type, m.reason) for m in messages] == [("stop", "api")]
    assert container.call_canceller.cancelled == []
    rows = await audit_rows(container.identity, org_id, "call.stop")
    assert [row.changes["after"] for row in rows] == [{"action": "stop"}]


async def test_a_stop_nobody_hears_still_succeeds_and_is_logged(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, "in_progress")
    stream = json_log_stream()
    response = await stop(client, key, call.call_id)
    assert response.status_code == 200, response.text
    assert "calls.stop_unheard" in {event["event"] for event in log_events(stream)}


@pytest.mark.parametrize("status", ENDED)
async def test_an_ended_or_transferred_call_is_not_active(
    container: SessionContainer, stop_in: StopIn, status: CallStatus
) -> None:
    call, response = await stop_in(status)
    assert error_of(response)[:2] == (409, "CALL_NOT_ACTIVE")
    assert response.json()["message"] == NOT_ACTIVE_MESSAGE
    assert await audit_rows(container.identity, call.org_id, "call.stop") == []


@pytest.mark.parametrize("value", ["not-a-uuid", "123", str(uuid.UUID(int=7))])
async def test_unknown_or_malformed_ids_are_call_not_found(
    client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str], value: str
) -> None:
    _, key = call_org
    assert error_of(await stop(client, key, value))[:2] == (404, "CALL_NOT_FOUND")


async def test_another_orgs_call_is_call_not_found(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    _, key = call_org
    foreign = await seed_call(container.database, status="in_progress")
    assert error_of(await stop(client, key, foreign.call_id))[:2] == (404, "CALL_NOT_FOUND")
    assert container.call_canceller.cancelled == []


async def test_a_queued_call_without_a_workflow_is_canceled_here(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    hold_id = await hold(container, org_id, "2")
    call = await a_call(container, org_id, "queued", credit_hold_id=hold_id)
    await container.redis.zadd(keys.concurrency_key(org_id), {str(call.call_id): 1})
    container.call_canceller.missing = True
    response = await stop(client, key, call.call_id)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == CANCELED_MESSAGE
    row = await load_call(container.database, call)
    assert (row.status, row.end_reason, row.ended_by, row.disposition_tag) == (
        "canceled",
        "canceled",
        "api",
        "CANCELED",
    )
    assert row.ended_at == container.clock.now()
    assert await hold_status(container, org_id, hold_id) == "released"
    events = await ended_events(container, org_id, call.call_id)
    assert [(event.data["end_reason"], event.data["ended_by"]) for event in events] == [("canceled", "api")]
    assert await container.redis.zscore(keys.concurrency_key(org_id), str(call.call_id)) is None
    assert len(await audit_rows(container.identity, org_id, "call.stop")) == 1


async def test_the_fallback_cancel_survives_event_and_slot_failures(
    container: SessionContainer,
    client: httpx.AsyncClient,
    call_org: tuple[uuid.UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, "scheduled")
    container.call_canceller.missing = True

    async def no_sequence(_self: EventPublisher, event: DomainEvent) -> DomainEvent:
        raise EventPublishError(event, "seq")

    async def no_release(_org_id: uuid.UUID, _call_id: uuid.UUID) -> None:
        raise RedisError(DOWN)

    monkeypatch.setattr(EventPublisher, "sequence", no_sequence)
    monkeypatch.setattr(container.call_limiter, "release_call", no_release)
    stream = json_log_stream()
    response = await stop(client, key, call.call_id)
    assert response.status_code == 200, response.text
    assert (await load_call(container.database, call)).status == "canceled"
    assert await ended_events(container, org_id, call.call_id) == []
    assert "calls.slot_release_failed" in {event["event"] for event in log_events(stream)}


async def test_a_ringing_call_without_a_workflow_is_unavailable(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, "ringing")
    container.call_canceller.missing = True
    assert error_of(await stop(client, key, call.call_id))[:2] == (503, "SERVICE_UNAVAILABLE")
    assert (await load_call(container.database, call)).status == "ringing"
    assert await audit_rows(container.identity, org_id, "call.stop") == []


async def test_a_signal_failure_is_unavailable_and_unaudited(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, "queued")
    container.call_canceller.fail = CallCancelError("temporal down")
    assert error_of(await stop(client, key, call.call_id))[:2] == (503, "SERVICE_UNAVAILABLE")
    assert await audit_rows(container.identity, org_id, "call.stop") == []


async def test_a_publish_failure_is_unavailable_and_unaudited(
    container: SessionContainer,
    client: httpx.AsyncClient,
    call_org: tuple[uuid.UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, "in_progress")

    async def broken(_channel: str, _message: str) -> int:
        raise RedisError(DOWN)

    monkeypatch.setattr(container.redis, "publish", broken)
    assert error_of(await stop(client, key, call.call_id))[:2] == (503, "SERVICE_UNAVAILABLE")
    assert await audit_rows(container.identity, org_id, "call.stop") == []


async def test_a_key_without_calls_write_is_forbidden(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, _ = call_org
    call = await a_call(container, org_id, "in_progress")
    key, _ = await store_api_key(container, org_id, ("calls:read",))
    assert error_of(await stop(client, key, call.call_id))[:2] == (403, "FORBIDDEN")
    assert error_of(await client.post(STOP_ALL, headers=bearer(key)))[:2] == (403, "FORBIDDEN")


async def session_post(client: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
    return await client.post(path, headers=write_headers(client), **kwargs)


async def test_session_roles_for_single_stop(container: SessionContainer, identity: IdentityDb) -> None:
    async with session_client(session_app(container)) as developer_client:
        developer = await signed_in(developer_client, identity, container, "developer")
        call = await a_call(container, developer.org.id, "in_progress")
        response = await session_post(developer_client, stop_path(call.call_id))
        assert response.status_code == 200, response.text
    async with session_client(session_app(container)) as analyst_client:
        analyst = await signed_in(analyst_client, identity, container, "analyst")
        call = await a_call(container, analyst.org.id, "in_progress")
        assert error_of(await session_post(analyst_client, stop_path(call.call_id)))[:2] == (403, "FORBIDDEN")


async def test_stop_all_stops_every_stoppable_call(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    queued = await a_call(container, org_id, "queued")
    live = await a_call(container, org_id, "in_progress")
    await a_call(container, org_id, "transferred")
    await a_call(container, org_id, "busy")
    pubsub = await subscribed(container, live.call_id)
    response = await client.post(STOP_ALL, headers=bearer(key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "status": "success",
        "message": "Stopped 2 calls.",
        "stopped": 2,
        "call_ids": [str(queued.call_id), str(live.call_id)],
    }
    assert container.call_canceller.cancelled == [queued.call_id]
    assert len(await published(pubsub)) == 1
    rows = await audit_rows(container.identity, org_id, "call.stop")
    assert sorted(str(row.resource_id) for row in rows) == sorted([str(queued.call_id), str(live.call_id)])


async def test_stop_all_with_call_ids_restricts_and_ignores_foreign_ids(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    wanted = await a_call(container, org_id, "scheduled")
    await a_call(container, org_id, "scheduled")
    foreign = await seed_call(container.database, status="scheduled")
    ids = [str(wanted.call_id), str(wanted.call_id), str(foreign.call_id), str(new_id())]
    response = await client.post(STOP_ALL, headers=bearer(key), json={"call_ids": ids})
    assert response.status_code == 200, response.text
    assert response.json()["call_ids"] == [str(wanted.call_id)]
    assert container.call_canceller.cancelled == [wanted.call_id]


async def test_stop_all_with_an_empty_body_object_stops_everything(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    call = await a_call(container, org_id, "queued")
    response = await client.post(STOP_ALL, headers=bearer(key), json={})
    assert response.json()["call_ids"] == [str(call.call_id)]


async def test_stop_all_with_nothing_to_stop(
    client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    _, key = call_org
    response = await client.post(STOP_ALL, headers=bearer(key))
    assert response.json() == {
        "status": "success",
        "message": "Stopped 0 calls.",
        "stopped": 0,
        "call_ids": [],
    }


async def test_stop_all_rejects_too_many_ids_and_unknown_fields(
    client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    _, key = call_org
    too_many = [str(new_id()) for _ in range(MAX_STOP_CALL_IDS + 1)]
    response = await client.post(STOP_ALL, headers=bearer(key), json={"call_ids": too_many})
    assert error_of(response)[:2] == (400, "INVALID_INPUT")
    response = await client.post(STOP_ALL, headers=bearer(key), json={"ids": []})
    assert error_of(response)[:2] == (400, "INVALID_INPUT")


async def test_stop_all_session_roles(container: SessionContainer, identity: IdentityDb) -> None:
    async with session_client(session_app(container)) as developer_client:
        await signed_in(developer_client, identity, container, "developer")
        response = await session_post(developer_client, STOP_ALL)
        assert error_of(response)[:2] == (403, "FORBIDDEN")
        assert response.json()["message"] == STOP_ALL_ROLE_MESSAGE
    async with session_client(session_app(container)) as admin_client:
        admin = await signed_in(admin_client, identity, container, "admin")
        call = await a_call(container, admin.org.id, "queued")
        response = await session_post(admin_client, STOP_ALL)
        assert response.status_code == 200, response.text
        assert response.json()["call_ids"] == [str(call.call_id)]


async def test_stop_all_fails_when_nothing_could_be_stopped(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    await a_call(container, org_id, "queued")
    container.call_canceller.fail = CallCancelError("temporal down")
    stream = json_log_stream()
    response = await client.post(STOP_ALL, headers=bearer(key))
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert "calls.stop_failed" in {event["event"] for event in log_events(stream)}


async def test_stop_all_reports_only_the_calls_it_stopped(
    container: SessionContainer, client: httpx.AsyncClient, call_org: tuple[uuid.UUID, str]
) -> None:
    org_id, key = call_org
    await a_call(container, org_id, "ringing")  # no workflow: unavailable
    queued = await a_call(container, org_id, "queued")  # no workflow: canceled here
    container.call_canceller.missing = True
    response = await client.post(STOP_ALL, headers=bearer(key))
    assert response.status_code == 200, response.text
    assert response.json()["call_ids"] == [str(queued.call_id)]
