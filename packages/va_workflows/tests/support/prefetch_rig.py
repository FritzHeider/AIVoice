"""Shared pieces of the ``prefetch_call_context`` tests: the opener configuration, the phrase cache over
FakeElevenLabs, and the activity run."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from call_rows import SeededCall
from fakeredis import FakeAsyncRedis
from resolved_configs import resolved_config
from rest_client_helpers import SimRest, sim_rest
from temporalio.testing import ActivityEnvironment
from tts_helpers import VOICE_ID

from va_domain.org import OrgSettings
from va_voice.phrase_cache import PhraseCache
from va_workflows.activities.call_context import CallContextActivities, PrefetchResult
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.prerender import OpenerPrerender

__all__ = ["Orgs", "opener_config", "opener_prerender", "phrase_cache", "run_prefetch"]


def opener_config(**overrides: Any) -> dict[str, Any]:
    """A resolved task configuration whose opener is spoken at once in FakeElevenLabs' seeded voice."""
    return {
        **resolved_config(),
        "first_sentence": "Hi {{first_name}}, this is Maya.",
        "request_data": {"first_name": "Ana"},
        "wait_for_greeting": False,
        "provider_voice_id": VOICE_ID,
        **overrides,
    }


@dataclass
class Orgs:
    """Every org has the default settings."""

    async def get(self, org_id: uuid.UUID) -> OrgSettings | None:
        del org_id
        return OrgSettings()


@asynccontextmanager
async def phrase_cache() -> AsyncIterator[tuple[SimRest, PhraseCache]]:
    """FakeElevenLabs REST (in process) and a phrase cache rendering through it."""
    async with sim_rest() as sim:
        cache = PhraseCache(sim.client, FakeAsyncRedis())
        try:
            yield sim, cache
        finally:
            await cache.aclose()


def opener_prerender(cache: PhraseCache) -> OpenerPrerender:
    return OpenerPrerender(cache=cache, org_settings=Orgs(), model_id="eleven_flash_v2_5")


async def run_prefetch(activities: CallContextActivities, call: SeededCall) -> PrefetchResult:
    request = CallActivityInput(call_id=call.call_id, org_id=call.org_id)
    return await ActivityEnvironment().run(activities.prefetch_call_context, request)
