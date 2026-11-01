"""Bootstrap against FakeTelnyx: idempotency, exact-name matching, drift repair, duplicates
(specs/06-telephony-telnyx.md §12, §13 "Bootstrap idempotency"; Decision 2026-11-01 P03-T15a)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from service_helpers import json_log_stream, log_events
from telnyx_client_helpers import sim_clients
from telnyx_helpers import telnyx_state

from va_telnyx import (
    BootstrapResult,
    bootstrap_account,
    org_resource_name,
    platform_resource_name,
    webhook_urls,
)

NAME = "voiceagents-test"
HOOKS = webhook_urls("edge.example.test")
ACCOUNT = uuid.UUID("0192f3a0-3333-7000-8000-000000000001")


async def _listed(raw: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    response = await raw.get(f"/v2/{path}", params={"page[size]": 250})
    assert response.status_code == 200
    items: list[dict[str, Any]] = response.json()["data"]
    return items


def test_resource_names_and_webhooks() -> None:
    assert platform_resource_name("local") == "voiceagents-local"
    assert org_resource_name(ACCOUNT) == f"voiceagents-org-{ACCOUNT}"
    assert org_resource_name("abc") == "voiceagents-org-abc"
    assert webhook_urls("localhost:8100").voice == "https://localhost:8100/telnyx/voice"
    assert webhook_urls("localhost:8100").messaging == "https://localhost:8100/telnyx/messaging"
    byo = webhook_urls("edge.example.test", ACCOUNT)
    assert byo.voice == f"https://edge.example.test/telnyx/voice/{ACCOUNT}"
    assert byo.messaging == f"https://edge.example.test/telnyx/messaging/{ACCOUNT}"


def test_env_values_follow_the_spec_order() -> None:
    result = BootstrapResult(call_control_app_id="a", outbound_voice_profile_id="p", messaging_profile_id="m")
    assert list(result.env_values().items()) == [
        ("TELNYX_CALL_CONTROL_APP_ID", "a"),
        ("TELNYX_OUTBOUND_VOICE_PROFILE_ID", "p"),
        ("TELNYX_MESSAGING_PROFILE_ID", "m"),
    ]


async def test_first_run_creates_and_second_run_changes_nothing() -> None:
    async with sim_clients(telnyx_state()) as (raw, client):
        first = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        second = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        apps = await _listed(raw, "call_control_applications")
        profiles = await _listed(raw, "outbound_voice_profiles")
        messaging = await _listed(raw, "messaging_profiles")
    assert first.created == ("outbound_voice_profile", "call_control_app", "messaging_profile")
    assert first.updated == ()
    assert (second.created, second.updated) == ((), ())
    assert second.env_values() == first.env_values()
    assert [(a["id"], a["application_name"]) for a in apps] == [(first.call_control_app_id, NAME)]
    assert apps[0]["webhook_event_url"] == HOOKS.voice
    assert apps[0]["outbound"]["outbound_voice_profile_id"] == first.outbound_voice_profile_id
    assert [(p["id"], p["name"]) for p in profiles] == [(first.outbound_voice_profile_id, NAME)]
    assert [(m["id"], m["name"], m["webhook_url"]) for m in messaging] == [
        (first.messaging_profile_id, NAME, HOOKS.messaging)
    ]


async def test_contains_matches_are_not_reused() -> None:
    async with sim_clients(telnyx_state()) as (_raw, client):
        old_profile = await client.create_outbound_voice_profile(f"{NAME}-old")
        old_app = await client.create_call_control_app(
            f"{NAME}-old", HOOKS.voice, outbound_voice_profile_id=old_profile.id
        )
        result = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
    assert result.created == ("outbound_voice_profile", "call_control_app", "messaging_profile")
    assert result.outbound_voice_profile_id != old_profile.id
    assert result.call_control_app_id != old_app.id


async def test_app_webhook_drift_is_patched() -> None:
    async with sim_clients(telnyx_state()) as (raw, client):
        first = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        drift = await raw.patch(
            f"/v2/call_control_applications/{first.call_control_app_id}",
            json={"webhook_event_url": "https://stale.example.test/telnyx/voice"},
        )
        assert drift.status_code == 200
        repaired = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        [app] = await _listed(raw, "call_control_applications")
    assert (repaired.created, repaired.updated) == ((), ("call_control_app",))
    assert repaired.env_values() == first.env_values()
    assert app["webhook_event_url"] == HOOKS.voice


async def test_app_profile_drift_is_patched() -> None:
    async with sim_clients(telnyx_state()) as (raw, client):
        first = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        other = await client.create_outbound_voice_profile("unrelated")
        await client.update_call_control_app(first.call_control_app_id, outbound_voice_profile_id=other.id)
        repaired = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        [app] = await _listed(raw, "call_control_applications")
    assert repaired.updated == ("call_control_app",)
    assert app["outbound"]["outbound_voice_profile_id"] == first.outbound_voice_profile_id


async def test_messaging_webhook_drift_is_patched() -> None:
    async with sim_clients(telnyx_state()) as (raw, client):
        first = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        await client.update_messaging_profile(
            first.messaging_profile_id, webhook_url="https://stale.test/sms"
        )
        repaired = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
        [profile] = await _listed(raw, "messaging_profiles")
    assert (repaired.created, repaired.updated) == ((), ("messaging_profile",))
    assert profile["webhook_url"] == HOOKS.messaging


async def test_byo_names_and_webhooks_are_used_as_given() -> None:
    name, hooks = org_resource_name(ACCOUNT), webhook_urls("edge.example.test", ACCOUNT)
    async with sim_clients(telnyx_state()) as (raw, client):
        await bootstrap_account(client, name=name, webhooks=hooks)
        [app] = await _listed(raw, "call_control_applications")
        [profile] = await _listed(raw, "messaging_profiles")
    assert (app["application_name"], app["webhook_event_url"]) == (name, hooks.voice)
    assert profile["webhook_url"] == hooks.messaging


async def test_duplicate_exact_names_use_the_first_and_warn() -> None:
    stream = json_log_stream()
    async with sim_clients(telnyx_state()) as (_raw, client):
        profile = await client.create_outbound_voice_profile(NAME)
        first_app = await client.create_call_control_app(
            NAME, HOOKS.voice, outbound_voice_profile_id=profile.id
        )
        await client.create_call_control_app(NAME, HOOKS.voice, outbound_voice_profile_id=profile.id)
        result = await bootstrap_account(client, name=NAME, webhooks=HOOKS)
    assert result.call_control_app_id == first_app.id
    assert result.created == ("messaging_profile",)
    [warning] = [e for e in log_events(stream) if e["event"] == "telnyx_bootstrap.duplicate_resources"]
    assert warning["level"] == "warning"
    assert (warning["kind"], warning["name"], warning["count"], warning["chosen_id"]) == (
        "call_control_app",
        NAME,
        2,
        first_app.id,
    )
