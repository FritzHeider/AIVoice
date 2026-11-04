"""``POST /v1/pathway/{pathway_id}/tests/from_call`` (specs/12 §8; Decision P07-T11b item 8)."""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
import pytest
from call_rows import seed_call
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, add, star_api
from session_helpers import SessionContainer, audit_rows, error_of

from va_api.services.pathway_tests import (
    NO_NODE_MESSAGE,
    NOT_CALLER_TURN_MESSAGE,
    WRONG_PATHWAY_MESSAGE,
    TranscriptTurn,
    capture_turn,
)
from va_common.errors import AppError
from va_db.models import CallTurn

pytestmark = pytest.mark.integration

# (turn_index, role, text, text_spoken, node_id): the opener was cut off by the caller.
TRANSCRIPT: Final = (
    (0, "assistant", "Hi, Bella Pasta, how can I help you today?", "Hi, Bella Pasta", "start"),
    (1, "user", "I want a pizza", None, None),
    (2, "assistant", "Sure, what size?", None, "node_order"),
    (3, "user", "A large pepperoni please", None, None),
    (4, "assistant", "Delivery or pickup?", None, "node_delivery"),
)


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


async def _call(
    identity: IdentityDb, org_id: uuid.UUID, pathway_id: str | None, turns: Any = TRANSCRIPT
) -> str:
    seeded = await seed_call(
        identity.database,
        org_id=org_id,
        status="completed",
        runner_type="pathway" if pathway_id else "task",
        pathway_id=None if pathway_id is None else uuid.UUID(pathway_id),
        pathway_version=1 if pathway_id else None,
    )
    rows = [
        CallTurn(
            org_id=org_id,
            call_id=seeded.call_id,
            turn_index=index,
            role=role,
            text_=text,
            text_spoken=spoken,
            start_ms=index * 1000,
            end_ms=index * 1000 + 900,
            node_id=node,
        )
        for index, role, text, spoken, node in turns
    ]
    await add(identity, org_id, *rows)
    return str(seeded.call_id)


async def _from_call(api: Api, pathway_id: str, **body: Any) -> httpx.Response:
    return await api.client.post(
        f"{BASE}/{pathway_id}/tests/from_call", json={"name": "Pepperoni", **body}, headers=api.headers
    )


async def test_builds_history_node_and_observed_route(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created()
    call_id = await _call(identity, api.org_id, pathway_id)
    response = await _from_call(api, pathway_id, call_id=call_id, turn_index=3, simulated_variations=0)
    assert response.status_code == 200, response.text
    test = response.json()["test"]
    assert test["history"] == [
        {"role": "assistant", "content": "Hi, Bella Pasta"},
        {"role": "user", "content": "I want a pizza"},
        {"role": "assistant", "content": "Sure, what size?"},
    ]
    assert (test["node_id"], test["user_message"]) == ("node_order", "A large pepperoni please")
    assert test["expected"] == {"route_to_node_id": "node_delivery"}
    assert (test["source_call_id"], test["simulated_variations"]) == (call_id, 0)
    (row,) = await audit_rows(identity, api.org_id, "pathway_test.create")
    assert row.changes["after"]["source_call_id"] == call_id

    explicit = await _from_call(
        api, pathway_id, call_id=call_id, turn_index=1, expected={"grading_goal": "Ask the size."}
    )
    assert explicit.json()["test"]["expected"] == {"grading_goal": "Ask the size."}
    assert explicit.json()["test"]["node_id"] == "start"
    assert explicit.json()["test"]["history"] == [{"role": "assistant", "content": "Hi, Bella Pasta"}]


async def test_refusals(
    api: Api, identity: IdentityDb, client: httpx.AsyncClient, container: SessionContainer
) -> None:
    pathway_id = await api.created()
    call_id = await _call(identity, api.org_id, pathway_id)
    for turn_index in (2, 9):
        response = await _from_call(api, pathway_id, call_id=call_id, turn_index=turn_index)
        assert error_of(response) == (400, "INVALID_INPUT", "turn_index")
        assert response.json()["errors"][0]["message"] == NOT_CALLER_TURN_MESSAGE

    other_pathway = await api.created(name="Other")
    response = await _from_call(api, other_pathway, call_id=call_id, turn_index=3)
    assert error_of(response) == (400, "INVALID_INPUT", "call_id")
    assert response.json()["errors"][0]["message"] == WRONG_PATHWAY_MESSAGE
    task_call = await _call(identity, api.org_id, None)
    assert error_of(await _from_call(api, pathway_id, call_id=task_call, turn_index=3))[:2] == (
        400,
        "INVALID_INPUT",
    )

    nodeless = await _call(identity, api.org_id, pathway_id, turns=[(0, "user", "Hello?", None, None)])
    response = await _from_call(api, pathway_id, call_id=nodeless, turn_index=0)
    assert error_of(response) == (400, "INVALID_INPUT", "turn_index")
    assert response.json()["errors"][0]["message"] == NO_NODE_MESSAGE

    stranger = await star_api(client, identity, container)
    foreign = await _call(identity, stranger.org_id, await stranger.created())
    assert error_of(await _from_call(api, pathway_id, call_id=foreign, turn_index=3))[:2] == (
        404,
        "CALL_NOT_FOUND",
    )
    assert error_of(await _from_call(api, pathway_id, call_id=str(uuid.uuid4()), turn_index=3))[:2] == (
        404,
        "CALL_NOT_FOUND",
    )
    assert error_of(await _from_call(stranger, pathway_id, call_id=call_id, turn_index=3))[:2] == (
        404,
        "NOT_FOUND",
    )
    assert await audit_rows(identity, api.org_id, "pathway_test.create") == []


def _turns(*rows: tuple[int, str, str, str | None, str | None]) -> list[TranscriptTurn]:
    return [TranscriptTurn(*row) for row in rows]


def test_capture_turn_orders_and_falls_back() -> None:
    turns = _turns(*reversed(TRANSCRIPT))
    captured = capture_turn(turns, 1)
    assert captured.history == [{"role": "assistant", "content": "Hi, Bella Pasta"}]
    assert (captured.node_id, captured.observed_route) == ("start", "node_order")
    last = capture_turn(_turns(*TRANSCRIPT[:4]), 3)
    assert last.observed_route is None
    own_node = capture_turn(_turns((0, "user", "Hi", None, "start"), (1, "assistant", "Yo", None, None)), 0)
    assert (own_node.node_id, own_node.history, own_node.observed_route) == ("start", [], None)


def test_capture_turn_errors_name_the_turn_field() -> None:
    with pytest.raises(AppError) as missing:
        capture_turn(_turns(*TRANSCRIPT), 4)
    assert (missing.value.code, missing.value.field) == ("INVALID_INPUT", "turn_index")
    with pytest.raises(AppError, match="no pathway node"):
        capture_turn(_turns((0, "user", "Hi", None, None)), 0)
