"""LLM providers and the registry that picks one per org (specs/10-llm-gateway.md §1; Decision P03-T13a
rules 2-3).

The platform provider comes from the ``LLM_*`` settings; an org with a row in ``org_llm_providers`` uses its
own OpenAI-compatible provider instead (:class:`va_llm.org_providers.OrgProviderStore`).
"""

from __future__ import annotations

import uuid
from typing import Final, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from va_llm.settings import LlmSettings

__all__ = [
    "MODEL_ROLES",
    "PLATFORM_PROVIDER_ID",
    "LlmProvider",
    "ModelRole",
    "OrgProviderSource",
    "ProviderRegistry",
    "org_provider_id",
    "platform_provider",
]

type ModelRole = Literal["dialogue", "router", "analysis", "fallback"]

MODEL_ROLES: Final[tuple[ModelRole, ...]] = get_args(ModelRole.__value__)
PLATFORM_PROVIDER_ID: Final = "platform"


class LlmProvider(BaseModel):
    """An OpenAI-compatible endpoint, its key, and the model used for each role (specs/10 §1)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr
    models: dict[ModelRole, str]
    max_concurrency: int = Field(default=500, ge=1)
    supports_tool_choice: bool = True
    supports_parallel_tool_calls: bool = False

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def _key_present(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            msg = "api_key must not be empty"
            raise ValueError(msg)
        return value

    @field_validator("models")
    @classmethod
    def _every_role(cls, value: dict[ModelRole, str]) -> dict[ModelRole, str]:
        missing = [role for role in MODEL_ROLES if not value.get(role, "").strip()]
        if missing:
            msg = f"models must name a model for every role; missing: {', '.join(missing)}"
            raise ValueError(msg)
        return value


def org_provider_id(org_id: uuid.UUID) -> str:
    """The provider id of an org's own provider: ``org:<uuid>``."""
    return f"org:{org_id}"


def platform_provider(settings: LlmSettings) -> LlmProvider:
    """The platform provider (Telnyx Inference in production, FakeLLM in development)."""
    return LlmProvider(
        id=PLATFORM_PROVIDER_ID,
        base_url=settings.llm_default_base_url,
        api_key=settings.llm_default_api_key,
        models={
            "dialogue": settings.llm_dialogue_model,
            "router": settings.llm_router_model,
            "analysis": settings.llm_analysis_model,
            "fallback": settings.llm_fallback_model,
        },
    )


class OrgProviderSource(Protocol):
    """Looks up an org's own provider (``None`` when the org uses the platform provider)."""

    async def get(self, org_id: uuid.UUID) -> LlmProvider | None: ...


class ProviderRegistry:
    """Resolves the provider of an org: its own when configured, else the platform provider."""

    def __init__(self, platform: LlmProvider, org_providers: OrgProviderSource | None = None) -> None:
        self.platform = platform
        self._org_providers = org_providers

    async def resolve(self, org_id: uuid.UUID) -> LlmProvider:
        if self._org_providers is None:
            return self.platform
        provider = await self._org_providers.get(org_id)
        return provider if provider is not None else self.platform
