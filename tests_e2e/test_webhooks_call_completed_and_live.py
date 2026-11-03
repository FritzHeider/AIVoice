"""``webhooks_call_completed_and_live`` (specs/30 §5; Decision P06-T08): a call's per-call webhook and an org
endpoint reach a signature-verifying receiver.

The per-call ``webhook`` subscribes to the ``queue`` and ``latency`` live categories and answers the first
``call.completed`` with a 500, so the dispatcher retries it ~10 s later; the org endpoint subscribes to
``call.completed`` and ``call.ended`` in the envelope format. Every request must verify against the secret
rotated at the start (the only response that shows the plaintext).
"""

from __future__ import annotations

import json
import time
from typing import Any, Final

import pytest
from e2e_assertions import CALL_OBJECT_FIELDS, alternating_roles
from e2e_harness import E2eOrg, Harness
from e2e_receiver import Received, Reply, ServedReceiver, WebhookReceiver, signature_failures
from e2e_webhooks import (
    LATENCY_MESSAGE,
    call_object_mismatches,
    envelope_failures,
    live_body_failures,
    retry_gap_ok,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

FIRST_SENTENCE: Final = "Hi, this is Maya from Bella Pasta calling about your reservation."
TASK: Final = "You are Maya from Bella Pasta. Ask when the caller would like to come in, then end the call."
CALL_PATH: Final = "/call"
ORG_PATH: Final = "/org"
LIVE_CATEGORIES: Final = frozenset({"queue", "latency"})
LIVE_TYPES: Final = frozenset({"call.queued", "call.latency"})
COMPLETED: Final = "call.completed"
ENDED: Final = "call.ended"
OK: Final = 200
WEBHOOK_WAIT_S: Final = 180.0
DELIVERY_WAIT_S: Final = 30.0
UNREACHABLE_HINT: Final = (
    "the api container could not reach the receiver: check E2E_RECEIVER_HOST (default host.docker.internal) "
    "and the extra_hosts entries in docker-compose.e2e.yml"
)


def body_of(received: Received) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(received.body)
    return parsed


def check_signatures(receiver: WebhookReceiver, secret: str) -> None:
    """Every request the receiver got verifies with ``secret`` (receipt wall time as ``now``)."""
    failures = {
        f"{request.path} {request.event_type}": problems
        for request in receiver.requests
        if (problems := signature_failures(request, secret, now=request.wall))
    }
    assert not failures, failures


async def preflight(harness: Harness, org: E2eOrg, served: ServedReceiver, secret: str) -> str:
    """Create the org endpoint and prove (test send) that the stack reaches the receiver; the endpoint id."""
    endpoint = await harness.create_endpoint(org, f"{served.url}{ORG_PATH}", [COMPLETED, ENDED])
    result = await harness.send_endpoint_test(org, endpoint["id"])
    assert (result["succeeded"], result["status_code"]) == (True, 200), (UNREACHABLE_HINT, result)
    [test_request] = served.receiver.of(ORG_PATH)
    assert not signature_failures(test_request, secret, now=test_request.wall), test_request.headers
    test_body = body_of(test_request)
    assert (test_body["type"], test_body["data"].get("test")) == (COMPLETED, True), test_body
    endpoint_id: str = endpoint["id"]
    return endpoint_id


def latency_seqs(events: list[dict[str, Any]]) -> set[int]:
    return {event["seq"] for event in events if event["type"] == "call.latency"}


def check_live(receiver: WebhookReceiver, call_id: str, metadata: dict[str, str], seqs: set[int]) -> None:
    """Only subscribed live categories (plus the final Call Object) reached the per-call webhook."""
    requests = receiver.of(CALL_PATH)
    assert {request.event_type for request in requests} <= {*LIVE_TYPES, COMPLETED}, [
        request.event_type for request in requests
    ]
    live = [request for request in requests if request.event_type in LIVE_TYPES]
    failures = {
        request.event_id: problems
        for request in live
        if (
            problems := live_body_failures(
                body_of(request), call_id=call_id, categories=LIVE_CATEGORIES, metadata=metadata
            )
        )
    }
    assert not failures, failures
    queued = {request.event_id for request in live if request.event_type == "call.queued"}
    assert len(queued) == 1, queued
    latency = [body_of(request) for request in live if request.event_type == "call.latency"]
    assert {body["seq"] for body in latency} == seqs, (latency, seqs)
    assert any(LATENCY_MESSAGE.match(body["message"]) for body in latency), latency


def check_retry(receiver: WebhookReceiver) -> dict[str, Any]:
    """The first ``call.completed`` was answered 500 and retried once ~10 s later; the Call Object body."""
    first, second = receiver.of(CALL_PATH, COMPLETED)
    assert (first.status, second.status) == (500, 200)
    assert first.event_id == second.event_id
    assert first.body == second.body
    assert retry_gap_ok(first.at, second.at), second.at - first.at
    return body_of(second)


async def check_deliveries(harness: Harness, org: E2eOrg, call_id: str, endpoint_id: str) -> str:
    """The per-call row succeeded on attempt 2, the endpoint row on attempt 1; the per-call delivery id."""

    def settled(rows: list[dict[str, Any]]) -> bool:
        return len(rows) == 2 and all(row["status"] == "succeeded" for row in rows)

    rows = await harness.wait_deliveries(
        org,
        settled,
        what="call.completed succeeded",
        timeout_s=DELIVERY_WAIT_S,
        call_id=call_id,
        event_type=COMPLETED,
    )
    by_target = {row["endpoint_id"]: row for row in rows}
    per_call, endpoint = by_target[None], by_target[endpoint_id]
    assert (per_call["format"], per_call["attempts"], per_call["last_status_code"]) == ("bland", 2, 200)
    assert per_call["url"].endswith(CALL_PATH), per_call
    assert (endpoint["format"], endpoint["attempts"], endpoint["last_status_code"]) == ("envelope", 1, 200)
    delivery_id: str = per_call["id"]
    return delivery_id


def check_attempt_log(events: list[dict[str, Any]], delivery_id: str) -> None:
    """The call log holds the failed (warning) and the successful (info) attempt of the per-call delivery."""
    attempts = [
        event
        for event in events
        if event["type"] == "call.webhook_delivery" and event["data"].get("delivery_id") == delivery_id
    ]
    levels = [(event["level"], event["data"]["attempt"]) for event in attempts]
    assert levels == [("warning", 1), ("info", 2)], attempts
    failed, delivered = attempts
    assert "failed: 500 (attempt 1), retrying in" in failed["message"], failed
    host = failed["data"]["host"]
    assert delivered["message"].startswith(f"Webhook delivered to {host}: 200"), delivered
    assert CALL_PATH not in failed["message"] + delivered["message"], attempts


def check_call_object(call: dict[str, Any], api_call: dict[str, Any], metadata: dict[str, str]) -> None:
    assert set(call) >= CALL_OBJECT_FIELDS, sorted(CALL_OBJECT_FIELDS - set(call))
    assert (call["status"], call["completed"], call["metadata"]) == ("completed", True, metadata)
    transcripts = call["transcripts"]
    assert (transcripts[0]["user"], transcripts[0]["text"]) == ("assistant", FIRST_SENTENCE)
    assert alternating_roles(transcripts), transcripts
    assert call["recording_url"], call
    assert api_call["recording_url"], api_call
    assert not call_object_mismatches(call, api_call), {
        key: (call.get(key), api_call.get(key)) for key in call_object_mismatches(call, api_call)
    }


def check_org_endpoint(receiver: WebhookReceiver, org: E2eOrg, call_id: str, call: dict[str, Any]) -> None:
    """``call.ended`` and ``call.completed`` reached the endpoint once each, as §7 envelopes."""
    requests = receiver.of(ORG_PATH)[1:]  # the first one is the preflight test event
    assert sorted(request.event_type for request in requests) == [COMPLETED, ENDED], requests
    bodies = {request.event_type: (request, body_of(request)) for request in requests}
    for request, body in bodies.values():
        problems = envelope_failures(body, headers=request.headers, org_id=org.org_id, call_id=call_id)
        assert not problems, (problems, body)
    assert bodies[COMPLETED][1]["data"] == call
    ended = bodies[ENDED][1]["data"]
    assert ended["data"].get("end_reason") == "agent_end_call", ended


async def test_webhooks_call_completed_and_live(harness: Harness, receiver: ServedReceiver) -> None:
    org = await harness.create_org()
    secret = await harness.rotate_signing_secret(org)
    endpoint_id = await preflight(harness, org, receiver, secret)
    receiver.receiver.script(CALL_PATH, Reply(500), event_type=COMPLETED)
    scenario = await harness.load_scenario("outbound_basic")
    metadata = {"run": harness.run_token}
    call_id = await harness.send_task_call(
        org,
        scenario,
        TASK,
        first_sentence=FIRST_SENTENCE,
        record=True,
        webhook=f"{receiver.url}{CALL_PATH}",
        webhook_events=sorted(LIVE_CATEGORIES),
        metadata=metadata,
    )
    started = time.monotonic()
    await receiver.receiver.wait_for(
        lambda: any(request.status == OK for request in receiver.receiver.of(CALL_PATH, COMPLETED)),
        timeout_s=WEBHOOK_WAIT_S,
    )
    print(f"webhooks flow: final Call Object after {time.monotonic() - started:.1f} s")  # noqa: T201
    events = await harness.events(org, call_id)
    seqs = latency_seqs(events)
    assert seqs, events
    await receiver.receiver.wait_for(
        lambda: {body_of(r)["seq"] for r in receiver.receiver.of(CALL_PATH, "call.latency")} >= seqs,
        timeout_s=DELIVERY_WAIT_S,
    )
    delivery_id = await check_deliveries(harness, org, call_id, endpoint_id)
    events = await harness.events(org, call_id)

    check_signatures(receiver.receiver, secret)
    check_live(receiver.receiver, call_id, metadata, seqs)
    call = check_retry(receiver.receiver)
    check_attempt_log(events, delivery_id)
    check_call_object(call, await harness.call_object(org, call_id), metadata)
    check_org_endpoint(receiver.receiver, org, call_id, call)
