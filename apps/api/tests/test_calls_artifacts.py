"""``GET /v1/calls/active``, ``/{call_id}/recording``, ``/{call_id}/transcript``, and ``/{call_id}/events``
over a real database (specs/05-calls-api.md §7.4, §7.5, §7.6, §7.9; Decision P05-T16b)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Final, Self

import httpx
import pytest
from call_fakes import PRESIGN_HOST
from call_helpers import bearer
from call_read_helpers import BASE, SeededOrg, an_org
from call_rows import seed_call
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events
from session_helpers import SessionContainer, error_of, store_api_key
from tenant_rows import RowFactory

from va_common.ids import new_id
from va_common.storage import PLAYBACK_URL_TTL_S, RecordingVariant, recording_variant_key
from va_db.base import Base
from va_db.models import CallEvent, CallStatus, CallTurn, CallTurnRole
from va_domain.call_object import Transcript, to_srt, to_vtt
from va_domain.carrier import carrier_state_key

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

CALLS: Final = "/v1/calls"
ACTIVE: Final = "/v1/calls/active"
ABSENT_ID: Final = "0193c5d2-0000-7000-8000-000000000001"
BUCKET: Final = "va-recordings"
RAW_KEY: Final = "org/2026/10/call.wav"
ALL_STATUSES: Final[tuple[CallStatus, ...]] = (
    "scheduled",
    "queued",
    "dialing",
    "ringing",
    "in_progress",
    "transferring",
    "transferred",
    "completed",
    "failed",
    "canceled",
    "no_answer",
    "busy",
)
ACTIVE_ONES: Final = ("dialing", "ringing", "in_progress", "transferring")


def _presigned(key: str) -> str:
    return f"{PRESIGN_HOST}/{BUCKET}/{key}?expires={PLAYBACK_URL_TTL_S}"


# --- active ---------------------------------------------------------------------------------------------


async def _active(client: httpx.AsyncClient, org: SeededOrg) -> dict[str, Any]:
    response = await client.get(ACTIVE, headers=bearer(org.key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_only_active_statuses_are_listed_oldest_first(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    calls = [
        {"status": status, "created_at": BASE - timedelta(minutes=index)}
        for index, status in enumerate(ALL_STATUSES)
    ]
    org = await an_org(container, *calls)
    body = await _active(client, org)
    expected = [org.calls[ALL_STATUSES.index(s)] for s in ACTIVE_ONES]
    assert body["status"] == "success"
    assert body["count"] == 4
    assert [uuid.UUID(c["call_id"]) for c in body["calls"]] == list(reversed(expected))
    assert [c["status"] for c in body["calls"]] == list(reversed(ACTIVE_ONES))


async def test_an_active_item_carries_every_field(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    now = container.clock.now()
    org = await an_org(
        container,
        {
            "status": "in_progress",
            "direction": "inbound",
            "runner_type": "task",
            "pathway_id": None,
            "answered_at": now - timedelta(seconds=75, milliseconds=600),
            "dial_started_at": now - timedelta(seconds=90),
        },
        {"status": "dialing", "to_number": None, "from_number": None, "dial_started_at": now},
    )
    async with container.database.tenant_session(org.org_id) as session:
        pathway = await RowFactory().insert(session, Base.metadata.tables["pathways"], org.org_id)
    pathway_id = uuid.UUID(str(pathway["id"]))
    ringing = await seed_call(
        container.database,
        status="ringing",
        org_id=org.org_id,
        runner_type="pathway",
        pathway_id=pathway_id,
        created_at=BASE + timedelta(minutes=2),
    )
    org.calls.append(ringing.call_id)
    await container.redis.hset(
        carrier_state_key(org.calls[2]), mapping={"node_id": "qualify", "answered": "0"}
    )
    first, second, third = (await _active(client, org))["calls"]
    assert first == {
        "call_id": str(org.calls[0]),
        "to": "+14155550100",
        "from": "+14155550199",
        "status": "in_progress",
        "started_at": first["started_at"],
        "direction": "inbound",
        "runner_type": "task",
        "pathway_id": None,
        "current_node_id": None,
        "duration_seconds": 75,
    }
    assert first["started_at"].startswith((now - timedelta(seconds=75)).strftime("%Y-%m-%dT%H:%M"))
    assert (second["to"], second["from"], second["duration_seconds"]) == ("", "", 0)
    assert second["started_at"] is not None
    assert (third["pathway_id"], third["current_node_id"], third["started_at"]) == (
        str(pathway_id),
        "qualify",
        None,
    )


class _BrokenPipeline:
    async def __aenter__(self) -> Self:
        raise RedisConnectionError

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_a_redis_failure_leaves_node_ids_null(
    container: SessionContainer, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await an_org(container, {"status": "in_progress"}, {"status": "ringing"})
    await container.redis.hset(carrier_state_key(org.calls[0]), "node_id", "start")
    monkeypatch.setattr(container.redis, "pipeline", lambda **_: _BrokenPipeline())
    stream = json_log_stream()
    body = await _active(client, org)
    assert [c["current_node_id"] for c in body["calls"]] == [None, None]
    (warning,) = [e for e in log_events(stream) if e["event"] == "calls.active_node_ids_unavailable"]
    assert warning["count"] == 2


async def test_other_orgs_active_calls_are_absent(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    mine = await an_org(container)
    await an_org(container, {"status": "in_progress"})
    assert await _active(client, mine) == {"status": "success", "count": 0, "calls": []}


# --- recording ------------------------------------------------------------------------------------------


async def _recorded(container: SessionContainer, **values: Any) -> SeededOrg:
    return await an_org(container, {"recording_s3_key": RAW_KEY, "recording_duration_ms": 143_200, **values})


def _variant(org: SeededOrg, name: RecordingVariant) -> str:
    return recording_variant_key(org.org_id, org.calls[0], BASE, name)


async def test_no_recording_is_not_found(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await an_org(container, {})
    for params in ({}, {"format": "json"}, {"channel": "caller"}):
        response = await client.get(
            f"{CALLS}/{org.calls[0]}/recording", params=params, headers=bearer(org.key)
        )
        assert error_of(response) == (404, "NOT_FOUND", None)
        assert response.json()["errors"][0]["message"] == "Recording not found."


async def test_default_redirects_to_the_raw_recording(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await _recorded(container)
    response = await client.get(f"{CALLS}/{org.calls[0]}/recording", headers=bearer(org.key))
    assert response.status_code == 302
    assert response.headers["location"] == _presigned(RAW_KEY)
    assert response.headers["cache-control"] == "no-store"
    assert container.presigner.presigned == [(BUCKET, RAW_KEY, PLAYBACK_URL_TTL_S)]


async def test_json_format_answers_the_link(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await _recorded(container)
    response = await client.get(
        f"{CALLS}/{org.calls[0]}/recording", params={"format": "json"}, headers=bearer(org.key)
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    expires = container.clock.now() + timedelta(seconds=PLAYBACK_URL_TTL_S)
    assert body == {
        "status": "success",
        "url": _presigned(RAW_KEY),
        "expires_at": body["expires_at"],
        "duration_ms": 143_200,
        "channels": 2,
        "format": "wav",
    }
    assert body["expires_at"].startswith(expires.strftime("%Y-%m-%dT%H:%M:%S"))


async def test_a_mixed_variant_is_preferred(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await _recorded(container)
    mixed = _variant(org, "mixed")
    container.presigner.existing.add(mixed)
    response = await client.get(
        f"{CALLS}/{org.calls[0]}/recording", params={"format": "json"}, headers=bearer(org.key)
    )
    assert (response.json()["url"], response.json()["channels"]) == (_presigned(mixed), 1)


@pytest.mark.parametrize("channel", ["caller", "agent"])
async def test_split_channels_need_their_variant(
    container: SessionContainer, client: httpx.AsyncClient, channel: RecordingVariant
) -> None:
    org = await _recorded(container)
    url = f"{CALLS}/{org.calls[0]}/recording"
    absent = await client.get(url, params={"channel": channel}, headers=bearer(org.key))
    assert error_of(absent) == (404, "NOT_FOUND", None)
    key = _variant(org, channel)
    container.presigner.existing.add(key)
    present = await client.get(url, params={"channel": channel}, headers=bearer(org.key))
    assert (present.status_code, present.headers["location"]) == (302, _presigned(key))


@pytest.mark.parametrize("failure", ["fail_head", "fail"])
async def test_storage_failures_are_unavailable(
    container: SessionContainer, client: httpx.AsyncClient, failure: str
) -> None:
    org = await _recorded(container)
    setattr(container.presigner, failure, True)
    stream = json_log_stream()
    response = await client.get(f"{CALLS}/{org.calls[0]}/recording", headers=bearer(org.key))
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    (event,) = [e for e in log_events(stream) if e["event"] == "calls.recording_link_failed"]
    assert (event["call_id"], event["error_type"]) == (str(org.calls[0]), "ClientError")
    assert RAW_KEY not in stream.getvalue()
    assert str(org.org_id) + "/" not in stream.getvalue()


@pytest.mark.parametrize(
    ("params", "field"), [({"channel": "bogus"}, "channel"), ({"format": "xml"}, "format")]
)
async def test_invalid_recording_parameters(
    container: SessionContainer, client: httpx.AsyncClient, params: dict[str, str], field: str
) -> None:
    org = await _recorded(container)
    response = await client.get(f"{CALLS}/{org.calls[0]}/recording", params=params, headers=bearer(org.key))
    assert error_of(response) == (400, "INVALID_INPUT", field)


# --- transcript -----------------------------------------------------------------------------------------

TURNS: Final[list[tuple[CallTurnRole, str]]] = [
    ("user", "Hello?"),
    ("assistant", "Hi, this is Ava."),
    ("system", "hidden"),
    ("agent-action", "Pressed 1 & <done>."),
]


async def _with_turns(container: SessionContainer, status: CallStatus = "completed") -> SeededOrg:
    org = await an_org(container, {"status": status})
    async with container.database.tenant_session(org.org_id) as session:
        session.add_all(
            CallTurn(
                id=new_id(),
                org_id=org.org_id,
                call_id=org.calls[0],
                turn_index=index,
                role=role,
                text_=text,
                start_ms=index * 1500,
                end_ms=index * 1500 + 900,
            )
            for index, (role, text) in enumerate(TURNS)
        )
    return org


async def test_json_transcript_equals_the_call_objects(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await _with_turns(container)
    detail = (await client.get(f"{CALLS}/{org.calls[0]}", headers=bearer(org.key))).json()
    response = await client.get(f"{CALLS}/{org.calls[0]}/transcript", headers=bearer(org.key))
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "call_id": str(org.calls[0]),
        "transcripts": detail["transcripts"],
        "concatenated_transcript": detail["concatenated_transcript"],
    }
    assert len(detail["transcripts"]) == 3


@pytest.mark.parametrize(
    ("fmt", "content_type"),
    [
        ("text", "text/plain; charset=utf-8"),
        ("srt", "application/x-subrip; charset=utf-8"),
        ("vtt", "text/vtt; charset=utf-8"),
    ],
)
async def test_text_formats(
    container: SessionContainer, client: httpx.AsyncClient, fmt: str, content_type: str
) -> None:
    org = await _with_turns(container, status="in_progress")
    url = f"{CALLS}/{org.calls[0]}/transcript"
    body = (await client.get(url, headers=bearer(org.key))).json()
    turns = [Transcript.model_validate(entry) for entry in body["transcripts"]]
    expected = {"text": body["concatenated_transcript"], "srt": to_srt(turns), "vtt": to_vtt(turns)}[fmt]
    response = await client.get(url, params={"format": fmt}, headers=bearer(org.key))
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.text == expected
    if fmt != "text":
        assert "Agent: Hi, this is Ava." in response.text


async def test_an_empty_transcript(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await an_org(container, {"status": "queued"})
    url = f"{CALLS}/{org.calls[0]}/transcript"
    body = (await client.get(url, headers=bearer(org.key))).json()
    assert (body["transcripts"], body["concatenated_transcript"]) == ([], "")
    assert (await client.get(url, params={"format": "vtt"}, headers=bearer(org.key))).text == "WEBVTT\n"
    assert (await client.get(url, params={"format": "srt"}, headers=bearer(org.key))).text == ""


async def test_an_unknown_transcript_format_is_invalid(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container, {})
    response = await client.get(
        f"{CALLS}/{org.calls[0]}/transcript", params={"format": "docx"}, headers=bearer(org.key)
    )
    assert error_of(response) == (400, "INVALID_INPUT", "format")


# --- events ---------------------------------------------------------------------------------------------

CATEGORIES: Final = ("queue", "call", "latency", "call", "tool", "call", "route")


async def _with_events(container: SessionContainer) -> SeededOrg:
    org = await an_org(container, {"status": "completed"})
    async with container.database.tenant_session(org.org_id) as session:
        session.add_all(
            CallEvent(
                id=new_id(),
                org_id=org.org_id,
                call_id=org.calls[0],
                seq=seq,
                category=category,
                level="warning" if category == "tool" else "info",
                message=f"event {seq}",
                data={"seq": seq},
            )
            for seq, category in enumerate(CATEGORIES, start=1)
        )
    return org


async def _events(client: httpx.AsyncClient, org: SeededOrg, **params: Any) -> dict[str, Any]:
    response = await client.get(f"{CALLS}/{org.calls[0]}/events", params=params, headers=bearer(org.key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_events_page_by_seq(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await _with_events(container)
    first = await _events(client, org, limit=3)
    assert first["status"] == "success"
    assert [e["seq"] for e in first["events"]] == [1, 2, 3]
    assert first["next_after_seq"] == 3
    event = first["events"][0]
    assert {k: v for k, v in event.items() if k != "created_at"} == {
        "seq": 1,
        "category": "queue",
        "level": "info",
        "message": "event 1",
        "data": {"seq": 1},
    }
    second = await _events(client, org, after_seq=3, limit=3)
    third = await _events(client, org, after_seq=second["next_after_seq"], limit=3)
    assert [e["seq"] for e in second["events"]] == [4, 5, 6]
    assert ([e["seq"] for e in third["events"]], third["next_after_seq"]) == ([7], None)
    assert [e["seq"] for e in (await _events(client, org))["events"]] == [1, 2, 3, 4, 5, 6, 7]


async def test_events_category_filter(container: SessionContainer, client: httpx.AsyncClient) -> None:
    org = await _with_events(container)
    calls_only = await _events(client, org, category="call")
    assert [e["seq"] for e in calls_only["events"]] == [2, 4, 6]
    several = await _events(client, org, category="tool, route,,tool")
    assert [e["seq"] for e in several["events"]] == [5, 7]
    assert len((await _events(client, org, category=" ,"))["events"]) == 7


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"category": "call,bogus"}, "category"),
        ({"limit": "0"}, "limit"),
        ({"limit": "1001"}, "limit"),
        ({"after_seq": "-1"}, "after_seq"),
    ],
)
async def test_invalid_event_parameters(
    container: SessionContainer, client: httpx.AsyncClient, params: dict[str, str], field: str
) -> None:
    org = await _with_events(container)
    response = await client.get(f"{CALLS}/{org.calls[0]}/events", params=params, headers=bearer(org.key))
    assert error_of(response) == (400, "INVALID_INPUT", field)


# --- shared ---------------------------------------------------------------------------------------------

ARTIFACTS: Final = ("recording", "transcript", "events")


@pytest.mark.parametrize("artifact", ARTIFACTS)
@pytest.mark.parametrize("call_id", ["not-a-uuid", ABSENT_ID, "theirs"])
async def test_unknown_or_foreign_calls_are_not_found(
    container: SessionContainer, client: httpx.AsyncClient, artifact: str, call_id: str
) -> None:
    mine = await an_org(container)
    theirs = await _recorded(container)
    target = str(theirs.calls[0]) if call_id == "theirs" else call_id
    response = await client.get(f"{CALLS}/{target}/{artifact}", headers=bearer(mine.key))
    assert error_of(response)[:2] == (404, "CALL_NOT_FOUND")


async def test_active_is_not_read_as_a_call_id(
    container: SessionContainer, client: httpx.AsyncClient
) -> None:
    org = await an_org(container)
    response = await client.get(ACTIVE, headers=bearer(org.key))
    assert response.status_code == 200
    assert "calls" in response.json()


@pytest.mark.parametrize(
    "suffix", ["/active", f"/{ABSENT_ID}/recording", f"/{ABSENT_ID}/transcript", f"/{ABSENT_ID}/events"]
)
async def test_every_artifact_needs_the_read_scope(
    container: SessionContainer, client: httpx.AsyncClient, suffix: str
) -> None:
    org = await an_org(container)
    writer, _ = await store_api_key(container, org.org_id, ("calls:write",))
    assert error_of(await client.get(f"{CALLS}{suffix}", headers=bearer(writer)))[:2] == (403, "FORBIDDEN")
    reader, _ = await store_api_key(container, org.org_id, ("calls:read",))
    assert (await client.get(ACTIVE, headers=bearer(reader))).status_code == 200
