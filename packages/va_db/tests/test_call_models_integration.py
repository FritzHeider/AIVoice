"""Call models against Postgres, on the rolled-back ``scratch_session`` schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, get_args

import pytest
from model_factories import (
    ModelBuilder,
    assert_check_enforced,
    assert_enum_check_enforced,
    assert_rejected,
    flushed,
    make_org,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.base import Base
from va_db.models import (
    AnsweredBy,
    Batch,
    BatchItem,
    BatchItemStatus,
    BatchStatus,
    Call,
    CallAnalysis,
    CallAnalysisKind,
    CallDirection,
    CallEvent,
    CallEventCategory,
    CallEventLevel,
    CallLeg,
    CallLegRole,
    CallLegState,
    CallStatus,
    CallTurn,
    CallTurnRole,
    Citation,
    CitationSchema,
    Contact,
    DncEntry,
    DncSource,
    EndedBy,
    EvalRun,
    EvalRunStatus,
    EvalSetup,
    EvalSetupVersion,
    GuardRailEvent,
    RunnerType,
    Voice,
)

pytestmark = pytest.mark.integration

_OCTOBER = "call_events_2026_10"
_NOVEMBER = "call_events_2026_11"
_MONTH_BOUNDS = {
    _OCTOBER: ("2026-10-01 00:00:00+00", "2026-11-01 00:00:00+00"),
    _NOVEMBER: ("2026-11-01 00:00:00+00", "2026-12-01 00:00:00+00"),
}


async def _create_partition(session: AsyncSession, name: str) -> None:
    """A monthly ``call_events`` partition, as RetentionWorkflow creates them."""
    start, end = _MONTH_BOUNDS[name]
    await session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF call_events "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )
    )


async def _partition_of(session: AsyncSession, event: CallEvent) -> str:
    query = text("SELECT tableoid::regclass::text FROM call_events WHERE id = :id")
    return str((await session.execute(query, {"id": event.id})).scalar_one())


async def _org_id(session: AsyncSession) -> uuid.UUID:
    return (await make_org(session)).id


def _call(org_id: uuid.UUID, **values: Any) -> Call:
    values.setdefault("direction", "outbound")
    values.setdefault("status", "queued")
    values.setdefault("runner_type", "task")
    values.setdefault("config", {"task": "Confirm the appointment"})
    return Call(org_id=org_id, **values)


async def _make_call(session: AsyncSession, org_id: uuid.UUID | None = None, **values: Any) -> Call:
    return await flushed(session, _call(org_id or await _org_id(session), **values))


def _event(call: Call, **values: Any) -> CallEvent:
    values.setdefault("seq", 1)
    values.setdefault("category", "call")
    values.setdefault("message", "Call answered")
    return CallEvent(org_id=call.org_id, call_id=call.id, **values)


def _turn(call: Call, turn_index: int, role: CallTurnRole, **values: Any) -> CallTurn:
    return CallTurn(
        org_id=call.org_id,
        call_id=call.id,
        turn_index=turn_index,
        role=role,
        text_="Hello, this is Ava from Acme Dental.",
        start_ms=0,
        end_ms=1800,
        **values,
    )


def _leg(call: Call, call_control_id: str) -> CallLeg:
    return CallLeg(
        org_id=call.org_id,
        call_id=call.id,
        role="primary",
        call_control_id=call_control_id,
        state="initiated",
    )


async def _eval_setup_version(session: AsyncSession, org_id: uuid.UUID) -> EvalSetupVersion:
    setup = await flushed(session, EvalSetup(org_id=org_id, name="QA"))
    return await flushed(
        session, EvalSetupVersion(org_id=org_id, eval_setup_id=setup.id, version_number=1, criteria=[])
    )


async def _build_batch(session: AsyncSession) -> Base:
    return Batch(org_id=await _org_id(session))


async def _build_batch_item(session: AsyncSession) -> Base:
    batch = await flushed(session, Batch(org_id=await _org_id(session)))
    ordinal = (await session.execute(select(func.count()).select_from(BatchItem))).scalar_one()
    return BatchItem(org_id=batch.org_id, batch_id=batch.id, ordinal=ordinal, payload={})


async def _build_call(session: AsyncSession) -> Base:
    return _call(await _org_id(session))


async def _build_call_leg(session: AsyncSession) -> Base:
    return _leg(await _make_call(session), f"v3:{uuid.uuid4().hex}")


async def _build_call_turn(session: AsyncSession) -> Base:
    return _turn(await _make_call(session), 0, "user")


async def _build_call_event(session: AsyncSession) -> Base:
    await _create_partition(session, _OCTOBER)
    return _event(await _make_call(session), created_at=datetime(2026, 10, 27, 12, tzinfo=UTC))


async def _build_call_analysis(session: AsyncSession) -> Base:
    call = await _make_call(session)
    return CallAnalysis(
        org_id=call.org_id, call_id=call.id, kind="summary", result="Booked.", model="zai-org/GLM-5.3-Flash"
    )


async def _build_eval_run(session: AsyncSession) -> Base:
    org_id = await _org_id(session)
    version = await _eval_setup_version(session, org_id)
    return EvalRun(org_id=org_id, eval_setup_version_id=version.id, status="queued")


async def _build_dnc_entry(session: AsyncSession) -> Base:
    return DncEntry(
        org_id=await _org_id(session), phone_e164=f"+1415{uuid.uuid4().int % 10**7:07d}", source="api"
    )


async def test_call_event_lands_in_monthly_partition(scratch_session: AsyncSession) -> None:
    await _create_partition(scratch_session, _OCTOBER)
    await _create_partition(scratch_session, _NOVEMBER)
    call = await _make_call(scratch_session)

    october = await flushed(
        scratch_session, _event(call, seq=1, created_at=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC))
    )
    november = await flushed(
        scratch_session, _event(call, seq=2, created_at=datetime(2026, 11, 1, tzinfo=UTC))
    )
    assert await _partition_of(scratch_session, october) == _OCTOBER
    assert await _partition_of(scratch_session, november) == _NOVEMBER
    assert (october.level, october.data) == ("info", {})

    with pytest.raises(DBAPIError, match='no partition of relation "call_events" found for row'):
        async with scratch_session.begin_nested():
            await flushed(scratch_session, _event(call, seq=3, created_at=datetime(2026, 12, 1, tzinfo=UTC)))

    ordered = select(CallEvent.seq).where(CallEvent.call_id == call.id).order_by(CallEvent.seq)
    assert (await scratch_session.execute(ordered)).scalars().all() == [1, 2]


async def test_call_event_created_at_comes_back_from_the_server_default(
    scratch_session: AsyncSession,
) -> None:
    await scratch_session.execute(
        text(
            "CREATE TABLE call_events_current PARTITION OF call_events FOR VALUES "
            "FROM (date_trunc('month', now())) TO (date_trunc('month', now()) + interval '1 month')"
        )
    )
    call = await _make_call(scratch_session)
    event = _event(call)
    scratch_session.add(event)
    await scratch_session.flush()

    now = (await scratch_session.execute(select(func.now()))).scalar_one()
    assert event.created_at == now
    assert await _partition_of(scratch_session, event) == "call_events_current"


async def test_call_defaults(scratch_session: AsyncSession) -> None:
    call = await _make_call(scratch_session)
    assert (call.connected_seconds, call.transfer_seconds) == (Decimal("0.000"), Decimal("0.000"))
    assert (call.price_micros, call.test_call) == (0, False)
    assert (call.request_data, call.metadata_, call.variables) == ({}, {}, {})
    assert (call.answered_by, call.ended_by, call.parent_call_id) == (None, None, None)

    call.connected_seconds = Decimal("61.237")
    call.metadata_ = {"crm_id": "c-42"}
    await scratch_session.flush()
    stored = (
        select(text("connected_seconds::text"), text("metadata")).select_from(Call).where(Call.id == call.id)
    )
    assert tuple((await scratch_session.execute(stored)).one()) == ("61.237", {"crm_id": "c-42"})

    batch = await flushed(scratch_session, await _build_batch(scratch_session))
    assert isinstance(batch, Batch)
    assert (batch.status, batch.global_props, batch.schedule) == ("created", {}, {})
    counters = (
        batch.calls_total,
        batch.calls_dispatched,
        batch.calls_completed,
        batch.calls_successful,
        batch.calls_failed,
    )
    assert counters == (0, 0, 0, 0, 0)
    item = await flushed(scratch_session, await _build_batch_item(scratch_session))
    assert isinstance(item, BatchItem)
    assert (item.status, item.attempts, item.call_id) == ("pending", 0, None)

    leg = await flushed(scratch_session, _leg(call, "v3:defaults"))
    assert leg.started_at is not None
    turn = await flushed(scratch_session, _turn(call, 0, "assistant"))
    assert (turn.interrupted, turn.latency) == (False, {})


@pytest.mark.parametrize(
    ("builder", "column", "literal"),
    [
        (_build_batch, "status", BatchStatus),
        (_build_batch_item, "status", BatchItemStatus),
        (_build_call, "direction", CallDirection),
        (_build_call, "status", CallStatus),
        (_build_call, "runner_type", RunnerType),
        (_build_call_leg, "role", CallLegRole),
        (_build_call_leg, "state", CallLegState),
        (_build_call_turn, "role", CallTurnRole),
        (_build_call_event, "category", CallEventCategory),
        (_build_call_event, "level", CallEventLevel),
        (_build_call_analysis, "kind", CallAnalysisKind),
        (_build_eval_run, "status", EvalRunStatus),
        (_build_dnc_entry, "source", DncSource),
    ],
)
async def test_enum_values_accepted_and_rejected(
    scratch_session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    await assert_enum_check_enforced(scratch_session, builder, column, literal)


@pytest.mark.parametrize(
    ("column", "accepted", "rejected"),
    [
        ("answered_by", (*get_args(AnsweredBy), None), ("bogus",)),
        ("ended_by", (*get_args(EndedBy), None), ("bogus",)),
        ("external_id", ("x" * 256, "", None), ("x" * 257,)),
    ],
)
async def test_nullable_call_checks(
    scratch_session: AsyncSession, column: str, accepted: tuple[Any, ...], rejected: tuple[Any, ...]
) -> None:
    await assert_check_enforced(scratch_session, _build_call, column, accepted, rejected)


@pytest.mark.parametrize(
    ("column", "first", "duplicate", "index"),
    [
        ("phone_e164", "+14155550100", "+14155550100", "contacts_phone_uq"),
        ("email", "Pat@Example.test", "pat@example.TEST", "contacts_email_uq"),
        ("external_id", "crm-1", "crm-1", "contacts_external_uq"),
    ],
)
async def test_contact_partial_uniques(
    scratch_session: AsyncSession, column: str, first: str, duplicate: str, index: str
) -> None:
    org_id, other_org_id = await _org_id(scratch_session), await _org_id(scratch_session)
    await flushed(scratch_session, Contact(org_id=org_id, **{column: first}))
    await assert_rejected(scratch_session, Contact(org_id=org_id, **{column: duplicate}), index)
    await flushed(scratch_session, Contact(org_id=other_org_id, **{column: duplicate}))
    for _ in range(2):
        await flushed(scratch_session, Contact(org_id=org_id, name="No identifiers"))
    contact = await flushed(scratch_session, Contact(org_id=org_id))
    assert contact.attributes == {}


async def test_dnc_scope_unique(scratch_session: AsyncSession) -> None:
    org_id, other_org_id = await _org_id(scratch_session), await _org_id(scratch_session)
    phone = "+14155550199"

    def entry(owner: uuid.UUID | None) -> DncEntry:
        return DncEntry(org_id=owner, phone_e164=phone, source="platform" if owner is None else "manual")

    await flushed(scratch_session, entry(org_id))
    await assert_rejected(scratch_session, entry(org_id), "dnc_scope_phone_uq")
    await flushed(scratch_session, entry(None))
    await assert_rejected(scratch_session, entry(None), "dnc_scope_phone_uq")
    await flushed(scratch_session, entry(other_org_id))


async def test_uniques(scratch_session: AsyncSession) -> None:
    item = await flushed(scratch_session, await _build_batch_item(scratch_session))
    assert isinstance(item, BatchItem)
    await assert_rejected(
        scratch_session,
        BatchItem(org_id=item.org_id, batch_id=item.batch_id, ordinal=item.ordinal, payload={}),
        "batch_items_batch_id_ordinal_key",
    )

    call = await _make_call(scratch_session)
    await flushed(scratch_session, _turn(call, 3, "user"))
    await flushed(scratch_session, _turn(call, 3, "assistant"))
    await assert_rejected(scratch_session, _turn(call, 3, "user"), "call_turns_call_id_turn_index_role_key")

    await flushed(scratch_session, _leg(call, "v3:ccid"))
    await assert_rejected(
        scratch_session, _leg(await _make_call(scratch_session), "v3:ccid"), "call_legs_ccid_uq"
    )


async def _count(session: AsyncSession, model: type[Base], call_id: uuid.UUID) -> int:
    query = select(func.count()).select_from(model).where(model.__table__.c.call_id == call_id)
    return int((await session.execute(query)).scalar_one())


async def _call_column(session: AsyncSession, call_id: uuid.UUID, column: Any) -> Any:
    return (await session.execute(select(column).where(Call.id == call_id))).scalar_one()


async def test_call_children_cascade(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    call = await _make_call(scratch_session, org_id)
    schema = await flushed(
        scratch_session, CitationSchema(org_id=org_id, name="Intake", variables=[{"name": "dob"}])
    )
    version = await _eval_setup_version(scratch_session, org_id)
    children: list[Base] = [
        _leg(call, "v3:cascade"),
        _turn(call, 0, "user"),
        CallAnalysis(org_id=org_id, call_id=call.id, kind="qa", result={"score": 9}, model="m"),
        Citation(
            org_id=org_id,
            call_id=call.id,
            citation_schema_id=schema.id,
            variable_name="dob",
            variable_type="string",
        ),
        GuardRailEvent(org_id=org_id, call_id=call.id, guard_rail="tcpa:opt_out", triggered=True),
        EvalRun(org_id=org_id, call_id=call.id, eval_setup_version_id=version.id, status="completed"),
    ]
    for child in children:
        await flushed(scratch_session, child)
    child_models = [type(child) for child in children]
    assert [await _count(scratch_session, model, call.id) for model in child_models] == [1] * 6
    scratch_session.expunge_all()

    await scratch_session.execute(delete(Call).where(Call.id == call.id))
    assert [await _count(scratch_session, model, call.id) for model in child_models] == [0] * 6


async def test_set_null_paths(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    contact = await flushed(scratch_session, Contact(org_id=org_id, phone_e164="+14155550123"))
    batch = await flushed(scratch_session, Batch(org_id=org_id, label="October recalls"))
    item = await flushed(scratch_session, BatchItem(org_id=org_id, batch_id=batch.id, ordinal=0, payload={}))
    voice = await flushed(scratch_session, Voice(org_id=org_id, name="Ava", provider_voice_id="pv_ava"))
    parent = await _make_call(scratch_session, org_id)
    call = await _make_call(
        scratch_session,
        org_id,
        contact_id=contact.id,
        batch_id=batch.id,
        batch_item_id=item.id,
        voice_id=voice.id,
        parent_call_id=parent.id,
    )
    scratch_session.expunge_all()

    for model, row_id, column in (
        (Call, parent.id, Call.parent_call_id),
        (Contact, contact.id, Call.contact_id),
        (Voice, voice.id, Call.voice_id),
    ):
        await scratch_session.execute(delete(model).where(model.id == row_id))
        assert await _call_column(scratch_session, call.id, column) is None

    await scratch_session.execute(delete(Batch).where(Batch.id == batch.id))
    assert await _call_column(scratch_session, call.id, Call.batch_id) is None
    assert await _call_column(scratch_session, call.id, Call.batch_item_id) is None
    items = select(func.count()).select_from(BatchItem).where(BatchItem.batch_id == batch.id)
    assert (await scratch_session.execute(items)).scalar_one() == 0


async def test_call_turn_text_and_real_round_trip(scratch_session: AsyncSession) -> None:
    call = await _make_call(scratch_session)
    turn = await flushed(scratch_session, _turn(call, 1, "user", stt_confidence=0.875, text_realtime="hello"))
    scratch_session.expunge_all()

    stored = (await scratch_session.execute(select(CallTurn).where(CallTurn.id == turn.id))).scalar_one()
    assert stored.text_ == "Hello, this is Ava from Acme Dental."
    assert stored.stt_confidence == 0.875
    assert isinstance(stored.stt_confidence, float)
    raw = select(text("text")).select_from(CallTurn).where(CallTurn.id == turn.id)
    assert (await scratch_session.execute(raw)).scalar_one() == stored.text_
