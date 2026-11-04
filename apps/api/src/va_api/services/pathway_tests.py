"""Pathway unit tests (CRUD, capture from a call) and test runs (read) (specs/12-pathways-api.md §8).

Rules: specs/12 §8 Decision P07-T11b. Every function runs in the caller's ``tenant_session`` and first loads
the live pathway (``NOT_FOUND`` "Pathway not found."), so a deleted pathway's tests and runs are gone with it.
Audit rows carry a summary of the test (never the history, caller message, or ``expected`` values: mocked
tool bodies may hold secrets).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit, changed
from va_api.auth.context import RequestContext
from va_api.services.call_read import org_call
from va_common.errors import AppError
from va_db.models import CallTurn, Pathway, PathwayTestRun, PathwayTestRunKind, PathwayUnitTest
from va_db.pagination import Page
from va_db.repositories import PathwayRepo, PathwayRunRepo, PathwayTestRepo

__all__ = [
    "NOT_CALLER_TURN_MESSAGE",
    "NO_NODE_MESSAGE",
    "TEST_DELETED_MESSAGE",
    "WRONG_PATHWAY_MESSAGE",
    "CapturedTurn",
    "TranscriptTurn",
    "capture_from_call",
    "capture_turn",
    "create_unit_test",
    "delete_unit_test",
    "get_run",
    "get_unit_test",
    "list_runs",
    "list_unit_tests",
    "update_unit_test",
]

TEST_DELETED_MESSAGE: Final = "Test deleted."
WRONG_PATHWAY_MESSAGE: Final = "The call did not run this pathway."
NOT_CALLER_TURN_MESSAGE: Final = "The call has no caller turn with this index."
NO_NODE_MESSAGE: Final = "The turn has no pathway node."
_RESOURCE: Final = "pathway_test"
_TURN_FIELD: Final = "turn_index"


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """The parts of a ``call_turns`` row a capture reads."""

    turn_index: int
    role: str
    text: str
    text_spoken: str | None
    node_id: str | None

    @classmethod
    def of(cls, row: CallTurn) -> TranscriptTurn:
        return cls(row.turn_index, row.role, row.text_, row.text_spoken, row.node_id)


@dataclass(frozen=True, slots=True)
class CapturedTurn:
    """A unit test built from one caller turn: the earlier messages, the node, and the route taken."""

    history: list[dict[str, str]]
    node_id: str
    user_message: str
    observed_route: str | None


def capture_turn(turns: Sequence[TranscriptTurn], turn_index: int) -> CapturedTurn:
    """Build the test of the ``user`` turn ``turn_index`` from a call's turns (any order).

    History = every earlier turn (assistant content = what was played, ``text_spoken``, when recorded);
    node = the turn's ``node_id``, else the latest earlier one; observed route = the node of the first later
    assistant turn that has one. ``INVALID_INPUT`` (field ``turn_index``) without such a caller turn or node.
    """
    ordered = sorted(turns, key=lambda turn: turn.turn_index)
    target = next((t for t in ordered if t.turn_index == turn_index and t.role == "user"), None)
    if target is None:
        raise AppError("INVALID_INPUT", NOT_CALLER_TURN_MESSAGE, field=_TURN_FIELD)
    earlier = [turn for turn in ordered if turn.turn_index < turn_index]
    history = [
        {
            "role": turn.role,
            "content": turn.text_spoken
            if turn.role == "assistant" and turn.text_spoken is not None
            else turn.text,
        }
        for turn in earlier
    ]
    node_id = target.node_id or next((t.node_id for t in reversed(earlier) if t.node_id), None)
    if not node_id:
        raise AppError("INVALID_INPUT", NO_NODE_MESSAGE, field=_TURN_FIELD)
    route = next(
        (t.node_id for t in ordered if t.turn_index > turn_index and t.role == "assistant" and t.node_id),
        None,
    )
    return CapturedTurn(history=history, node_id=node_id, user_message=target.text, observed_route=route)


def _summary(test: PathwayUnitTest) -> dict[str, Any]:
    return {
        "name": test.name,
        "node_id": test.node_id,
        "history_messages": len(test.history),
        "expected_keys": sorted(test.expected),
        "simulated_variations": test.simulated_variations,
        "source_call_id": None if test.source_call_id is None else str(test.source_call_id),
    }


async def _pathway(session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID) -> Pathway:
    return await PathwayRepo(session, ctx.org_id).get(pathway_id)


async def list_unit_tests(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, *, limit: int, cursor: str | None
) -> Page[PathwayUnitTest]:
    """One page of the pathway's unit tests, newest first."""
    pathway = await _pathway(session, ctx, pathway_id)
    return await PathwayTestRepo(session, ctx.org_id).page(pathway.id, limit=limit, cursor=cursor)


async def get_unit_test(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, test_id: uuid.UUID
) -> PathwayUnitTest:
    """One unit test of the pathway; ``NOT_FOUND`` "Pathway test not found." otherwise."""
    pathway = await _pathway(session, ctx, pathway_id)
    return await PathwayTestRepo(session, ctx.org_id).get(pathway.id, test_id)


async def create_unit_test(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, values: Mapping[str, Any]
) -> PathwayUnitTest:
    """Store a unit test of the pathway (``values`` = column values) and audit it."""
    pathway = await _pathway(session, ctx, pathway_id)
    test = await PathwayTestRepo(session, ctx.org_id).add(pathway.id, values)
    await audit(session, ctx, "pathway_test.create", _RESOURCE, test.id, after=_summary(test))
    return test


async def update_unit_test(
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    test_id: uuid.UUID,
    values: Mapping[str, Any],
) -> PathwayUnitTest:
    """Set the columns in ``values`` that differ; a no-op writes nothing and no audit row."""
    pathway = await _pathway(session, ctx, pathway_id)
    repo = PathwayTestRepo(session, ctx.org_id)
    test = await repo.get(pathway.id, test_id)
    updates = {key: value for key, value in values.items() if getattr(test, key) != value}
    if not updates:
        return test
    before = _summary(test)
    await repo.change(test, updates)
    old, new = changed(before, _summary(test))
    after = {**new, "fields": sorted(updates)}
    await audit(session, ctx, "pathway_test.update", _RESOURCE, test.id, before=old, after=after)
    return test


async def delete_unit_test(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, test_id: uuid.UUID
) -> None:
    """Remove a unit test of the pathway."""
    pathway = await _pathway(session, ctx, pathway_id)
    repo = PathwayTestRepo(session, ctx.org_id)
    test = await repo.get(pathway.id, test_id)
    before = _summary(test)
    await repo.delete(test)
    await audit(session, ctx, "pathway_test.delete", _RESOURCE, test_id, before=before)


async def capture_from_call(  # noqa: PLR0913 - the from_call body fields
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    *,
    call_id: uuid.UUID,
    turn_index: int,
    name: str,
    expected: Mapping[str, Any] | None,
    simulated_variations: int,
) -> PathwayUnitTest:
    """Create a unit test from caller turn ``turn_index`` of a call that ran the pathway.

    ``CALL_NOT_FOUND`` for an unknown call; ``INVALID_INPUT`` (field ``call_id``) when the call ran another
    pathway; :func:`capture_turn` errors otherwise. Without ``expected`` the test asserts the observed route.
    """
    pathway = await _pathway(session, ctx, pathway_id)
    call = await org_call(session, ctx.org_id, call_id)
    if call.pathway_id != pathway.id:
        raise AppError("INVALID_INPUT", WRONG_PATHWAY_MESSAGE, field="call_id")
    rows = await session.scalars(
        select(CallTurn).where(CallTurn.org_id == ctx.org_id, CallTurn.call_id == call.id)
    )
    captured = capture_turn([TranscriptTurn.of(row) for row in rows], turn_index)
    if expected is None:
        expected = {} if captured.observed_route is None else {"route_to_node_id": captured.observed_route}
    values = {
        "name": name,
        "node_id": captured.node_id,
        "history": captured.history,
        "user_message": captured.user_message,
        "expected": dict(expected),
        "simulated_variations": simulated_variations,
        "source_call_id": call.id,
    }
    test = await PathwayTestRepo(session, ctx.org_id).add(pathway.id, values)
    await audit(session, ctx, "pathway_test.create", _RESOURCE, test.id, after=_summary(test))
    return test


async def list_runs(  # noqa: PLR0913 - pathway, filter, and page
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    *,
    kind: PathwayTestRunKind | None,
    limit: int,
    cursor: str | None,
) -> Page[PathwayTestRun]:
    """One page of the pathway's test runs, newest first."""
    pathway = await _pathway(session, ctx, pathway_id)
    return await PathwayRunRepo(session, ctx.org_id).page(pathway.id, kind=kind, limit=limit, cursor=cursor)


async def get_run(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, run_id: uuid.UUID
) -> PathwayTestRun:
    """One test run of the pathway; ``NOT_FOUND`` "Pathway test run not found." otherwise."""
    pathway = await _pathway(session, ctx, pathway_id)
    return await PathwayRunRepo(session, ctx.org_id).get(pathway.id, run_id)
