"""The process's LLM gateway: the platform provider plus per-org overrides (specs/10-llm-gateway.md §1).

Every service that calls models (api test chat, media worker, worker test runs) builds its gateway here, so
the provider registry, cost table, and metrics wiring stay one definition.
"""

from __future__ import annotations

import httpx

from va_common.crypto import Envelope
from va_common.time import Clock
from va_llm.gateway import LlmGateway
from va_llm.metrics import LlmMetrics
from va_llm.org_providers import OrgProviderStore, TenantSessions
from va_llm.pricing import CostTable, load_costs
from va_llm.providers import ProviderRegistry, platform_provider
from va_llm.settings import LlmSettings

__all__ = ["platform_gateway"]


def platform_gateway(  # noqa: PLR0913 - the shared collaborators, keyword-only
    settings: LlmSettings,
    *,
    database: TenantSessions,
    envelope: Envelope,
    http: httpx.AsyncClient,
    clock: Clock,
    metrics: LlmMetrics,
) -> LlmGateway:
    """A gateway over ``http`` (owned by the caller): ``LLM_*`` platform provider, org providers decrypted
    through ``envelope``, ``LLM_MODEL_COSTS_JSON`` costs."""
    return LlmGateway(
        ProviderRegistry(platform_provider(settings), OrgProviderStore(database, envelope, clock=clock)),
        http,
        clock=clock,
        metrics=metrics,
        costs=CostTable(load_costs(settings.llm_model_costs_json)),
    )
