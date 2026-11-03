"""Unit tests of the e2e webhook receiver and its pure checks (specs/30 §5 Decision P06-T08)."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

import httpx
import pytest
from e2e_receiver import (
    USER_AGENT,
    Received,
    ReceiverSettings,
    Reply,
    WebhookReceiver,
    hex_hmac,
    parse_signature_header,
    served_receiver,
    signature_failures,
)
from e2e_webhooks import (
    LATENCY_MESSAGE,
    call_object_mismatches,
    envelope_failures,
    iso_z_ms,
    live_body_failures,
    retry_gap_ok,
)

from va_events.webhook_signing import request_headers

NOW = 1_793_700_000
CALL_ID = "01a1a0af-6b7a-77b2-aa11-9f6fdcaf8157"
ORG_ID = "01a1a0af-6af0-76f2-a38b-a42dc1e423cd"
EVENT_ID = "evt_0192f3a45b6c7d8e9f0123456789abcd"


def signed(
    body: bytes, signing: list[str], *, timestamp: int = NOW, event_type: str = "call.completed"
) -> Received:
    """A request as the dispatcher sends it (production signer), headers lower-cased like the receiver."""
    headers = request_headers(
        event_id=EVENT_ID, event_type=event_type, body=body, secrets=signing, timestamp=timestamp
    )
    lowered = {key.lower(): value for key, value in headers.items()}
    return Received("/call", lowered, body, at=0.0, wall=float(NOW), status=200)


def with_header(received: Received, name: str, value: str | None) -> Received:
    headers = dict(received.headers)
    if value is None:
        headers.pop(name, None)
    else:
        headers[name] = value
    return Received(received.path, headers, received.body, received.at, received.wall, received.status)


@pytest.fixture
def secret() -> str:
    return f"whsec_{secrets.token_hex(16)}"


def test_a_single_signature_verifies(secret: str) -> None:
    assert signature_failures(signed(b'{"a":1}', [secret]), secret, now=NOW) == []


def test_a_dual_signature_verifies_with_the_previous_secret_too(secret: str) -> None:
    previous = f"whsec_{secrets.token_hex(16)}"
    received = signed(b'{"a":1}', [secret, previous])
    assert signature_failures(received, secret, now=NOW) == []
    only_second = signature_failures(received, previous, now=NOW)
    assert only_second == ["x-webhook-signature does not match the secret"]


def test_a_wrong_secret_fails(secret: str) -> None:
    failures = signature_failures(signed(b"{}", [secret]), f"whsec_{secrets.token_hex(16)}", now=NOW)
    assert failures == [
        "no v1 in x-va-signature matches the secret",
        "x-webhook-signature does not match the secret",
    ]


def test_a_tampered_body_fails(secret: str) -> None:
    received = signed(b'{"a":1}', [secret])
    tampered = Received(received.path, received.headers, b'{"a":2}', 0.0, float(NOW), 200)
    assert len(signature_failures(tampered, secret, now=NOW)) == 2


def test_the_timestamp_header_must_match_t(secret: str) -> None:
    received = with_header(signed(b"{}", [secret]), "x-va-timestamp", str(NOW + 1))
    assert signature_failures(received, secret, now=NOW) == [f"t={NOW} differs from x-va-timestamp"]


def test_a_stale_timestamp_fails(secret: str) -> None:
    received = signed(b"{}", [secret], timestamp=NOW - 301)
    [failure] = signature_failures(received, secret, now=NOW)
    assert failure.startswith(f"t={NOW - 301} is more than 300.0 s")
    assert signature_failures(signed(b"{}", [secret], timestamp=NOW - 300), secret, now=NOW) == []


def test_missing_headers_are_reported_not_raised(secret: str) -> None:
    received = Received("/call", {}, b"{}", 0.0, float(NOW), 200)
    failures = signature_failures(received, secret, now=NOW)
    assert "x-va-signature missing or malformed" in failures
    assert any("user-agent" in failure for failure in failures)
    assert any("x-va-event-type missing" in failure for failure in failures)


def test_the_event_id_headers_must_agree(secret: str) -> None:
    received = with_header(signed(b"{}", [secret]), "x-event-id", "evt_other")
    assert signature_failures(received, secret, now=NOW) == ["x-event-id differs from x-va-event-id"]


def test_an_unprefixed_event_id_fails(secret: str) -> None:
    received = with_header(with_header(signed(b"{}", [secret]), "x-va-event-id", "abc"), "x-event-id", "abc")
    assert signature_failures(received, secret, now=NOW) == ["x-va-event-id 'abc' does not start with evt_"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("t=12,v1=aa", ("12", ["aa"])),
        ("t=12,v1=aa,v1=bb", ("12", ["aa", "bb"])),
        ("v1=aa,t=12", ("12", ["aa"])),
        ("t=12", None),
        ("t=x,v1=aa", None),
        ("t=12,v2=aa", None),
        ("t=12,v1=", None),
        ("", None),
    ],
)
def test_parse_signature_header(value: str, expected: tuple[str, list[str]] | None) -> None:
    parsed = parse_signature_header(value)
    assert (None if parsed is None else (parsed.timestamp, parsed.versions)) == expected


def test_hex_hmac_is_hmac_sha256(secret: str) -> None:
    assert len(hex_hmac(secret, b"x")) == 64
    assert hex_hmac(secret, b"x") != hex_hmac(secret, b"y")


def live_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "call_id": CALL_ID,
        "category": "latency",
        "level": "info",
        "message": "E2E: 426ms",
        "data": {},
        "seq": 7,
        "created_at": "2026-11-03T09:13:55.158Z",
        "metadata": {"run": "r"},
    }
    return {**body, **overrides}


def live(body: dict[str, Any]) -> list[str]:
    return live_body_failures(body, call_id=CALL_ID, categories={"queue", "latency"}, metadata={"run": "r"})


def test_a_live_body_passes() -> None:
    assert live(live_body()) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"extra": 1}, "keys"),
        ({"category": "call"}, "category"),
        ({"metadata": {}}, "metadata"),
        ({"created_at": "2026-11-03T09:13:55Z"}, "created_at"),
        ({"call_id": ORG_ID}, "call_id"),
        ({"seq": "7"}, "seq"),
    ],
)
def test_live_body_failures(overrides: dict[str, Any], fragment: str) -> None:
    [failure] = live(live_body(**overrides))
    assert failure.startswith(fragment)


def test_iso_z_ms() -> None:
    assert iso_z_ms("2026-11-03T09:13:55.158Z")
    assert not iso_z_ms("2026-11-03T09:13:55.158+00:00")
    assert not iso_z_ms(None)


def envelope(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": EVENT_ID,
        "type": "call.ended",
        "created_at": "2026-11-03T09:13:55.158Z",
        "org_id": ORG_ID,
        "call_id": CALL_ID,
        "batch_id": None,
        "conversation_id": None,
        "data": {},
    }
    return {**body, **overrides}


HEADERS = {"x-va-event-id": EVENT_ID, "x-va-event-type": "call.ended"}


def test_an_envelope_passes() -> None:
    assert envelope_failures(envelope(), headers=HEADERS, org_id=ORG_ID, call_id=CALL_ID) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"id": "evt_other"}, "id"),
        ({"type": "call.completed"}, "type"),
        ({"org_id": CALL_ID}, "org_id"),
        ({"call_id": None}, "call_id"),
        ({"created_at": "yesterday"}, "created_at"),
    ],
)
def test_envelope_failures(overrides: dict[str, Any], fragment: str) -> None:
    [failure] = envelope_failures(envelope(**overrides), headers=HEADERS, org_id=ORG_ID, call_id=CALL_ID)
    assert failure.startswith(fragment)


def test_an_envelope_with_missing_keys_fails() -> None:
    body = envelope()
    del body["batch_id"]
    [failure] = envelope_failures(body, headers=HEADERS, org_id=ORG_ID, call_id=CALL_ID)
    assert failure.startswith("keys")


@pytest.mark.parametrize(
    ("gap", "ok"), [(9.0, True), (8.9, False), (25.0, True), (25.1, False), (11.2, True)]
)
def test_retry_gap_ok(gap: float, ok: bool) -> None:
    assert retry_gap_ok(100.0, 100.0 + gap) is ok


def test_call_object_mismatches_ignore_presigned_fields() -> None:
    webhook = {"call_id": "a", "recording_url": "u1", "recording_expiration": "e1", "price": 1}
    api = {"call_id": "a", "recording_url": "u2", "recording_expiration": "e2", "price": 2, "extra": None}
    assert call_object_mismatches(webhook, api) == ["extra", "price"]
    assert call_object_mismatches(webhook, api, ignore=("price", "extra")) == [
        "recording_expiration",
        "recording_url",
    ]


def test_latency_message() -> None:
    assert LATENCY_MESSAGE.match("E2E: 426ms")
    assert LATENCY_MESSAGE.match("TTS: 22ms")
    assert not LATENCY_MESSAGE.match("E2E: fast")


def test_receiver_settings_from_env() -> None:
    assert ReceiverSettings.from_env({}) == ReceiverSettings("0.0.0.0", 0, "host.docker.internal")  # noqa: S104
    custom = ReceiverSettings.from_env(
        {"E2E_RECEIVER_BIND": "172.17.0.1", "E2E_RECEIVER_PORT": "18080", "E2E_RECEIVER_HOST": "10.0.0.5"}
    )
    assert custom == ReceiverSettings("172.17.0.1", 18080, "10.0.0.5")
    assert custom.url(18080) == "http://10.0.0.5:18080"


async def test_the_receiver_records_and_answers_scripted_replies() -> None:
    receiver = WebhookReceiver()
    receiver.script("/call", Reply(500), event_type="call.completed")
    receiver.script("/org", Reply(503))
    async with served_receiver(receiver, bind="127.0.0.1") as port, httpx.AsyncClient(timeout=5.0) as http:
        base = f"http://127.0.0.1:{port}"

        async def post(path: str, event_type: str) -> int:
            body = json.dumps({"type": event_type}).encode()
            headers = {"X-VA-Event-Type": event_type, "User-Agent": USER_AGENT}
            return (await http.post(f"{base}{path}", content=body, headers=headers)).status_code

        statuses = [
            await post("/call", "call.queued"),
            await post("/call", "call.completed"),
            await post("/call", "call.completed"),
            await post("/org", "call.ended"),
            await post("/org", "call.ended"),
        ]
        await receiver.wait_for(lambda: len(receiver.requests) == 5, timeout_s=5.0)
    assert statuses == [200, 500, 200, 503, 200]
    assert [request.status for request in receiver.requests] == statuses
    completed = receiver.of("/call", "call.completed")
    assert [request.status for request in completed] == [500, 200]
    assert completed[0].at <= completed[1].at
    assert abs(completed[0].wall - time.time()) < 60
    assert completed[0].headers["user-agent"] == USER_AGENT
    assert json.loads(completed[0].body) == {"type": "call.completed"}
    assert len(receiver.of("/org")) == 2


async def test_wait_for_times_out_with_what_was_received() -> None:
    receiver = WebhookReceiver()
    with pytest.raises(AssertionError, match=r"not met within 0\.01 s"):
        await receiver.wait_for(lambda: False, timeout_s=0.01)
