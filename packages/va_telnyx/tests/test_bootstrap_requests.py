"""Bootstrap requests: drift PATCH bodies carry only the drifted fields, nothing is sent when nothing drifted,
errors propagate (specs/06-telephony-telnyx.md §12; Decision 2026-11-01 P03-T15a rule 4)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from telnyx_client_helpers import body_of, rig

from va_telnyx import TelnyxApiError, bootstrap_account, webhook_urls

NAME = "voiceagents-test"
HOOKS = webhook_urls("edge.example.test")
PROFILE_ID = "1293384261075731500"
APP_ID = "1293384261075731499"
MESSAGING_ID = "40017f8e-49bd-4f16-9e3d-ef103f916228"


def _page(*items: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(items), "meta": {"page_number": 1, "page_size": 250, "total_pages": 1}}


def _app(webhook: str = HOOKS.voice, profile_id: str = PROFILE_ID) -> dict[str, Any]:
    return {
        "id": APP_ID,
        "application_name": NAME,
        "webhook_event_url": webhook,
        "outbound": {"outbound_voice_profile_id": profile_id},
    }


def _existing(telnyx_api: respx.MockRouter, app: dict[str, Any], sms_webhook: str = HOOKS.messaging) -> None:
    telnyx_api.get("/outbound_voice_profiles").respond(200, json=_page({"id": PROFILE_ID, "name": NAME}))
    telnyx_api.get("/call_control_applications").respond(200, json=_page(app))
    telnyx_api.get("/messaging_profiles").respond(
        200, json=_page({"id": MESSAGING_ID, "name": NAME, "webhook_url": sms_webhook})
    )


async def test_nothing_drifted_sends_only_lists(telnyx_api: respx.MockRouter) -> None:
    _existing(telnyx_api, _app())
    async with httpx.AsyncClient() as http:
        result = await bootstrap_account(rig(http).client, name=NAME, webhooks=HOOKS)
    assert [call.request.method for call in telnyx_api.calls] == ["GET", "GET", "GET"]
    assert (result.created, result.updated) == ((), ())
    assert result.env_values()["TELNYX_MESSAGING_PROFILE_ID"] == MESSAGING_ID


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (_app(webhook="https://stale.example.test/telnyx/voice"), {"webhook_event_url": HOOKS.voice}),
        (_app(profile_id="999"), {"outbound": {"outbound_voice_profile_id": PROFILE_ID}}),
    ],
    ids=["webhook", "profile"],
)
async def test_app_drift_patches_only_the_drifted_field(
    telnyx_api: respx.MockRouter, stored: dict[str, Any], expected: dict[str, Any]
) -> None:
    _existing(telnyx_api, stored)
    patch = telnyx_api.patch(f"/call_control_applications/{APP_ID}").respond(200, json={"data": _app()})
    async with httpx.AsyncClient() as http:
        result = await bootstrap_account(rig(http).client, name=NAME, webhooks=HOOKS)
    assert [body_of(call.request) for call in patch.calls] == [expected]
    assert result.updated == ("call_control_app",)


async def test_messaging_drift_patches_the_webhook(telnyx_api: respx.MockRouter) -> None:
    _existing(telnyx_api, _app(), sms_webhook="https://stale.example.test/sms")
    patch = telnyx_api.patch(f"/messaging_profiles/{MESSAGING_ID}").respond(
        200, json={"data": {"id": MESSAGING_ID, "name": NAME, "webhook_url": HOOKS.messaging}}
    )
    async with httpx.AsyncClient() as http:
        result = await bootstrap_account(rig(http).client, name=NAME, webhooks=HOOKS)
    assert [body_of(call.request) for call in patch.calls] == [{"webhook_url": HOOKS.messaging}]
    assert result.updated == ("messaging_profile",)


async def test_create_error_propagates(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.get("/outbound_voice_profiles").respond(200, json=_page())
    telnyx_api.post("/outbound_voice_profiles").respond(
        422, json={"errors": [{"code": "10015", "title": "Invalid value"}]}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await bootstrap_account(rig(http).client, name=NAME, webhooks=HOOKS)
    assert caught.value.status == 422
    assert [item.code for item in caught.value.errors] == ["10015"]
    assert not any(call.request.url.path.endswith("/call_control_applications") for call in telnyx_api.calls)
