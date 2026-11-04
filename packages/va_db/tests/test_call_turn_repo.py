"""``CallTurnRepo`` on a migrated database: idempotent upsert, latency merge, delete, order, and RLS
(specs/03-data-model.md ``call_turns``; specs/07 §8 Decision P05-T11a)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from call_rows import SeededCall, seed_call
from tenant_rows import open_database

from va_db.call_turns import CallTurnRow
from va_db.models import CallTurn, CallTurnRole
from va_db.repositories import CallTurnRepo
from va_db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


def _row(call: SeededCall, index: int, role: CallTurnRole = "user", **values: Any) -> CallTurnRow:
    fields: dict[str, Any] = {
        "text": f"turn {index}",
        "start_ms": index * 1000,
        "end_ms": index * 1000 + 500,
    }
    fields.update(values)
    return CallTurnRow(org_id=call.org_id, call_id=call.call_id, turn_index=index, role=role, **fields)


async def _turns(database: Database, call: SeededCall) -> list[CallTurn]:
    async with database.tenant_session(call.org_id) as session:
        return await CallTurnRepo(session, call.org_id).list_for_call(call.call_id)


async def test_upsert_inserts_then_updates_the_same_key(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        written = await CallTurnRepo(session, call.org_id).upsert_many(
            [_row(call, 0, "assistant", text="Hello", text_spoken="Hello"), _row(call, 1)]
        )
    assert written == 2
    async with database.tenant_session(call.org_id) as session:
        await CallTurnRepo(session, call.org_id).upsert_many(
            [_row(call, 0, "assistant", text="Hello there", text_spoken="Hello", interrupted=True)]
        )
    turns = await _turns(database, call)
    assert [(t.turn_index, t.role, t.text_, t.text_spoken, t.interrupted) for t in turns] == [
        (0, "assistant", "Hello there", "Hello", True),
        (1, "user", "turn 1", None, False),
    ]


async def test_resending_rows_is_idempotent(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    rows = [_row(call, 1, language_code="en"), _row(call, 2, "assistant")]
    for _ in range(2):
        async with database.tenant_session(call.org_id) as session:
            await CallTurnRepo(session, call.org_id).upsert_many(rows)
    turns = await _turns(database, call)
    assert [(t.turn_index, t.role, t.language_code) for t in turns] == [
        (1, "user", "en"),
        (2, "assistant", None),
    ]


async def test_latency_merge_keeps_the_text_and_survives_a_resent_row(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        repo = CallTurnRepo(session, call.org_id)
        await repo.upsert_many([_row(call, 1, text="book a table", latency={"stt_final_ms": 40})])
        assert await repo.merge_latency(call.call_id, 1, "user", {"e2e_ms": 700, "stt_final_ms": 63})
        assert not await repo.merge_latency(call.call_id, 9, "user", {"e2e_ms": 1})
    async with database.tenant_session(call.org_id) as session:
        await CallTurnRepo(session, call.org_id).upsert_many([_row(call, 1, text="book a table please")])
    (turn,) = await _turns(database, call)
    assert turn.text_ == "book a table please"
    assert turn.latency == {"e2e_ms": 700, "stt_final_ms": 63}


async def test_a_repeated_key_in_one_batch_collapses_to_the_last_row(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        written = await CallTurnRepo(session, call.org_id).upsert_many(
            [_row(call, 3, text="first", latency={"a": 1}), _row(call, 3, text="second", latency={"b": 2})]
        )
    assert written == 1
    (turn,) = await _turns(database, call)
    assert (turn.text_, turn.latency) == ("second", {"a": 1, "b": 2})


async def test_empty_batch_writes_nothing(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        assert await CallTurnRepo(session, call.org_id).upsert_many([]) == 0


async def test_delete_removes_only_the_named_row(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        repo = CallTurnRepo(session, call.org_id)
        await repo.upsert_many([_row(call, 1), _row(call, 2), _row(call, 1, "assistant")])
        assert await repo.delete_turn(call.call_id, 1, "user")
        assert not await repo.delete_turn(call.call_id, 1, "user")
    assert [(t.turn_index, t.role) for t in await _turns(database, call)] == [(1, "assistant"), (2, "user")]


async def test_list_orders_by_index_then_role(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        await CallTurnRepo(session, call.org_id).upsert_many(
            [_row(call, 4, "assistant"), _row(call, 0, "assistant"), _row(call, 4), _row(call, 2)]
        )
    order = [(t.turn_index, t.role) for t in await _turns(database, call)]
    assert order == [(0, "assistant"), (2, "user"), (4, "assistant"), (4, "user")]


async def test_another_org_sees_and_changes_nothing(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    other = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        await CallTurnRepo(session, call.org_id).upsert_many([_row(call, 1)])
    async with database.tenant_session(other.org_id) as session:
        repo = CallTurnRepo(session, other.org_id)
        assert await repo.list_for_call(call.call_id) == []
        assert not await repo.merge_latency(call.call_id, 1, "user", {"e2e_ms": 1})
        assert not await repo.delete_turn(call.call_id, 1, "user")
    assert len(await _turns(database, call)) == 1


async def test_a_row_of_another_org_is_refused(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        repo = CallTurnRepo(session, uuid.uuid4())
        with pytest.raises(ValueError, match="another org"):
            await repo.upsert_many([_row(call, 1)])


async def test_pathway_turn_is_written_and_updated(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        repo = CallTurnRepo(session, call.org_id)
        await repo.upsert_many([_row(call, 2, "assistant", pathway_turn=1), _row(call, 3)])
        await repo.upsert_many([_row(call, 3, pathway_turn=2)])
    assert [(t.turn_index, t.pathway_turn) for t in await _turns(database, call)] == [(2, 1), (3, 2)]
