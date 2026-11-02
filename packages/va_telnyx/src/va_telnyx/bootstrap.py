"""Idempotent Telnyx account bootstrap (specs/06-telephony-telnyx.md §12; Decision 2026-11-01 P03-T15a).

:func:`bootstrap_account` finds or creates the outbound voice profile, the Call Control Application, and the
messaging profile of one Telnyx account, and patches only the fields that drifted. It talks only to a
:class:`~va_telnyx.client.TelnyxClient`: the platform script (``scripts/telnyx_bootstrap.py``) and the BYO
activity decide names, webhook URLs, and where the ids are stored.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from va_common.logging import get_logger
from va_common.stream_tokens import INSECURE_MEDIA_ENVS
from va_telnyx.client import TelnyxClient
from va_telnyx.provisioning_models import CallControlApp, MessagingProfile, OutboundVoiceProfile

__all__ = [
    "ENV_KEYS",
    "RESOURCE_PREFIX",
    "BootstrapResult",
    "ResourceKind",
    "WebhookScheme",
    "WebhookUrls",
    "bootstrap_account",
    "org_resource_name",
    "platform_resource_name",
    "webhook_scheme",
    "webhook_urls",
]

RESOURCE_PREFIX: Final = "voiceagents"
ENV_KEYS: Final = (
    "TELNYX_CALL_CONTROL_APP_ID",
    "TELNYX_OUTBOUND_VOICE_PROFILE_ID",
    "TELNYX_MESSAGING_PROFILE_ID",
)

ResourceKind = Literal["outbound_voice_profile", "call_control_app", "messaging_profile"]
WebhookScheme = Literal["http", "https"]

_log = get_logger(__name__)


def platform_resource_name(va_env: str) -> str:
    """``voiceagents-{VA_ENV}`` (§12 steps 1-3)."""
    return f"{RESOURCE_PREFIX}-{va_env}"


def org_resource_name(org_id: uuid.UUID | str) -> str:
    """``voiceagents-org-{org_id}`` for BYO accounts (§12 step 5)."""
    return f"{RESOURCE_PREFIX}-org-{org_id}"


@dataclass(frozen=True, slots=True)
class WebhookUrls:
    voice: str
    messaging: str


def webhook_scheme(va_env: str) -> WebhookScheme:
    """``http`` in ``local``/``test`` (plain-HTTP edge, like the ``ws`` media URL), ``https`` elsewhere."""
    return "http" if va_env in INSECURE_MEDIA_ENVS else "https"


def webhook_urls(
    edge_public_host: str,
    telnyx_account_id: uuid.UUID | str | None = None,
    *,
    scheme: WebhookScheme = "https",
) -> WebhookUrls:
    """The edge's voice and messaging webhook URLs; BYO accounts get their account id as the last segment."""
    suffix = "" if telnyx_account_id is None else f"/{telnyx_account_id}"
    base = f"{scheme}://{edge_public_host}/telnyx"
    return WebhookUrls(voice=f"{base}/voice{suffix}", messaging=f"{base}/messaging{suffix}")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    call_control_app_id: str
    outbound_voice_profile_id: str
    messaging_profile_id: str
    created: tuple[ResourceKind, ...] = ()
    updated: tuple[ResourceKind, ...] = ()

    def env_values(self) -> dict[str, str]:
        """The three ``TELNYX_*_ID`` variables in §12 step 4 order."""
        values = (self.call_control_app_id, self.outbound_voice_profile_id, self.messaging_profile_id)
        return dict(zip(ENV_KEYS, values, strict=True))


def _exact[T](kind: ResourceKind, name: str, items: Sequence[T], name_of: Callable[[T], str]) -> T | None:
    """The first item named exactly ``name``; several exact matches are logged, never deleted."""
    matches = [item for item in items if name_of(item) == name]
    if not matches:
        return None
    if len(matches) > 1:
        _log.warning(
            "telnyx_bootstrap.duplicate_resources",
            kind=kind,
            name=name,
            count=len(matches),
            chosen_id=getattr(matches[0], "id", None),
        )
    return matches[0]


class _Run:
    """One bootstrap pass; collects which resources were created or updated."""

    def __init__(self, client: TelnyxClient, name: str, webhooks: WebhookUrls) -> None:
        self.client = client
        self.name = name
        self.webhooks = webhooks
        self.created: list[ResourceKind] = []
        self.updated: list[ResourceKind] = []

    async def voice_profile(self) -> OutboundVoiceProfile:
        listed = await self.client.list_outbound_voice_profiles(self.name)
        found = _exact("outbound_voice_profile", self.name, listed, lambda p: p.name)
        if found is not None:
            return found
        self.created.append("outbound_voice_profile")
        return await self.client.create_outbound_voice_profile(self.name)

    async def app(self, profile_id: str) -> CallControlApp:
        listed = await self.client.list_call_control_apps(self.name)
        found = _exact("call_control_app", self.name, listed, lambda a: a.application_name)
        if found is None:
            self.created.append("call_control_app")
            return await self.client.create_call_control_app(
                self.name, self.webhooks.voice, outbound_voice_profile_id=profile_id
            )
        webhook = None if found.webhook_event_url == self.webhooks.voice else self.webhooks.voice
        profile = None if found.outbound_voice_profile_id == profile_id else profile_id
        if webhook is None and profile is None:
            return found
        self.updated.append("call_control_app")
        return await self.client.update_call_control_app(
            found.id, webhook_url=webhook, outbound_voice_profile_id=profile
        )

    async def messaging(self) -> MessagingProfile:
        listed = await self.client.list_messaging_profiles(self.name)
        found = _exact("messaging_profile", self.name, listed, lambda m: m.name)
        if found is None:
            self.created.append("messaging_profile")
            return await self.client.create_messaging_profile(self.name, self.webhooks.messaging)
        if found.webhook_url == self.webhooks.messaging:
            return found
        self.updated.append("messaging_profile")
        return await self.client.update_messaging_profile(found.id, webhook_url=self.webhooks.messaging)


async def bootstrap_account(client: TelnyxClient, *, name: str, webhooks: WebhookUrls) -> BootstrapResult:
    """Find or create the account's voice profile, Call Control Application, and messaging profile (in that
    order), patching drifted webhooks and the app's profile; a :class:`~va_telnyx.errors.TelnyxError`
    propagates."""
    run = _Run(client, name, webhooks)
    profile = await run.voice_profile()
    app = await run.app(profile.id)
    messaging = await run.messaging()
    result = BootstrapResult(
        call_control_app_id=app.id,
        outbound_voice_profile_id=profile.id,
        messaging_profile_id=messaging.id,
        created=tuple(run.created),
        updated=tuple(run.updated),
    )
    _log.info(
        "telnyx_bootstrap.done",
        name=name,
        created=list(result.created),
        updated=list(result.updated),
        **result.env_values(),
    )
    return result
