"""``InboundAccept`` on Postgres with FakeTelnyx (specs/06-telephony-telnyx.md §7 steps 8-11, Decision
P05-T12c).

An accepted call becomes an inbound ``ringing`` call (row, primary leg, contact, ``call.ringing`` event,
context in Redis), its ``CallWorkflow`` is started in inbound mode, and the leg is answered with the stream
and ``client_state``. Failures after the commit record the call ``failed`` and refuse it through the router.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import secrets
import uuid
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from accept_helpers import MEDIA_HOST, AcceptRig, GatedClock, accept_rig, answer_failing_factory, broken_redis
from routing_helpers import CALLER, FALLBACK, random_number
from sqlalchemy import select
from structlog.testing import capture_logs

from va_billing import keys
from va_common.ids import new_id
from va_common.stream_tokens import verify_stream_token
from va_db.call_context import CallContextStore
from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.repositories import CallRepo
from va_db.session import Database
from va_domain.call_workflow import CallWorkflowInput
from va_domain.numbers import MAINTENANCE_MESSAGE_KEY
from va_edge.accept import ACCEPT_BUDGET_S, ACCEPT_FAILED_MESSAGE, CONTEXT_BUDGET_S, RINGING_MESSAGE
from va_edge.processor import MAX_ATTEMPTS
from va_events import DomainEvent, EventPublisher, EventPublishError
from va_telnyx.client_state import decode

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PROMPT = "Help the caller with their order."


async def routed_with_logs(
    r: AcceptRig, error: str | None
) -> tuple[uuid.UUID, str, list[MutableMapping[str, Any]]]:
    """Ring a fresh prompt number of a fresh org, route the call, and check the stored ``process_error``:
    ``(org, call_control_id, captured logs)``."""
    org = await r.org()
    ccid, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
    with capture_logs() as logs:
        assert (await r.route(event)).process_error == error
    return org, ccid, logs


def answer_body(r: AcceptRig, ccid: str) -> dict[str, Any]:
    [answer] = [c for c in r.rig.commands(ccid) if c.action == "answer"]
    return answer.body


async def test_an_accepted_call_is_created_started_and_answered(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        to = await r.rig.number(org, {"prompt": PROMPT, "max_duration": 12})
        ccid, event = await r.rig.ring(to)

        row = await r.route(event)

        assert (row.process_error, row.process_attempts) == (None, 1)
        [call] = await r.calls(org)
        assert (call.direction, call.status, call.queue_status) == ("inbound", "ringing", "started")
        assert (call.to_number, call.from_number, call.runner_type) == (to, CALLER, "task")
        assert call.workflow_id == f"call-{call.id}"
        assert call.config["task"] == PROMPT
        assert (call.config["phone_number"], call.config["from_number"]) == (CALLER, to)
        assert call.config["max_duration_min"] == 12
        assert call.config["pricing"]["connected_rate_micros_per_min"] > 0
        assert MAINTENANCE_MESSAGE_KEY not in call.config
        [contact] = await r.contacts(org)
        assert (call.contact_id, contact.phone_e164) == (contact.id, CALLER)
        [leg] = await r.legs(org, call.id)
        assert (leg.role, leg.call_control_id, leg.state, leg.telnyx_account_id) == (
            "primary",
            ccid,
            "initiated",
            None,
        )
        [ringing] = await r.events(org, call.id)
        assert (ringing.seq, ringing.message, ringing.data) == (1, RINGING_MESSAGE, {"direction": "inbound"})

        context = await CallContextStore(r.rig.redis).load(call.id)
        assert context is not None
        assert (context.direction, context.to_number, context.from_number) == ("inbound", to, CALLER)
        assert (context.contact_id, context.maintenance_message) == (contact.id, None)

        [started] = r.starter.started
        assert (started.call_id, started.org_id, started.direction, started.max_duration_s) == (
            call.id,
            org,
            "inbound",
            720,
        )

        body = answer_body(r, ccid)
        assert body["command_id"] == f"answer-{call.id}"
        state = decode(body["client_state"])
        assert (state.call_id, state.leg, state.org_id) == (call.id, "primary", org)
        url = urlsplit(body["stream_url"])
        assert (url.scheme, url.netloc, url.path) == ("ws", MEDIA_HOST, "/ws/telnyx")
        query = parse_qs(url.query)
        assert query["call_id"] == [str(call.id)]
        claims = verify_stream_token(
            query["token"][0], secrets=[r.secret], now=datetime.now(UTC), call_id=call.id
        )
        assert claims.call_id == call.id
        assert (body["stream_codec"], body["stream_bidirectional_mode"]) == ("PCMU", "rtp")
        assert "record" not in body
        assert r.rig.commands(ccid)[0].authorization == "Bearer sim-telnyx-key"
        assert (await r.rig.leg(ccid))["answered_at"] is not None

        assert await r.rig.redis.zscore(keys.concurrency_key(org), str(call.id)) is not None
        assert r.rig.counted("answered") == 1
        assert r.answered() == 1


async def _org_endpoint(database: Database, org_id: uuid.UUID) -> str:
    url = f"https://org-{secrets.token_hex(4)}.hooks.test/events"
    async with database.tenant_session(org_id) as session:
        session.add(WebhookEndpoint(id=new_id(), org_id=org_id, url=url, event_types=["call.*"]))
    return url


async def _delivered(database: Database, org_id: uuid.UUID) -> list[tuple[str, str, str]]:
    async with database.tenant_session(org_id) as session:
        rows = await session.execute(
            select(WebhookDelivery.event_type, WebhookDelivery.url, WebhookDelivery.format).order_by(
                WebhookDelivery.created_at, WebhookDelivery.event_type
            )
        )
        return [(event_type, url, fmt) for event_type, url, fmt in rows]


async def test_the_ringing_event_reaches_the_orgs_endpoints(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        url = await _org_endpoint(database, org)
        _, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        assert (await r.route(event)).process_error is None
        [call] = await r.calls(org)
        [ringing] = await r.events(org, call.id)
        assert await _delivered(database, org) == [("call.ringing", url, "envelope")]
        async with database.tenant_session(org) as session:
            delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None
        assert (delivery.event_id, delivery.call_id) == (f"evt_{ringing.id.hex}", call.id)


async def test_a_failed_accept_delivers_ringing_and_ended(database: Database) -> None:
    async with accept_rig(database) as r:
        r.starter.error = ConnectionError("temporal down")
        org = await r.org()
        url = await _org_endpoint(database, org)
        _, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        assert (await r.route(event)).process_error == "accept_failed"
        assert sorted(await _delivered(database, org)) == [
            ("call.ended", url, "envelope"),
            ("call.ringing", url, "envelope"),
        ]


async def test_the_caller_contact_is_reused_and_anonymous_callers_get_none(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        to = await r.rig.number(org, {"prompt": PROMPT})
        for caller in (CALLER, CALLER, "anonymous"):
            _, event = await r.rig.ring(to)
            if caller == "anonymous":
                # FakeTelnyx only places calls from E.164 numbers: rewrite the stored payload's caller.
                envelope = copy.deepcopy(event.envelope)
                envelope["data"]["payload"]["from"] = caller
                event = dataclasses.replace(event, envelope=envelope)
            assert (await r.route(event)).process_error is None
        calls = await r.calls(org)
        [contact] = await r.contacts(org)
        assert [call.contact_id for call in calls] == [contact.id, contact.id, None]
        assert calls[2].from_number == "anonymous"


async def test_recording_and_hd_audio_reach_the_answer(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        to = await r.rig.number(org, {"prompt": PROMPT, "record": True, "hd_audio": True})
        ccid, event = await r.rig.ring(to, time_limit_s=30)
        await r.route(event)
        body = answer_body(r, ccid)
        assert (body["record"], body["record_channels"], body["record_format"]) == (
            "record-from-answer",
            "dual",
            "wav",
        )
        assert (body["stream_codec"], body["stream_bidirectional_sampling_rate"]) == ("L16", 16000)


async def test_a_byo_number_is_answered_through_its_own_account(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        account = await r.rig.byo_account(org)
        to = await r.rig.number(org, {"prompt": PROMPT}, account=account)
        ccid, event = await r.rig.ring(to)
        assert (await r.route(event)).process_error is None
        [answer] = [c for c in r.rig.commands(ccid) if c.action == "answer"]
        assert answer.authorization == f"Bearer {account[1]}"
        [call] = await r.calls(org)
        [leg] = await r.legs(org, call.id)
        assert leg.telnyx_account_id == account[0]


async def test_a_maintenance_accept_carries_the_message(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        message = "We are closed for maintenance, please call back later."
        to = await r.rig.number(org, {"prompt": PROMPT, "maintenance": {"enabled": True, "message": message}})
        ccid, event = await r.rig.ring(to)
        assert (await r.route(event)).process_error is None
        [call] = await r.calls(org)
        assert call.config[MAINTENANCE_MESSAGE_KEY] == message
        context = await CallContextStore(r.rig.redis).load(call.id)
        assert context is not None
        assert context.maintenance_message == message
        assert [c.action for c in r.rig.commands(ccid)] == ["answer"]


@pytest.mark.parametrize("fallback", [None, FALLBACK])
async def test_a_resolution_failure_commits_nothing_and_refuses(
    database: Database, fallback: str | None
) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        config: dict[str, Any] = {"pathway_id": str(uuid.uuid4())}
        if fallback is not None:
            config["fallback_number"] = fallback
        ccid, event = await r.rig.ring(await r.rig.number(org, config))
        row = await r.route(event)
        assert (row.process_error, row.process_attempts) == ("accept_failed", MAX_ATTEMPTS)
        assert await r.calls(org) == []
        assert await r.contacts(org) == []
        assert r.starter.started == []
        assert [c.action for c in r.rig.commands(ccid)] == ["reject" if fallback is None else "transfer"]
        assert await r.rig.redis.zcard(keys.concurrency_key(org)) == 0


async def assert_failed(r: AcceptRig, org: uuid.UUID) -> None:
    [call] = await r.calls(org)
    assert (call.status, call.end_reason, call.ended_by, call.error_message) == (
        "failed",
        "error",
        "error",
        ACCEPT_FAILED_MESSAGE,
    )
    assert call.queue_status == "call_error"
    ringing, ended = await r.events(org, call.id)
    assert ringing.message == RINGING_MESSAGE
    assert ended.data == {"end_reason": "error", "ended_by": "error"}
    assert await r.rig.redis.zcard(keys.concurrency_key(org)) == 0


async def test_a_failed_workflow_start_fails_the_call_and_refuses(database: Database) -> None:
    async with accept_rig(database) as r:
        r.starter.error = ConnectionError("temporal down")
        org = await r.org()
        ccid, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        row = await r.route(event)
        assert row.process_error == "accept_failed"
        await assert_failed(r, org)
        assert r.starter.terminated == []
        assert [c.action for c in r.rig.commands(ccid)] == ["reject"]


async def test_a_failed_answer_terminates_the_workflow_and_falls_back(database: Database) -> None:
    async with accept_rig(database, telnyx=answer_failing_factory) as r:
        org = await r.org()
        to = await r.rig.number(org, {"prompt": PROMPT, "fallback_number": FALLBACK})
        ccid, event = await r.rig.ring(to)
        row = await r.route(event)
        assert row.process_error == "accept_failed"
        await assert_failed(r, org)
        [call] = await r.calls(org)
        assert r.starter.terminated == [(call.id, "inbound_accept_failed")]
        assert [c.action for c in r.rig.commands(ccid)] == ["transfer"]


async def test_cleanup_errors_never_mask_the_refusal(database: Database) -> None:
    async with accept_rig(database, telnyx=answer_failing_factory) as r:
        r.starter.terminate_error = ConnectionError("temporal down")
        org = await r.org()
        ccid, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        with capture_logs() as logs:
            row = await r.route(event)
        assert row.process_error == "accept_failed"
        assert [log["step"] for log in logs if log["event"] == "telnyx.inbound_cleanup_failed"] == [
            "terminate"
        ]
        assert [c.action for c in r.rig.commands(ccid)] == ["reject"]


async def test_the_accept_budget_cancels_a_hanging_start_and_refuses(database: Database) -> None:
    clock = GatedClock()
    async with accept_rig(database, clock=clock) as r:
        r.starter.hang = True
        org = await r.org()
        ccid, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        routing = asyncio.ensure_future(r.route(event))
        await r.starter.entered.wait()
        clock.fire(ACCEPT_BUDGET_S)
        row = await routing
        assert row.process_error == "accept_failed"
        await assert_failed(r, org)
        assert r.starter.terminated == []
        assert [c.action for c in r.rig.commands(ccid)] == ["reject"]


async def test_a_context_store_failure_still_answers(database: Database) -> None:
    async with accept_rig(database, contexts_redis=lambda _rig: broken_redis()) as r:
        org, ccid, logs = await routed_with_logs(r, None)
        assert [log["reason"] for log in logs if log["event"] == "telnyx.inbound_context_skipped"] == [
            "ConnectionError"
        ]
        assert [c.action for c in r.rig.commands(ccid)] == ["answer"]
        [call] = await r.calls(org)
        assert call.status == "ringing"


async def test_a_caller_gone_before_the_answer_is_not_refused(database: Database) -> None:
    async with accept_rig(database) as r:
        org = await r.org()
        ccid, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        hangup = await r.rig.raw.post(f"/sim/telnyx/calls/{ccid}/hangup", json={})
        assert hangup.status_code < 300, hangup.text
        with capture_logs() as logs:
            assert (await r.route(event)).process_error is None
        assert "telnyx.inbound_caller_gone_before_answer" in [log["event"] for log in logs]
        [call] = await r.calls(org)
        assert call.status == "ringing"
        assert r.answered() == 0
        assert [c.action for c in r.rig.commands(ccid)] == ["answer"]


async def test_the_answer_for_an_unknown_number_never_runs(database: Database) -> None:
    async with accept_rig(database) as r:
        to = random_number()
        record_id = r.rig.state.ids.uuid()
        r.rig.state.telnyx.accounts["sim-telnyx-key"].numbers[record_id] = {
            "id": record_id,
            "phone_number": to,
            "connection_id": r.rig.app_ids["sim-telnyx-key"],
            "messaging_profile_id": None,
        }
        ccid, event = await r.rig.ring(to)
        assert (await r.route(event)).process_error == "unknown_number"
        assert r.starter.started == []
        assert [c.action for c in r.rig.commands(ccid)] == ["reject"]


class _HangingRedis:
    """A Redis stand-in whose ``SET`` never answers."""

    async def set(self, *_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()


async def test_a_slow_context_store_is_skipped_after_its_budget(database: Database) -> None:
    clock = GatedClock()
    clock.fire(CONTEXT_BUDGET_S)
    async with accept_rig(database, clock=clock, contexts_redis=lambda _rig: _HangingRedis()) as r:
        _org, ccid, logs = await routed_with_logs(r, None)
        assert [log["reason"] for log in logs if log["event"] == "telnyx.inbound_context_skipped"] == [
            "timeout"
        ]
        assert [c.action for c in r.rig.commands(ccid)] == ["answer"]


async def test_a_failed_broadcast_is_logged_and_the_call_still_answered(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_self: EventPublisher, event: DomainEvent) -> None:
        raise EventPublishError(event, "stream")

    monkeypatch.setattr(EventPublisher, "broadcast", refuse)
    async with accept_rig(database) as r:
        _org, ccid, logs = await routed_with_logs(r, None)
        assert "telnyx.inbound_event_broadcast_failed" in [log["event"] for log in logs]
        assert [c.action for c in r.rig.commands(ccid)] == ["answer"]


async def test_a_call_that_moved_on_is_left_alone_by_the_cleanup(database: Database) -> None:
    async with accept_rig(database, telnyx=answer_failing_factory) as r:
        org = await r.org()
        ccid, event = await r.rig.ring(await r.rig.number(org, {"prompt": PROMPT}))
        original = r.starter.start

        async def start_then_cancel(call: CallWorkflowInput) -> None:
            await original(call)
            async with database.tenant_session(org) as session:
                await CallRepo(session, org).transition(call.call_id, "canceled", from_={"ringing"})

        r.starter.start = start_then_cancel  # type: ignore[method-assign]
        with capture_logs() as logs:
            assert (await r.route(event)).process_error == "accept_failed"
        [moved] = [log for log in logs if log["event"] == "telnyx.inbound_cleanup_moved_on"]
        assert moved["status"] == "canceled"
        [call] = await r.calls(org)
        assert (call.status, len(await r.events(org, call.id))) == ("canceled", 1)
        assert [c.action for c in r.rig.commands(ccid)] == ["reject"]


async def test_a_failing_cleanup_transition_is_logged(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError

    async with accept_rig(database) as r:
        r.starter.error = ConnectionError("temporal down")
        monkeypatch.setattr(CallRepo, "transition", broken)
        _org, ccid, logs = await routed_with_logs(r, "accept_failed")
        assert [log["step"] for log in logs if log["event"] == "telnyx.inbound_cleanup_failed"] == [
            "fail_call"
        ]
        assert [c.action for c in r.rig.commands(ccid)] == ["reject"]
