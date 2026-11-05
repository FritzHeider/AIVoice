"""The executor-backed system tools ``send_sms`` and ``schedule_callback`` on a real database (specs/13 §6;
Decision P08-T02b2)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import fakeredis
import pytest
from call_rows import SeededCall, seed_call
from media_helpers import BrokenDatabase, call_context
from service_helpers import json_log_stream, log_events
from sms_fakes import FakeTelnyx
from sms_rows import add_sms_number, random_e164
from sqlalchemy import select
from tenant_rows import OrgFactory, open_database

from va_common.ids import new_id
from va_common.time import SystemClock, utcnow
from va_db.models import Call, CallEvent, Message
from va_db.session import Database
from va_domain.call_context import CallContext
from va_domain.call_workflow import CallWorkflowInput
from va_events import DbWebhookRouter, EventPublisher
from va_llm.models import ToolCall
from va_media.call_sms import OutboundSms
from va_media.call_system_tools import (
    CALLBACK_START_FAILED,
    CallbackDeps,
    CallbackScheduler,
    CallbackStartError,
    ToolSms,
    sms_capable,
)

pytestmark = pytest.mark.integration


@dataclass
class RecordingStarter:
    """Records starts; ``fail`` raises :class:`CallbackStartError`, ``on_start`` runs before returning."""

    started: list[CallWorkflowInput] = field(default_factory=list)
    fail: bool = False

    async def start(self, call: CallWorkflowInput) -> None:
        if self.fail:
            raise CallbackStartError("refused") from ConnectionError("down")
        self.started.append(call)


@dataclass
class RecordingToolEvents:
    sms: list[dict[str, Any]] = field(default_factory=list)

    def tool(self, data: dict[str, Any]) -> None:
        raise AssertionError(data)

    def tool_failed(self, detail: str) -> None:
        raise AssertionError(detail)

    def sms_sent(self, data: dict[str, Any]) -> None:
        self.sms.append(data)


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


async def _call(
    database: Database,
    *,
    direction: Literal["outbound", "inbound"] = "outbound",
    sms: bool = True,
    **number: Any,
) -> tuple[SeededCall, CallContext]:
    org_id = await OrgFactory(database).create()
    agent = await add_sms_number(database, org_id, **number) if sms else random_e164()
    other = random_e164()
    to_number, from_number = (agent, other) if direction == "inbound" else (other, agent)
    seeded = await seed_call(
        database,
        org_id=org_id,
        status="in_progress",
        direction=direction,
        to_number=to_number,
        from_number=from_number,
        config={"task": "Sell", "timezone": "America/New_York"},
    )
    context = call_context(
        seeded.call_id, org_id=org_id, direction=direction, timezone="America/New_York"
    ).model_copy(update={"to_number": to_number, "from_number": from_number})
    return seeded, context


def _sender(database: Database, telnyx: FakeTelnyx) -> OutboundSms:
    return OutboundSms(
        database=database,
        telnyx=telnyx,
        platform_profile_id="mp-platform",
        edge_public_host="edge.test:8100",
        clock=SystemClock(),
        log_prefix="media.tool_sms",
    )


def _tool_call(name: str, call_id: str = "call_1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments_json=json.dumps(arguments))


async def _messages(database: Database, org_id: uuid.UUID) -> list[Message]:
    async with database.tenant_session(org_id) as session:
        return list(await session.scalars(select(Message).where(Message.org_id == org_id)))


async def _children(database: Database, seeded: SeededCall) -> list[Call]:
    async with database.tenant_session(seeded.org_id) as session:
        return list(await session.scalars(select(Call).where(Call.parent_call_id == seeded.call_id)))


async def _events(database: Database, seeded: SeededCall, call_id: uuid.UUID) -> list[CallEvent]:
    async with database.tenant_session(seeded.org_id) as session:
        statement = select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.seq)
        return list(await session.scalars(statement))


# sms_capable


async def test_an_active_sms_number_is_sms_capable(database: Database) -> None:
    _, context = await _call(database)
    assert await sms_capable(database, context)


@pytest.mark.parametrize("number", [{"capabilities": ["voice"]}, {"status": "released"}])
async def test_a_voice_only_or_inactive_number_is_not_sms_capable(
    database: Database, number: dict[str, Any]
) -> None:
    _, context = await _call(database, **number)
    assert not await sms_capable(database, context)


async def test_an_inbound_call_checks_the_called_number(database: Database) -> None:
    _, context = await _call(database, direction="inbound")
    assert await sms_capable(database, context)


async def test_an_unreadable_capability_is_not_sms_capable(database: Database) -> None:
    _, context = await _call(database)
    stream = json_log_stream()
    assert not await sms_capable(BrokenDatabase(), context)  # type: ignore[arg-type]
    assert any(e["event"] == "media.sms_capability_unavailable" for e in log_events(stream))


# send_sms


async def test_send_sms_texts_the_other_party_from_the_agent_number(database: Database) -> None:
    seeded, context = await _call(database)
    telnyx = FakeTelnyx()
    events = RecordingToolEvents()
    tool = ToolSms(_sender(database, telnyx), context, events)

    outcome = await tool.run(_tool_call("send_sms", message="  Your code is 42.  "))

    assert (outcome.failure, json.loads(outcome.content)) == (None, {"status": "sent", "segments": 2})
    [sent] = telnyx.sent
    assert (sent.from_, sent.to, sent.text) == (context.from_number, context.to_number, "Your code is 42.")
    [message] = await _messages(database, seeded.org_id)
    [data] = events.sms
    assert data == {
        "message_id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "segments": 2,
        "source": "tool",
    }


@pytest.mark.parametrize("tool_call_id", ["call_0", ""])
async def test_every_send_sms_run_sends_even_with_a_reused_tool_call_id(
    database: Database, tool_call_id: str
) -> None:
    seeded, context = await _call(database)
    telnyx = FakeTelnyx()
    tool = ToolSms(_sender(database, telnyx), context, None)
    first = await tool.run(_tool_call("send_sms", tool_call_id, message="Hi"))
    again = await tool.run(_tool_call("send_sms", tool_call_id, message="See you then"))
    assert [json.loads(o.content) for o in (first, again)] == [{"status": "sent", "segments": 2}] * 2
    assert [m.text for m in telnyx.sent] == ["Hi", "See you then"]
    assert len({m.id for m in await _messages(database, seeded.org_id)}) == 2


async def test_send_sms_from_a_number_without_sms_fails(database: Database) -> None:
    _, context = await _call(database, sms=False)
    telnyx = FakeTelnyx()
    tool = ToolSms(_sender(database, telnyx), context, None)
    outcome = await tool.run(_tool_call("send_sms", message="Hi"))
    assert outcome.failure == "sms_not_enabled"
    assert json.loads(outcome.content) == {"error": "sms_failed", "reason": "sms_not_enabled"}
    assert telnyx.sent == []


async def test_a_blank_message_is_refused(database: Database) -> None:
    _, context = await _call(database)
    telnyx = FakeTelnyx()
    outcome = await ToolSms(_sender(database, telnyx), context, None).run(_tool_call("send_sms", message=" "))
    assert outcome.failure == "blank_message"
    assert telnyx.sent == []


# schedule_callback


def _scheduler(database: Database, redis: Any, starter: RecordingStarter, context: CallContext) -> Any:
    publisher = EventPublisher(redis, call_events=None, router=DbWebhookRouter(database))
    deps = CallbackDeps(database=database, publisher=publisher, starter=starter, clock=SystemClock())
    return CallbackScheduler(deps, context)


async def test_schedule_callback_creates_the_child_its_event_and_its_workflow(database: Database) -> None:
    seeded, context = await _call(database, direction="inbound")
    starter = RecordingStarter()
    redis = fakeredis.FakeAsyncRedis()
    when = (utcnow() + timedelta(days=1)).astimezone(ZoneInfo("America/New_York")).replace(microsecond=0)
    naive = when.replace(tzinfo=None).isoformat()

    outcome = await _scheduler(database, redis, starter, context).run(
        _tool_call("schedule_callback", datetime_iso=naive, note="Wants a quote")
    )

    child_id = uuid.UUID(json.loads(outcome.content)["scheduled_call_id"])
    assert outcome.failure is None
    assert json.loads(outcome.content) == {
        "status": "scheduled",
        "scheduled_call_id": str(child_id),
        "scheduled_for": when.isoformat(),
    }
    [child] = await _children(database, seeded)
    assert child.id == child_id
    assert (child.status, child.direction, child.to_number) == ("scheduled", "outbound", context.from_number)
    assert child.start_time == when
    assert child.config["callback_note"] == "Wants a quote"
    [event] = await _events(database, seeded, child_id)
    assert (event.event_type, event.seq) == ("call.scheduled", 1)
    assert event.data["parent_call_id"] == str(seeded.call_id)
    assert event.data["source"] == "callback"
    assert starter.started == [CallWorkflowInput(call_id=child_id, org_id=seeded.org_id, scheduled_for=when)]
    assert await redis.xlen("events:calls") == 1


@pytest.mark.parametrize("tool_call_id", ["call_0", ""])
async def test_every_callback_run_schedules_even_with_a_reused_tool_call_id(
    database: Database, tool_call_id: str
) -> None:
    seeded, context = await _call(database)
    starter = RecordingStarter()
    scheduler = _scheduler(database, fakeredis.FakeAsyncRedis(), starter, context)
    early = (utcnow() + timedelta(hours=3)).replace(microsecond=0)
    late = early + timedelta(days=1)
    first = await scheduler.run(_tool_call("schedule_callback", tool_call_id, datetime_iso=early.isoformat()))
    again = await scheduler.run(_tool_call("schedule_callback", tool_call_id, datetime_iso=late.isoformat()))
    ids = [uuid.UUID(json.loads(o.content)["scheduled_call_id"]) for o in (first, again)]
    assert ids[0] != ids[1]
    children = {child.id: child.start_time for child in await _children(database, seeded)}
    assert children == {ids[0]: early, ids[1]: late}
    assert [started.call_id for started in starter.started] == ids


@pytest.mark.parametrize(
    ("value", "reason"),
    [("next tuesday", "invalid_datetime"), ("2020-01-01T10:00:00Z", "in_the_past")],
)
async def test_an_unusable_time_schedules_nothing(database: Database, value: str, reason: str) -> None:
    seeded, context = await _call(database)
    starter = RecordingStarter()
    outcome = await _scheduler(database, fakeredis.FakeAsyncRedis(), starter, context).run(
        _tool_call("schedule_callback", datetime_iso=value)
    )
    assert outcome.failure is None
    assert json.loads(outcome.content) == {"error": "invalid_datetime", "reason": reason}
    assert await _children(database, seeded) == []
    assert starter.started == []


async def test_a_failed_start_fails_the_child(database: Database) -> None:
    seeded, context = await _call(database)
    starter = RecordingStarter(fail=True)
    when = (utcnow() + timedelta(hours=3)).isoformat()
    outcome = await _scheduler(database, fakeredis.FakeAsyncRedis(), starter, context).run(
        _tool_call("schedule_callback", datetime_iso=when)
    )
    assert outcome.failure == "workflow_start"
    assert json.loads(outcome.content) == {"error": "callback_failed"}
    [child] = await _children(database, seeded)
    assert (child.status, child.queue_status, child.end_reason, child.error_message) == (
        "failed",
        "call_error",
        "error",
        CALLBACK_START_FAILED,
    )
    events = await _events(database, seeded, child.id)
    assert [e.event_type for e in events] == ["call.scheduled", "call.error"]
    assert events[1].data == {"code": "dispatch_failed", "detail": "workflow_start"}


async def test_a_database_failure_answers_callback_failed(database: Database) -> None:
    _, context = await _call(database)
    starter = RecordingStarter()
    when = (utcnow() + timedelta(hours=3)).isoformat()
    outcome = await _scheduler(BrokenDatabase(), fakeredis.FakeAsyncRedis(), starter, context).run(  # type: ignore[arg-type]
        _tool_call("schedule_callback", datetime_iso=when)
    )
    assert (outcome.failure, json.loads(outcome.content)) == ("database", {"error": "callback_failed"})
    assert starter.started == []


async def test_a_parent_of_another_org_schedules_nothing(database: Database) -> None:
    seeded, context = await _call(database)
    other = context.model_copy(update={"org_id": await OrgFactory(database).create(), "call_id": new_id()})
    starter = RecordingStarter()
    when = (utcnow() + timedelta(hours=3)).isoformat()
    outcome = await _scheduler(database, fakeredis.FakeAsyncRedis(), starter, other).run(
        _tool_call("schedule_callback", datetime_iso=when)
    )
    assert outcome.failure == "database"
    assert await _children(database, seeded) == []
