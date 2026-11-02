"""``GET /v1/calls/{call_id}`` and ``GET /v1/calls`` over a real database (specs/05-calls-api.md §6, §7.1;
specs/19 §4; Decision P05-T16a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
from call_fakes import PRESIGN_HOST
from call_helpers import bearer
from call_read_helpers import BASE, SeededOrg, an_org
from service_helpers import json_log_stream, log_events
from session_helpers import SessionContainer, error_of, store_api_key
from sqlalchemy import update
from tenant_rows import RowFactory

from va_common.ids import new_id
from va_common.storage import RECORDING_URL_TTL_S
from va_db.base import Base
from va_db.models import Agent, Batch, Call, Org, Pathway, Persona

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

LIST: Final = "/v1/calls"
NUMBER_A: Final = "+14155550101"
NUMBER_B: Final = "+14155550102"
ABSENT_ID: Final = "0193c5d2-0000-7000-8000-000000000001"
OTHER_ID: Final = "0193c5d2-0000-7000-8000-000000000002"


async def listed(client: httpx.AsyncClient, org: SeededOrg, **params: Any) -> list[uuid.UUID]:
    """The call ids of one listing, restricted to the org's seeded calls (the org's first call is older)."""
    response = await client.get(LIST, params=params, headers=bearer(org.key))
    assert response.status_code == 200, response.text
    return [
        uuid.UUID(call["call_id"])
        for call in response.json()["calls"]
        if uuid.UUID(call["call_id"]) in org.calls
    ]


def picked(org: SeededOrg, *indices: int) -> set[uuid.UUID]:
    return {org.calls[index] for index in indices}


# --- detail ---------------------------------------------------------------------------------------------


async def test_detail_returns_the_bare_call_object_with_a_fresh_recording_url(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(
        container,
        {"recording_s3_key": "org/2026/10/call.wav", "status": "no_answer", "answered_by": "no_answer"},
    )
    call_id = org.calls[0]
    response = await client.get(f"{LIST}/{call_id}", headers=bearer(org.key))
    assert response.status_code == 200
    body = response.json()
    assert body["call_id"] == body["c_id"] == str(call_id)
    assert body["status"] == "no-answer"
    assert body["answered_by"] == "no-answer"
    assert "message" not in body
    assert (
        body["recording_url"]
        == f"{PRESIGN_HOST}/va-recordings/org/2026/10/call.wav?expires={RECORDING_URL_TTL_S}"
    )
    expires = container.clock.now() + timedelta(seconds=RECORDING_URL_TTL_S)
    assert datetime.fromisoformat(body["recording_expiration"]) == expires
    assert container.presigner.presigned == [("va-recordings", "org/2026/10/call.wav", RECORDING_URL_TTL_S)]
    assert body["transcripts"] == [] and body["concatenated_transcript"] == ""


async def test_the_recording_url_is_regenerated_on_each_read(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {"recording_s3_key": "k.wav"})
    for _ in range(2):
        response = await client.get(f"{LIST}/{org.calls[0]}", headers=bearer(org.key))
        assert response.json()["recording_url"] is not None
    assert len(container.presigner.presigned) == 2


async def test_a_call_without_a_stored_recording_has_no_url(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {})
    body = (await client.get(f"{LIST}/{org.calls[0]}", headers=bearer(org.key))).json()
    assert (body["recording_url"], body["recording_expiration"]) == (None, None)
    assert container.presigner.presigned == []


async def test_a_presign_failure_returns_the_call_without_a_url(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {"recording_s3_key": "secret/key.wav"})
    container.presigner.fail = True
    stream = json_log_stream()
    response = await client.get(f"{LIST}/{org.calls[0]}", headers=bearer(org.key))
    assert response.status_code == 200
    assert response.json()["recording_url"] is None
    (failure,) = [e for e in log_events(stream) if e["event"] == "calls.recording_url_failed"]
    assert failure["error_type"] == "ClientError"
    assert "secret/key.wav" not in stream.getvalue()


@pytest.mark.parametrize("call_id", ["not-a-uuid", ABSENT_ID])
async def test_an_unknown_call_is_not_found(
    container: SessionContainer, client: httpx.AsyncClient, call_id: str
) -> None:
    org = await an_org(container)
    response = await client.get(f"{LIST}/{call_id}", headers=bearer(org.key))
    assert response.status_code == 404
    assert response.json()["errors"][0]["error"] == "CALL_NOT_FOUND"


async def test_another_orgs_call_is_not_found(container: SessionContainer, client: httpx.AsyncClient) -> None:
    mine, theirs = await an_org(container), await an_org(container, {})
    response = await client.get(f"{LIST}/{theirs.calls[0]}", headers=bearer(mine.key))
    assert response.status_code == 404
    assert response.json()["errors"][0]["error"] == "CALL_NOT_FOUND"
    assert theirs.calls[0] not in await listed(client, mine)


@pytest.mark.parametrize("path", [LIST, f"{LIST}/{ABSENT_ID}"])
async def test_reading_calls_needs_the_read_scope(
    container: SessionContainer, client: httpx.AsyncClient, path: str
) -> None:
    org = await an_org(container)
    key, _ = await store_api_key(container, org.org_id, ("calls:write",))
    response = await client.get(path, headers=bearer(key))
    assert error_of(response)[:2] == (403, "FORBIDDEN")
    reader, _ = await store_api_key(container, org.org_id, ("calls:read",))
    assert (await client.get(LIST, headers=bearer(reader))).status_code == 200


# --- list -----------------------------------------------------------------------------------------------


async def test_the_list_body_and_default_order(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {}, {}, {"external_id": "x-1"})
    response = await client.get(LIST, headers=bearer(org.key))
    body = response.json()
    assert (body["status"], body["total_count"], body["count"], body["next_cursor"]) == (
        "success",
        4,
        4,
        None,
    )
    assert [uuid.UUID(call["call_id"]) for call in body["calls"]][:3] == org.calls[::-1]
    item = body["calls"][0]
    assert item["external_id"] == "x-1"
    assert "transcripts" not in item and "variables" not in item and "analysis" not in item
    assert "latency" in item and "guard_rails" in item


async def test_ascending_lists_oldest_first(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await an_org(container, {}, {}, {})
    assert await listed(client, org, ascending="true") == org.calls


FILTERS: Final[list[tuple[str, list[dict[str, Any]], dict[str, Any], tuple[int, ...]]]] = [
    ("to_number", [{"to_number": NUMBER_A}, {"to_number": NUMBER_B}], {"to_number": NUMBER_A}, (0,)),
    ("to", [{"to_number": NUMBER_A}, {"to_number": NUMBER_B}], {"to": NUMBER_B}, (1,)),
    ("from_number", [{"from_number": NUMBER_A}, {"from_number": NUMBER_B}], {"from_number": NUMBER_A}, (0,)),
    ("from", [{"from_number": NUMBER_A}, {"from_number": NUMBER_B}], {"from": NUMBER_B}, (1,)),
    ("to_and_alias_equal", [{"to_number": NUMBER_A}, {}], {"to": NUMBER_A, "to_number": NUMBER_A}, (0,)),
    (
        "completed_true",
        [{"queue_status": "complete"}, {"queue_status": "started"}, {}],
        {"completed": "true"},
        (0,),
    ),
    (
        "completed_false",
        [{"queue_status": "complete"}, {"queue_status": "started"}, {}],
        {"completed": "false"},
        (1, 2),
    ),
    (
        "answered_by",
        [{"answered_by": "human"}, {"answered_by": "voicemail"}],
        {"answered_by": "voicemail"},
        (1,),
    ),
    (
        "answered_by_no_answer",
        [{"answered_by": "no_answer"}, {"answered_by": "human"}],
        {"answered_by": "no-answer"},
        (0,),
    ),
    (
        "inbound_true",
        [{"direction": "inbound"}, {"direction": "outbound"}, {"direction": "web"}],
        {"inbound": "true"},
        (0,),
    ),
    (
        "inbound_false",
        [{"direction": "inbound"}, {"direction": "outbound"}, {"direction": "web"}],
        {"inbound": "false"},
        (1, 2),
    ),
    (
        "status_bland",
        [{"status": "no_answer"}, {"status": "in_progress"}, {"status": "ringing"}, {"status": "busy"}],
        {"status": "no-answer,in-progress"},
        (0, 1, 2),
    ),
    (
        "status_completed_covers_transferred",
        [{"status": "transferred"}, {"status": "completed"}, {"status": "failed"}],
        {"status": "completed"},
        (0, 1),
    ),
    (
        "status_internal",
        [{"status": "ringing"}, {"status": "in_progress"}, {"status": "no_answer"}],
        {"status": "ringing, no_answer"},
        (0, 2),
    ),
    (
        "disposition_tag",
        [{"disposition_tag": "WON"}, {"disposition_tag": "LOST"}],
        {"disposition_tag": "WON"},
        (0,),
    ),
    (
        "duration_gt",
        [
            {"connected_seconds": Decimal(59)},
            {"connected_seconds": Decimal(61)},
            {"connected_seconds": Decimal(60)},
        ],
        {"duration_gt": "1"},
        (1,),
    ),
    (
        "duration_lt",
        [
            {"connected_seconds": Decimal(59)},
            {"connected_seconds": Decimal(61)},
            {"connected_seconds": Decimal(60)},
        ],
        {"duration_lt": "1"},
        (0,),
    ),
    (
        "duration_range",
        [{"connected_seconds": Decimal(90)}, {"connected_seconds": Decimal(200)}],
        {"duration_gt": "1", "duration_lt": "2"},
        (0,),
    ),
    ("external_id", [{"external_id": "a"}, {"external_id": "b"}], {"external_id": "b"}, (1,)),
    (
        "created_at_gte",
        [{"created_at": BASE}, {"created_at": BASE + timedelta(hours=1)}],
        {"created_at_gte": (BASE + timedelta(hours=1)).isoformat()},
        (1,),
    ),
    (
        "created_at_lte",
        [{"created_at": BASE}, {"created_at": BASE + timedelta(hours=1)}],
        {"created_at_lte": BASE.isoformat()},
        (0,),
    ),
    (
        "created_at_naive_is_utc",
        [{"created_at": BASE}, {"created_at": BASE + timedelta(hours=1)}],
        {"created_at_gte": "2026-10-20T12:30:00"},
        (1,),
    ),
]


@pytest.mark.parametrize(
    ("seeds", "params", "expected"), [f[1:] for f in FILTERS], ids=[f[0] for f in FILTERS]
)
async def test_each_filter(
    container: SessionContainer,
    client: httpx.AsyncClient,
    seeds: list[dict[str, Any]],
    params: dict[str, Any],
    expected: tuple[int, ...],
) -> None:
    org = await an_org(container, *seeds)
    assert set(await listed(client, org, **params)) == picked(org, *expected)


@pytest.mark.parametrize("param", ["batch_id", "campaign_id"])
async def test_batch_filter_and_its_campaign_alias(
    container: SessionContainer, client: httpx.AsyncClient, param: str
) -> None:
    org = await an_org(container, {}, {})
    batch_id = await _batch(container, org)
    await _set_call(container, org, 1, batch_id=batch_id)
    assert await listed(client, org, **{param: str(batch_id)}) == [org.calls[1]]


@pytest.mark.parametrize("column", ["pathway_id", "persona_id", "agent_id"])
async def test_runner_filters(container: SessionContainer, client: httpx.AsyncClient, column: str) -> None:
    org = await an_org(container, {}, {})
    runner_id = await _runner_row(container, org, column)
    await _set_call(container, org, 0, **{column: runner_id})
    assert await listed(client, org, **{column: str(runner_id)}) == [org.calls[0]]
    assert await listed(client, org, **{column: str(new_id())}) == []


async def test_start_and_end_date_use_the_org_timezone(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    edges = [
        datetime(2026, 10, 24, 3, 59, tzinfo=UTC),  # Oct 23 23:59 in New York (EDT)
        datetime(2026, 10, 24, 4, 0, tzinfo=UTC),  # Oct 24 00:00
        datetime(2026, 10, 25, 3, 59, 59, tzinfo=UTC),  # Oct 24 23:59:59
        datetime(2026, 10, 25, 4, 0, tzinfo=UTC),  # Oct 25 00:00
    ]
    org = await an_org(container, *({"created_at": at} for at in edges))
    async with container.database.system_session() as session:
        await session.execute(
            update(Org).where(Org.id == org.org_id).values(settings={"timezone": "America/New_York"})
        )
    day = "2026-10-24"
    assert set(await listed(client, org, start_date=day, end_date=day)) == picked(org, 1, 2)
    assert set(await listed(client, org, start_date="2026-10-25")) == picked(org, 3)
    assert set(await listed(client, org, end_date="2026-10-23")) == picked(org, 0)


async def test_dates_without_org_settings_use_the_default_timezone(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(
        container,
        {"created_at": datetime(2026, 10, 24, 6, 59, tzinfo=UTC)},  # Oct 23 23:59 in Los Angeles (PDT)
        {"created_at": datetime(2026, 10, 24, 7, 0, tzinfo=UTC)},
    )
    assert set(await listed(client, org, start_date="2026-10-24")) == picked(org, 1)


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": "0"}, "limit"),
        ({"limit": "1001"}, "limit"),
        ({"status": "completed,dancing"}, "status"),
        ({"to": NUMBER_A, "to_number": NUMBER_B}, "to"),
        ({"from": NUMBER_A, "from_number": NUMBER_B}, "from"),
        ({"batch_id": ABSENT_ID, "campaign_id": OTHER_ID}, "campaign_id"),
        ({"cursor": "not a cursor!"}, "cursor"),
        ({"answered_by": "robot"}, "answered_by"),
        ({"pathway_id": "nope"}, "pathway_id"),
    ],
)
async def test_invalid_list_parameters(
    container: SessionContainer, client: httpx.AsyncClient, params: dict[str, str], field: str
) -> None:
    org = await an_org(container)
    response = await client.get(LIST, params=params, headers=bearer(org.key))
    assert error_of(response) == (400, "INVALID_INPUT", field)


async def test_unknown_parameters_are_ignored(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await an_org(container, {})
    assert await listed(client, org, color="blue") == org.calls


@pytest.mark.parametrize("ascending", [False, True])
async def test_cursor_paging_visits_every_call_once(
    container: SessionContainer, client: httpx.AsyncClient, ascending: bool
) -> None:
    org = await an_org(container, {}, {}, {}, {}, {})
    params: dict[str, Any] = {"limit": 2, "ascending": str(ascending).lower()}
    seen: list[uuid.UUID] = []
    bodies: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page_params = params if cursor is None else {**params, "cursor": cursor}
        body = (await client.get(LIST, params=page_params, headers=bearer(org.key))).json()
        bodies.append(body)
        seen.extend(uuid.UUID(call["call_id"]) for call in body["calls"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(bodies) == 3
    assert [body["count"] for body in bodies] == [2, 2, 2]
    assert bodies[0]["total_count"] == 6
    assert all("total_count" not in body for body in bodies[1:])
    assert len(seen) == len(set(seen)) == 6
    ordered = [call for call in seen if call in org.calls]
    assert ordered == (org.calls if ascending else org.calls[::-1])


async def test_total_count_counts_every_match_beyond_the_page(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {"external_id": "e"}, {"external_id": "e"}, {"external_id": "e"})
    body = (await client.get(LIST, params={"external_id": "e", "limit": 1}, headers=bearer(org.key))).json()
    assert (body["total_count"], body["count"]) == (3, 1)
    assert body["next_cursor"] is not None


async def test_list_items_carry_recording_urls(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {"recording_s3_key": "a.wav"}, {})
    body = (await client.get(LIST, params={"ascending": "true"}, headers=bearer(org.key))).json()
    urls = {call["call_id"]: call["recording_url"] for call in body["calls"]}
    assert urls[str(org.calls[0])] == f"{PRESIGN_HOST}/va-recordings/a.wav?expires={RECORDING_URL_TTL_S}"
    assert urls[str(org.calls[1])] is None


async def _set_call(container: SessionContainer, org: SeededOrg, index: int, **values: Any) -> None:
    async with container.database.tenant_session(org.org_id) as session:
        await session.execute(update(Call).where(Call.id == org.calls[index]).values(**values))


async def _batch(container: SessionContainer, org: SeededOrg) -> uuid.UUID:
    async with container.database.tenant_session(org.org_id) as session:
        key = await RowFactory().insert(session, Base.metadata.tables[Batch.__tablename__], org.org_id)
    return uuid.UUID(str(key["id"]))


async def _runner_row(container: SessionContainer, org: SeededOrg, column: str) -> uuid.UUID:
    model = {"pathway_id": Pathway, "persona_id": Persona, "agent_id": Agent}[column]
    async with container.database.tenant_session(org.org_id) as session:
        key = await RowFactory().insert(session, Base.metadata.tables[model.__tablename__], org.org_id)
    return uuid.UUID(str(key["id"]))
