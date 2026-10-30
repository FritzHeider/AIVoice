"""Call control applications, outbound voice profiles, messaging profiles (specs/06 §2.1)."""

from __future__ import annotations

import pytest
from telnyx_helpers import (
    APP_URL,
    advance,
    create_app,
    create_messaging_profile,
    error_of,
    telnyx_client,
    telnyx_state,
)
from telnyx_shapes import (
    CallControlApplication,
    MessagingProfile,
    OutboundVoiceProfile,
    PageMeta,
    assert_shape,
    assert_shapes,
)

APPS = "/v2/call_control_applications"
VOICE_PROFILES = "/v2/outbound_voice_profiles"
MESSAGING_PROFILES = "/v2/messaging_profiles"


async def test_app_with_required_fields_gets_the_spec_defaults() -> None:
    async with telnyx_client(telnyx_state()) as client:
        app = await create_app(client)
    parsed = assert_shape(CallControlApplication, app)
    assert len(parsed.id) == 19
    assert parsed.id.isdigit()
    assert parsed.webhook_event_url == APP_URL
    assert (parsed.webhook_api_version, parsed.webhook_event_failover_url, parsed.webhook_timeout_secs) == (
        "2",
        "",
        25,
    )
    assert (parsed.first_command_timeout, parsed.first_command_timeout_secs) == (True, 30)
    assert (parsed.anchorsite_override, parsed.dtmf_type, parsed.active) == ("Latency", "RFC 2833", True)
    assert app["inbound"] == {
        "channel_limit": None,
        "sip_subdomain": None,
        "sip_subdomain_receive_settings": "only_my_connections",
        "shaken_stir_enabled": False,
    }
    assert app["outbound"] == {"outbound_voice_profile_id": None, "channel_limit": None}
    assert parsed.created_at == parsed.updated_at == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"application_name": "a", "webhook_event_url": APP_URL, "webhook_url": APP_URL}, "/webhook_url"),
        ({"application_name": "a", "webhook_event_url": APP_URL, "dtmf_type": "rfc"}, "/dtmf_type"),
        ({"webhook_event_url": APP_URL}, "/application_name"),
        ({"application_name": "a", "webhook_event_url": "ftp://x"}, "/webhook_event_url"),
        ({"application_name": "a", "webhook_event_url": APP_URL, "inbound": {"bogus": 1}}, "/inbound/bogus"),
        (
            {"application_name": "a", "webhook_event_url": APP_URL, "webhook_timeout_secs": 31},
            "/webhook_timeout_secs",
        ),
    ],
)
async def test_invalid_app_bodies_get_10015_with_a_pointer(body: dict[str, object], pointer: str) -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.post(APPS, json=body)
    assert error_of(response) == (422, "10015", pointer)


async def test_unknown_field_detail_and_non_object_bodies() -> None:
    async with telnyx_client(telnyx_state()) as client:
        unknown = await client.post(
            APPS, json={"application_name": "a", "webhook_event_url": APP_URL, "x": 1}
        )
        array = await client.post(APPS, json=[1])
        broken = await client.post(APPS, content=b"{")
    assert unknown.json()["errors"][0]["detail"] == "is not a recognised parameter"
    assert unknown.json()["errors"][0]["title"] == "Invalid value"
    assert error_of(array) == (422, "10015", "")
    assert error_of(broken) == (422, "10015", "")


async def test_app_outbound_profile_must_exist() -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.post(
            APPS,
            json={
                "application_name": "a",
                "webhook_event_url": APP_URL,
                "outbound": {"outbound_voice_profile_id": "1"},
            },
        )
    assert error_of(response) == (422, "10015", "/outbound/outbound_voice_profile_id")


async def test_app_list_filter_get_and_nested_patch() -> None:
    state = telnyx_state()
    async with telnyx_client(state) as client:
        profile = (await client.post(VOICE_PROFILES, json={"name": "voiceagents-test"})).json()["data"]
        first = await create_app(client, application_name="VoiceAgents-Local", outbound={"channel_limit": 5})
        await create_app(client, application_name="other")
        listed = await client.get(APPS, params={"filter[application_name][contains]": "voiceagents"})
        advance(state, 5)
        patched = await client.patch(
            f"{APPS}/{first['id']}",
            json={
                "webhook_event_url": "http://edge:8100/v2",
                "outbound": {"outbound_voice_profile_id": profile["id"]},
            },
        )
        bad_patch = await client.patch(f"{APPS}/{first['id']}", json={"dtmf_type": "nope"})
        bad_profile = await client.patch(
            f"{APPS}/{first['id']}", json={"outbound": {"outbound_voice_profile_id": "404"}}
        )
        fetched = await client.get(f"{APPS}/{first['id']}")
        voice_profile = await client.get(f"{VOICE_PROFILES}/{profile['id']}")
        missing = await client.get(f"{APPS}/123")
        missing_patch = await client.patch(f"{APPS}/123", json={})
        not_object = await client.patch(f"{APPS}/{first['id']}", json=[])
    [only] = listed.json()["data"]
    assert only["id"] == first["id"]
    assert_shape(PageMeta, listed.json()["meta"])
    data = patched.json()["data"]
    assert data["outbound"] == {"outbound_voice_profile_id": profile["id"], "channel_limit": 5}
    assert data["webhook_event_url"] == "http://edge:8100/v2"
    assert (data["created_at"], data["updated_at"]) == ("2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z")
    assert error_of(bad_patch) == (422, "10015", "/dtmf_type")
    assert error_of(bad_profile) == (422, "10015", "/outbound/outbound_voice_profile_id")
    assert fetched.json()["data"] == data
    assert voice_profile.json()["data"]["connections_count"] == 1
    assert error_of(missing) == (404, "10005", None)
    assert error_of(missing_patch) == (404, "10005", None)
    assert error_of(not_object) == (422, "10015", "")


async def test_voice_profiles_create_list_patch() -> None:
    async with telnyx_client(telnyx_state()) as client:
        created = await client.post(VOICE_PROFILES, json={"name": "voiceagents-local"})
        await client.post(VOICE_PROFILES, json={"name": "else", "whitelisted_destinations": ["GB"]})
        bad = await client.post(VOICE_PROFILES, json={"name": "x", "whitelisted_destinations": ["usa"]})
        profile = created.json()["data"]
        listed = await client.get(VOICE_PROFILES, params={"filter[name][contains]": "LOCAL"})
        patched = await client.patch(f"{VOICE_PROFILES}/{profile['id']}", json={"concurrent_call_limit": 10})
    assert created.status_code == 201
    parsed = assert_shape(OutboundVoiceProfile, profile)
    assert (parsed.traffic_type, parsed.service_plan, parsed.enabled) == ("conversational", "global", True)
    assert parsed.whitelisted_destinations == ["US", "CA"]
    assert (parsed.connections_count, parsed.usage_payment_method) == (0, "rate-deck")
    assert error_of(bad) == (422, "10015", "/whitelisted_destinations/0")
    assert [item["id"] for item in listed.json()["data"]] == [profile["id"]]
    assert patched.json()["data"]["concurrent_call_limit"] == 10


async def test_messaging_profiles_require_destinations_and_filter_by_exact_name() -> None:
    async with telnyx_client(telnyx_state()) as client:
        missing = await client.post(MESSAGING_PROFILES, json={"name": "voiceagents-local"})
        profile = await create_messaging_profile(
            client, name="voiceagents-local", webhook_url="http://edge:8100/m"
        )
        await create_messaging_profile(client, name="voiceagents-local-2")
        exact = await client.get(MESSAGING_PROFILES, params={"filter[name]": "voiceagents-local"})
        patched = await client.patch(f"{MESSAGING_PROFILES}/{profile['id']}", json={"enabled": False})
        everything = await client.get(MESSAGING_PROFILES)
    assert error_of(missing) == (422, "10015", "/whitelisted_destinations")
    assert missing.json()["errors"][0]["detail"] == "is required"
    parsed = assert_shape(MessagingProfile, profile)
    assert (parsed.webhook_api_version, parsed.webhook_url) == ("2", "http://edge:8100/m")
    assert [item["id"] for item in exact.json()["data"]] == [profile["id"]]
    assert patched.json()["data"]["enabled"] is False
    assert len(assert_shapes(MessagingProfile, everything.json()["data"])) == 2


async def test_pagination() -> None:
    async with telnyx_client(telnyx_state()) as client:
        for index in range(5):
            await client.post(VOICE_PROFILES, json={"name": f"p{index}"})
        second = await client.get(VOICE_PROFILES, params={"page[number]": 2, "page[size]": 2})
        beyond = await client.get(VOICE_PROFILES, params={"page[number]": 9, "page[size]": 2})
        too_big = await client.get(VOICE_PROFILES, params={"page[size]": 251})
        not_int = await client.get(VOICE_PROFILES, params={"page[number]": "x"})
    assert [item["name"] for item in second.json()["data"]] == ["p2", "p3"]
    assert second.json()["meta"] == {"page_number": 2, "page_size": 2, "total_pages": 3, "total_results": 5}
    assert beyond.json()["data"] == []
    assert error_of(too_big) == (422, "10015", "page[size]")
    assert error_of(not_int) == (422, "10015", "page[number]")
