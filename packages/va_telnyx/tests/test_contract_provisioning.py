"""Contract tests: provisioning methods, balance, and BYO clients against FakeTelnyx
(specs/06-telephony-telnyx.md §1, §2.1, §12; specs/30-testing-and-evals.md §3.2).

FakeTelnyx forbids unknown body fields and validates values, so a body the client builds wrongly fails here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import fakeredis
from clock_helpers import RecordingClock
from telnyx_client_helpers import SIM_BASE_URL, sim_clients, sim_http
from telnyx_helpers import telnyx_state

from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx import TelnyxClientFactory, TelnyxRateLimiter

VOICE = "https://edge.example.test/telnyx/voice"
VOICE_2 = "https://edge2.example.test/telnyx/voice"
SMS = "https://edge.example.test/telnyx/messaging"
BYO_ACCOUNT = uuid.UUID("0192f3a0-2222-7000-8000-000000000001")
BYO_KEY = "KEY-byo-contract"


async def test_bootstrap_resources_round_trip() -> None:
    state = telnyx_state()
    async with sim_clients(state) as (raw, client):
        profile = await client.create_outbound_voice_profile("voiceagents-test")
        app = await client.create_call_control_app(
            "voiceagents-test", VOICE, outbound_voice_profile_id=profile.id
        )
        await client.create_call_control_app("unrelated", VOICE, outbound_voice_profile_id=profile.id)
        listed_apps = await client.list_call_control_apps("voiceagents-")
        listed_profiles = await client.list_outbound_voice_profiles("voiceagents-test")
        second = await client.create_outbound_voice_profile("voiceagents-test-2")
        updated = await client.update_call_control_app(
            app.id, webhook_url=VOICE_2, outbound_voice_profile_id=second.id
        )
        stored = (await raw.get(f"/v2/call_control_applications/{app.id}")).json()["data"]
        messaging = await client.create_messaging_profile("voiceagents-test", SMS)
        exact = await client.list_messaging_profiles("voiceagents-test")
        prefix_only = await client.list_messaging_profiles("voiceagents-")
        patched = await client.update_messaging_profile(messaging.id, webhook_url=VOICE_2)
        balance = await client.get_balance()
    assert profile.whitelisted_destinations == ("US", "CA")
    assert app.outbound_voice_profile_id == profile.id
    assert [a.id for a in listed_apps] == [app.id]
    assert [p.id for p in listed_profiles] == [profile.id]
    assert (updated.webhook_event_url, updated.outbound_voice_profile_id) == (VOICE_2, second.id)
    assert stored["webhook_event_url"] == VOICE_2
    assert stored["inbound"]["channel_limit"] is None
    assert stored["first_command_timeout_secs"] == 30
    assert [m.id for m in exact] == [messaging.id]
    assert prefix_only == []
    assert patched.webhook_url == VOICE_2
    assert balance.balance == Decimal("100.00")
    assert balance.currency == "USD"


async def test_lists_page_through_more_than_one_page() -> None:
    state = telnyx_state()
    async with sim_clients(state) as (_raw, client):
        created = [await client.create_outbound_voice_profile(f"voiceagents-bulk-{n:03}") for n in range(260)]
        listed = await client.list_outbound_voice_profiles("voiceagents-bulk-")
    assert [p.id for p in listed] == [p.id for p in created]


async def test_byo_clients_see_only_their_account() -> None:
    state = telnyx_state()
    async with sim_http(state) as (raw, http):
        registered = await raw.post("/sim/telnyx/accounts", json={"api_key": BYO_KEY})
        assert registered.status_code == 201
        clock = RecordingClock()

        async def resolve(account_id: uuid.UUID) -> str | None:
            return BYO_KEY if account_id == BYO_ACCOUNT else None

        factory = TelnyxClientFactory(
            base_url=SIM_BASE_URL,
            http=http,
            limiter=TelnyxRateLimiter(fakeredis.FakeAsyncRedis(), clock=clock),
            platform_api_key=DEFAULT_API_KEY,
            resolve_key=resolve,
            clock=clock,
        )
        platform_profile = await factory.platform.create_outbound_voice_profile("voiceagents-test")
        byo = await factory.for_call(_Call({"telnyx_account_id": str(BYO_ACCOUNT)}))
        assert await byo.list_outbound_voice_profiles("voiceagents-") == []
        byo_profile = await byo.create_outbound_voice_profile(f"voiceagents-org-{BYO_ACCOUNT}")
        platform_view = await factory.platform.list_outbound_voice_profiles("voiceagents-")
        balance = await byo.get_balance()
    assert [p.id for p in platform_view] == [platform_profile.id]
    assert byo_profile.id != platform_profile.id
    assert balance.available_credit == Decimal("100.00")


class _Call:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
