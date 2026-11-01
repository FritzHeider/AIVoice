"""Webhook intake against a migrated database (specs/06-telephony-telnyx.md §4, §4.1; Decision P05-T01)."""

from __future__ import annotations

import os
import secrets
import time
import uuid

import orjson
import pytest
from edge_helpers import (
    Keypair,
    client_state,
    edge_client,
    envelope,
    event_id_of,
    metric,
    post,
    signed_headers,
)
from sqlalchemy import select
from tenant_rows import OrgFactory

from va_db.models import TelnyxAccount, TelnyxEvent
from va_db.session import Database
from va_edge.processor import StoredEvent
from va_telnyx.signatures import TOLERANCE_S
from va_telnyx.webhooks import EventPayload

pytestmark = pytest.mark.integration

PLATFORM = Keypair.generate()


async def _rows(database: Database, telnyx_event_id: str) -> list[TelnyxEvent]:
    async with database.system_session() as session:
        statement = select(TelnyxEvent).where(TelnyxEvent.telnyx_event_id == telnyx_event_id)
        return list((await session.scalars(statement)).all())


async def _account(database: Database, org: uuid.UUID, key: Keypair, status: str = "active") -> uuid.UUID:
    async with database.system_session() as session:
        account = TelnyxAccount(
            org_id=org,
            label="byo",
            api_key_enc=os.urandom(16),
            public_key=key.public_b64,
            encrypted_key_handle=f"tk-{secrets.token_hex(8)}",
            status=status,
        )
        session.add(account)
        await session.flush()
        return account.id


async def _deliver(  # noqa: PLR0913 - keyword-only delivery options
    edge_db_url: str,
    database: Database,
    body: bytes,
    *,
    path: str = "/telnyx/voice",
    key: Keypair = PLATFORM,
    counted: str | None = None,
) -> TelnyxEvent:
    """Deliver ``body`` (answered ``200``), wait for processing, and return its stored row; ``counted`` is the
    ``result`` the event must have been counted once with."""
    async with edge_client(edge_db_url, PLATFORM) as (client, app):
        response = await post(client, path, body, key)
        await app.state.processor.join()
        assert response.status_code == 200, response.text
        if counted is not None:
            assert metric(app, orjson.loads(body)["data"]["event_type"], counted) == 1
    [row] = await _rows(database, event_id_of(body))
    return row


class Recorder:
    def __init__(self) -> None:
        self.seen: list[tuple[StoredEvent, EventPayload]] = []

    async def __call__(self, event: StoredEvent, payload: EventPayload) -> None:
        self.seen.append((event, payload))


async def test_valid_webhook_is_stored_processed_and_attributed(edge_db_url: str, database: Database) -> None:
    org = await OrgFactory(database).create()
    call_id = uuid.uuid4()
    recorder = Recorder()
    payload = {"call_control_id": "v3:leg-1", "client_state": client_state(org, call_id), "extra": [1, 2]}
    body = envelope("call.answered", payload=payload)
    event_id = event_id_of(body)
    async with edge_client(edge_db_url, PLATFORM, handlers={"call.answered": recorder}) as (client, app):
        response = await post(client, "/telnyx/voice", body, PLATFORM)
        await app.state.processor.join()
        assert (response.status_code, response.json()) == (200, {"status": "ok"})
        assert metric(app, "call.answered", "processed") == 1
        histogram = app.state.metrics.registry.get_sample_value(
            "va_telnyx_webhook_processing_ms_count", {"event_type": "call.answered"}
        )
        assert histogram == 1
    [row] = await _rows(database, event_id)
    assert (row.event_type, row.org_id, row.call_id, row.call_control_id) == (
        "call.answered",
        org,
        call_id,
        "v3:leg-1",
    )
    assert row.payload["data"]["payload"]["extra"] == [1, 2]
    assert row.payload["meta"] == {"attempt": 1, "delivered_to": "https://edge.test/telnyx/voice"}
    assert (row.process_error, row.process_attempts) == (None, 1)
    assert row.processed_at is not None
    [(event, parsed)] = recorder.seen
    assert (event.id, event.call_id, getattr(parsed, "call_control_id", None)) == (
        row.id,
        call_id,
        "v3:leg-1",
    )


_SKEW_S = TOLERANCE_S + 60  # a minute of slack beyond the verification window


def _tampered(body: bytes, tamper: str, now: int) -> tuple[bytes, dict[str, str]]:
    headers = signed_headers(body, PLATFORM, timestamp=now)
    sent = body
    if tamper == "wrong_key":
        headers = signed_headers(body, Keypair.generate(), timestamp=now)
    elif tamper == "tampered_body":
        sent = body.replace(b"call.answered", b"call.hangup")
    elif tamper in {"stale", "future"}:
        skewed = now - _SKEW_S if tamper == "stale" else now + _SKEW_S
        headers = signed_headers(body, PLATFORM, timestamp=skewed)
    elif tamper == "missing_signature":
        del headers["telnyx-signature-ed25519"]
    elif tamper == "missing_timestamp":
        del headers["telnyx-timestamp"]
    else:
        headers["telnyx-signature-ed25519"] = "not base64!"
    return sent, headers


@pytest.mark.parametrize(
    "tamper",
    ["wrong_key", "tampered_body", "stale", "future", "missing_signature", "missing_timestamp", "garbage"],
)
async def test_unverified_webhooks_are_rejected_and_not_stored(
    edge_db_url: str, database: Database, tamper: str
) -> None:
    body = envelope("call.answered")
    event_id = event_id_of(body)
    async with edge_client(edge_db_url, PLATFORM) as (client, app):
        # Sign after startup, right before the request: the verifier reads the wall clock, so a timestamp
        # taken before the app started drifts towards the tolerance window. _SKEW_S keeps a minute of slack
        # beyond TOLERANCE_S, so elapsed time can never make a stale/future timestamp valid.
        sent, headers = _tampered(body, tamper, int(time.time()))
        response = await client.post("/telnyx/voice", content=sent, headers=headers)
        assert (response.status_code, response.json()) == (401, {"error": "invalid_signature"})
        assert metric(app, "unverified", "invalid_signature") == 1
    assert await _rows(database, event_id) == []


async def test_duplicate_delivery_is_stored_and_handled_once(edge_db_url: str, database: Database) -> None:
    recorder = Recorder()
    body = envelope("call.hangup")
    event_id = event_id_of(body)
    async with edge_client(edge_db_url, PLATFORM, handlers={"call.hangup": recorder}) as (client, app):
        first = await post(client, "/telnyx/voice", body, PLATFORM)
        await app.state.processor.join()
        second = await post(client, "/telnyx/voice", body, PLATFORM)
        await app.state.processor.join()
        assert [first.json(), second.json()] == [{"status": "ok"}, {"status": "duplicate"}]
        assert second.status_code == 200
        assert metric(app, "call.hangup", "duplicate") == 1
        assert metric(app, "call.hangup", "processed") == 1
    assert len(await _rows(database, event_id)) == 1
    assert len(recorder.seen) == 1


async def test_unknown_event_type_is_stored_and_counted_unhandled(
    edge_db_url: str, database: Database
) -> None:
    body = envelope("call.teleported", payload={"anything": True})
    row = await _deliver(edge_db_url, database, body, counted="unhandled")
    assert row.processed_at is not None
    assert (row.process_error, row.org_id, row.call_control_id) == (None, None, None)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"data": {"id": "x"}}', b'{"meta": {}}'])
async def test_verified_but_malformed_envelope_is_400(edge_db_url: str, body: bytes) -> None:
    async with edge_client(edge_db_url, PLATFORM) as (client, app):
        response = await post(client, "/telnyx/voice", body, PLATFORM)
        assert (response.status_code, response.json()) == (400, {"error": "invalid_payload"})
        assert metric(app, "invalid", "error") == 1


async def test_known_type_with_malformed_payload_records_the_error(
    edge_db_url: str, database: Database
) -> None:
    body = envelope("call.answered", payload={"no_call_control_id": True})
    row = await _deliver(edge_db_url, database, body, counted="error")
    assert (row.process_attempts, (row.process_error or "").split(":")[0]) == (1, "WebhookParseError")


async def test_messaging_route_stores_inbound_messages(edge_db_url: str, database: Database) -> None:
    payload = {"id": "msg-1", "direction": "inbound", "from": {"phone_number": "+15550100"}, "text": "hi"}
    body = envelope("message.received", payload=payload)
    row = await _deliver(edge_db_url, database, body, path="/telnyx/messaging", counted="processed")
    assert (row.event_type, row.call_control_id) == ("message.received", None)


async def test_client_state_of_a_missing_org_stores_null_org(edge_db_url: str, database: Database) -> None:
    payload = {"call_control_id": "v3:x", "client_state": client_state(uuid.uuid4())}
    row = await _deliver(edge_db_url, database, envelope("call.answered", payload=payload))
    assert row.org_id is None
    assert row.call_id is not None


async def test_undecodable_client_state_is_ignored(edge_db_url: str, database: Database) -> None:
    body = envelope("call.answered", payload={"call_control_id": "v3:y", "client_state": "%%%"})
    row = await _deliver(edge_db_url, database, body)
    assert (row.org_id, row.call_id, row.call_control_id) == (None, None, "v3:y")


@pytest.mark.parametrize("channel", ["voice", "messaging"])
async def test_byo_route_verifies_with_the_account_key(
    edge_db_url: str, database: Database, channel: str
) -> None:
    org = await OrgFactory(database).create()
    byo = Keypair.generate()
    account = await _account(database, org, byo)
    body = envelope("call.answered")
    event_id = event_id_of(body)
    async with edge_client(edge_db_url, PLATFORM) as (client, app):
        assert (await post(client, f"/telnyx/{channel}/{account}", body, PLATFORM)).status_code == 401
        assert (await post(client, f"/telnyx/{channel}", body, byo)).status_code == 401
        assert (await post(client, f"/telnyx/{channel}/{account}", body, byo)).status_code == 200
        assert metric(app, "unverified", "invalid_signature") == 2
    [row] = await _rows(database, event_id)
    assert row.org_id == org


async def test_byo_route_ignores_client_state_of_another_org(edge_db_url: str, database: Database) -> None:
    org, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    byo = Keypair.generate()
    account = await _account(database, org, byo)
    body = envelope("call.answered", payload={"call_control_id": "v3:z", "client_state": client_state(other)})
    row = await _deliver(edge_db_url, database, body, path=f"/telnyx/voice/{account}", key=byo)
    assert (row.org_id, row.call_id) == (org, None)


async def test_byo_route_keeps_client_state_of_its_own_org(edge_db_url: str, database: Database) -> None:
    org = await OrgFactory(database).create()
    byo = Keypair.generate()
    account = await _account(database, org, byo, status="pending")
    call_id = uuid.uuid4()
    payload = {"call_control_id": "v3:o", "client_state": client_state(org, call_id)}
    body = envelope("call.answered", payload=payload)
    row = await _deliver(edge_db_url, database, body, path=f"/telnyx/voice/{account}", key=byo)
    assert (row.org_id, row.call_id) == (org, call_id)


@pytest.mark.parametrize("account", ["deleted", "unknown", "not-a-uuid"])
async def test_byo_route_of_unusable_account_is_401(
    edge_db_url: str, database: Database, account: str
) -> None:
    byo = Keypair.generate()
    if account == "deleted":
        path_id = str(await _account(database, await OrgFactory(database).create(), byo, status="deleted"))
    elif account == "unknown":
        path_id = str(uuid.uuid4())
    else:
        path_id = account
    body = envelope("call.answered")
    async with edge_client(edge_db_url, PLATFORM) as (client, app):
        response = await post(client, f"/telnyx/voice/{path_id}", body, byo)
        assert (response.status_code, response.json()) == (401, {"error": "invalid_signature"})
        assert metric(app, "unverified", "invalid_signature") == 1
