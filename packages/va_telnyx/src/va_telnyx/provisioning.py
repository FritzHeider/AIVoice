"""Telnyx provisioning commands (specs/06-telephony-telnyx.md §2.1 provisioning rows, §12).

Create bodies carry exactly the fields of the §2.1 rows (``null`` values included); updates send only the
given fields. Lists return every page (Decision 2026-11-01 P03-T08b rule 2); name filters are applied by
Telnyx, so callers that need an exact name compare it themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from va_telnyx.provisioning_models import Balance, CallControlApp, MessagingProfile, OutboundVoiceProfile
from va_telnyx.transport import Operation, TelnyxTransport, parse_data, segment

__all__ = ["DEFAULT_DESTINATIONS", "ProvisioningCommands"]

DEFAULT_DESTINATIONS: Final = ("US", "CA")

_CREATE_APP: Final = Operation("create_call_control_app", "POST", None, idempotent=False)
_LIST_APPS: Final = Operation("list_call_control_apps", "GET", None, idempotent=True)
_UPDATE_APP: Final = Operation("update_call_control_app", "PATCH", None, idempotent=True)
_CREATE_VOICE_PROFILE: Final = Operation("create_outbound_voice_profile", "POST", None, idempotent=False)
_LIST_VOICE_PROFILES: Final = Operation("list_outbound_voice_profiles", "GET", None, idempotent=True)
_CREATE_MESSAGING_PROFILE: Final = Operation("create_messaging_profile", "POST", None, idempotent=False)
_LIST_MESSAGING_PROFILES: Final = Operation("list_messaging_profiles", "GET", None, idempotent=True)
_UPDATE_MESSAGING_PROFILE: Final = Operation("update_messaging_profile", "PATCH", None, idempotent=True)
_GET_BALANCE: Final = Operation("get_balance", "GET", None, idempotent=True)


class ProvisioningCommands:
    """Call Control Applications, outbound voice profiles, messaging profiles, and the account balance."""

    _transport: TelnyxTransport

    async def create_call_control_app(
        self, name: str, webhook_url: str, *, outbound_voice_profile_id: str
    ) -> CallControlApp:
        body = {
            "application_name": name,
            "webhook_event_url": webhook_url,
            "webhook_api_version": "2",
            "webhook_event_failover_url": "",
            "webhook_timeout_secs": 25,
            "first_command_timeout": True,
            "first_command_timeout_secs": 30,
            "anchorsite_override": "Latency",
            "dtmf_type": "RFC 2833",
            "inbound": {"channel_limit": None, "sip_subdomain_receive_settings": "only_my_connections"},
            "outbound": {"outbound_voice_profile_id": outbound_voice_profile_id},
        }
        envelope = await self._transport.request(_CREATE_APP, "/call_control_applications", json=body)
        return parse_data(_CREATE_APP, envelope, CallControlApp)

    async def list_call_control_apps(self, name_contains: str) -> list[CallControlApp]:
        params = {"filter[application_name][contains]": name_contains}
        return await self._transport.request_pages(
            _LIST_APPS, "/call_control_applications", CallControlApp, params=params
        )

    async def update_call_control_app(
        self, app_id: str, *, webhook_url: str | None = None, outbound_voice_profile_id: str | None = None
    ) -> CallControlApp:
        """PATCH the webhook URL and/or the outbound voice profile (``ValueError`` when neither is given)."""
        body: dict[str, Any] = {}
        if webhook_url is not None:
            body["webhook_event_url"] = webhook_url
        if outbound_voice_profile_id is not None:
            body["outbound"] = {"outbound_voice_profile_id": outbound_voice_profile_id}
        if not body:
            msg = "update_call_control_app needs webhook_url or outbound_voice_profile_id"
            raise ValueError(msg)
        envelope = await self._transport.request(
            _UPDATE_APP, f"/call_control_applications/{segment(app_id)}", json=body
        )
        return parse_data(_UPDATE_APP, envelope, CallControlApp)

    async def create_outbound_voice_profile(
        self, name: str, *, whitelisted_destinations: Sequence[str] = DEFAULT_DESTINATIONS
    ) -> OutboundVoiceProfile:
        """A conversational profile; BYO accounts extend the destinations with the org's international
        policy."""
        body = {
            "name": name,
            "traffic_type": "conversational",
            "service_plan": "global",
            "concurrent_call_limit": None,
            "enabled": True,
            "whitelisted_destinations": list(whitelisted_destinations),
        }
        envelope = await self._transport.request(_CREATE_VOICE_PROFILE, "/outbound_voice_profiles", json=body)
        return parse_data(_CREATE_VOICE_PROFILE, envelope, OutboundVoiceProfile)

    async def list_outbound_voice_profiles(self, name: str) -> list[OutboundVoiceProfile]:
        return await self._transport.request_pages(
            _LIST_VOICE_PROFILES,
            "/outbound_voice_profiles",
            OutboundVoiceProfile,
            params={"filter[name][contains]": name},
        )

    async def create_messaging_profile(
        self, name: str, webhook_url: str, *, whitelisted_destinations: Sequence[str] = DEFAULT_DESTINATIONS
    ) -> MessagingProfile:
        body = {
            "name": name,
            "webhook_url": webhook_url,
            "webhook_api_version": "2",
            "whitelisted_destinations": list(whitelisted_destinations),
            "enabled": True,
        }
        envelope = await self._transport.request(_CREATE_MESSAGING_PROFILE, "/messaging_profiles", json=body)
        return parse_data(_CREATE_MESSAGING_PROFILE, envelope, MessagingProfile)

    async def list_messaging_profiles(self, name: str) -> list[MessagingProfile]:
        return await self._transport.request_pages(
            _LIST_MESSAGING_PROFILES, "/messaging_profiles", MessagingProfile, params={"filter[name]": name}
        )

    async def update_messaging_profile(self, profile_id: str, *, webhook_url: str) -> MessagingProfile:
        envelope = await self._transport.request(
            _UPDATE_MESSAGING_PROFILE,
            f"/messaging_profiles/{segment(profile_id)}",
            json={"webhook_url": webhook_url},
        )
        return parse_data(_UPDATE_MESSAGING_PROFILE, envelope, MessagingProfile)

    async def get_balance(self) -> Balance:
        """``GET /balance`` (also validates a BYO key, specs/06 §12)."""
        envelope = await self._transport.request(_GET_BALANCE, "/balance")
        return parse_data(_GET_BALANCE, envelope, Balance)
