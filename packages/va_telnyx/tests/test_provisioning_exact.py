"""Provisioning requests: exact paths, query strings, and bodies; list paging; balance parsing
(specs/06-telephony-telnyx.md §2.1, §12; Decision 2026-11-01 P03-T08b)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from telnyx_client_helpers import body_of, rig

from va_telnyx import Balance, CallControlApp, MessagingProfile, OutboundVoiceProfile, TelnyxApiError
from va_telnyx.provisioning_models import AppOutboundSettings
from va_telnyx.transport import MAX_PAGES

WEBHOOK = "https://edge.example.test/telnyx/voice"
SMS_WEBHOOK = "https://edge.example.test/telnyx/messaging"


def _app(app_id: str = "1293384261075731499", **fields: Any) -> dict[str, Any]:
    return {
        "id": app_id,
        "record_type": "call_control_application",
        "application_name": "voiceagents-test",
        "webhook_event_url": WEBHOOK,
        "outbound": {"outbound_voice_profile_id": "1293384261075731500", "channel_limit": None},
        "active": True,
        **fields,
    }


def _page(items: list[dict[str, Any]], number: int, total_pages: int) -> httpx.Response:
    meta = {"page_number": number, "page_size": 250, "total_pages": total_pages, "total_results": 0}
    return httpx.Response(200, json={"data": items, "meta": meta})


def _query(request: httpx.Request) -> list[tuple[str, str]]:
    return request.url.params.multi_items()


async def test_create_call_control_app_sends_the_spec_row(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post("/call_control_applications").respond(201, json={"data": _app()})
    async with httpx.AsyncClient() as http:
        app = await rig(http).client.create_call_control_app(
            "voiceagents-test", WEBHOOK, outbound_voice_profile_id="1293384261075731500"
        )
    assert body_of(route.calls[0].request) == {
        "application_name": "voiceagents-test",
        "webhook_event_url": WEBHOOK,
        "webhook_api_version": "2",
        "webhook_event_failover_url": "",
        "webhook_timeout_secs": 25,
        "first_command_timeout": True,
        "first_command_timeout_secs": 30,
        "anchorsite_override": "Latency",
        "dtmf_type": "RFC 2833",
        "inbound": {"channel_limit": None, "sip_subdomain_receive_settings": "only_my_connections"},
        "outbound": {"outbound_voice_profile_id": "1293384261075731500"},
    }
    assert app == CallControlApp(
        id="1293384261075731499",
        application_name="voiceagents-test",
        webhook_event_url=WEBHOOK,
        outbound=AppOutboundSettings(outbound_voice_profile_id="1293384261075731500"),
    )
    assert app.outbound_voice_profile_id == "1293384261075731500"


async def test_numeric_ids_are_read_as_strings(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.post("/outbound_voice_profiles").respond(
        201, json={"data": {"id": 1293384261075731500, "name": "voiceagents-test"}}
    )
    async with httpx.AsyncClient() as http:
        profile = await rig(http).client.create_outbound_voice_profile("voiceagents-test")
    assert profile.id == "1293384261075731500"


async def test_list_call_control_apps_follows_every_page(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/call_control_applications").mock(
        side_effect=[_page([_app("1")], 1, 2), _page([_app("2")], 2, 2)]
    )
    async with httpx.AsyncClient() as http:
        apps = await rig(http).client.list_call_control_apps("voiceagents-")
    assert [app.id for app in apps] == ["1", "2"]
    assert [_query(call.request) for call in route.calls] == [
        [
            ("filter[application_name][contains]", "voiceagents-"),
            ("page[size]", "250"),
            ("page[number]", "1"),
        ],
        [
            ("filter[application_name][contains]", "voiceagents-"),
            ("page[size]", "250"),
            ("page[number]", "2"),
        ],
    ]


async def test_paging_stops_after_the_page_cap(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/outbound_voice_profiles").mock(
        side_effect=lambda request: _page(
            [{"id": request.url.params["page[number]"], "name": "voiceagents-x"}], 1, 99
        )
    )
    async with httpx.AsyncClient() as http:
        profiles = await rig(http).client.list_outbound_voice_profiles("voiceagents-x")
    assert route.call_count == MAX_PAGES
    assert [p.id for p in profiles] == [str(n) for n in range(1, MAX_PAGES + 1)]
    assert _query(route.calls[0].request)[0] == ("filter[name][contains]", "voiceagents-x")


async def test_a_list_without_meta_is_one_page(telnyx_api: respx.MockRouter) -> None:
    profile = {"id": "p1", "name": "voiceagents-test", "webhook_url": SMS_WEBHOOK}
    route = telnyx_api.get("/messaging_profiles").respond(200, json={"data": [profile]})
    async with httpx.AsyncClient() as http:
        profiles = await rig(http).client.list_messaging_profiles("voiceagents-test")
    assert profiles == [MessagingProfile(id="p1", name="voiceagents-test", webhook_url=SMS_WEBHOOK)]
    assert _query(route.calls[0].request) == [
        ("filter[name]", "voiceagents-test"),
        ("page[size]", "250"),
        ("page[number]", "1"),
    ]


async def test_a_list_whose_data_is_not_an_array_is_invalid(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.get("/messaging_profiles").respond(200, json={"data": {"id": "p1"}})
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.list_messaging_profiles("x")
    assert caught.value.errors[0].code == "invalid_response"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"webhook_url": WEBHOOK}, {"webhook_event_url": WEBHOOK}),
        ({"outbound_voice_profile_id": "42"}, {"outbound": {"outbound_voice_profile_id": "42"}}),
        (
            {"webhook_url": WEBHOOK, "outbound_voice_profile_id": "42"},
            {"webhook_event_url": WEBHOOK, "outbound": {"outbound_voice_profile_id": "42"}},
        ),
    ],
)
async def test_update_call_control_app_sends_only_given_fields(
    telnyx_api: respx.MockRouter, kwargs: dict[str, str], expected: dict[str, Any]
) -> None:
    route = telnyx_api.patch("/call_control_applications/7").respond(200, json={"data": _app("7")})
    async with httpx.AsyncClient() as http:
        app = await rig(http).client.update_call_control_app("7", **kwargs)
    assert app.id == "7"
    assert body_of(route.calls[0].request) == expected


async def test_an_empty_update_raises_before_any_request(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.patch("/call_control_applications/7").respond(200, json={"data": _app("7")})
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="webhook_url or outbound_voice_profile_id"):
            await rig(http).client.update_call_control_app("7")
    assert not route.called


async def test_create_outbound_voice_profile_body(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post("/outbound_voice_profiles").respond(
        201,
        json={"data": {"id": "9", "name": "voiceagents-org", "whitelisted_destinations": ["US", "CA", "GB"]}},
    )
    async with httpx.AsyncClient() as http:
        profile = await rig(http).client.create_outbound_voice_profile(
            "voiceagents-org", whitelisted_destinations=["US", "CA", "GB"]
        )
    assert body_of(route.calls[0].request) == {
        "name": "voiceagents-org",
        "traffic_type": "conversational",
        "service_plan": "global",
        "concurrent_call_limit": None,
        "enabled": True,
        "whitelisted_destinations": ["US", "CA", "GB"],
    }
    assert profile == OutboundVoiceProfile(
        id="9", name="voiceagents-org", whitelisted_destinations=("US", "CA", "GB")
    )


async def test_create_and_update_messaging_profile_bodies(telnyx_api: respx.MockRouter) -> None:
    profile = {"id": "mp-1", "name": "voiceagents-test", "webhook_url": SMS_WEBHOOK}
    create = telnyx_api.post("/messaging_profiles").respond(200, json={"data": profile})
    update = telnyx_api.patch("/messaging_profiles/mp-1").respond(200, json={"data": profile})
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        await client.create_messaging_profile("voiceagents-test", SMS_WEBHOOK)
        updated = await client.update_messaging_profile("mp-1", webhook_url=SMS_WEBHOOK)
    assert body_of(create.calls[0].request) == {
        "name": "voiceagents-test",
        "webhook_url": SMS_WEBHOOK,
        "webhook_api_version": "2",
        "whitelisted_destinations": ["US", "CA"],
        "enabled": True,
    }
    assert body_of(update.calls[0].request) == {"webhook_url": SMS_WEBHOOK}
    assert updated.webhook_url == SMS_WEBHOOK


async def test_get_balance_parses_decimals(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/balance").respond(
        200,
        json={
            "data": {
                "record_type": "balance",
                "balance": "300.00",
                "credit_limit": "100.00",
                "available_credit": "400.00",
                "pending": "10.00",
                "currency": "USD",
            }
        },
    )
    async with httpx.AsyncClient() as http:
        balance = await rig(http).client.get_balance()
    assert balance == Balance(
        balance=Decimal("300.00"),
        credit_limit=Decimal("100.00"),
        available_credit=Decimal("400.00"),
        currency="USD",
    )
    assert route.calls[0].request.method == "GET"
    assert route.calls[0].request.content == b""
