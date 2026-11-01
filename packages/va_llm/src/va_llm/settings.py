"""LLM gateway settings (specs/02-repo-layout-and-conventions.md §6 rows ``LLM_*``; specs/10-llm-gateway.md
§1).

Mixed into the settings of every service that builds an :class:`va_llm.LlmGateway` (media-worker, worker,
api).
"""

from __future__ import annotations

from pydantic import SecretStr

from va_common.settings import BaseServiceSettings

__all__ = ["LlmSettings"]


class LlmSettings(BaseServiceSettings):
    """The platform provider (base URL, key, one model per role), the per-model COGS overrides, and the
    embeddings model and dimensions (``EMBEDDINGS_MODEL``/``EMBEDDINGS_DIMENSIONS``)."""

    llm_default_base_url: str = "http://localhost:9300/v1"
    llm_default_api_key: SecretStr = SecretStr("sim-llm-key")
    llm_dialogue_model: str = "zai-org/GLM-5.3-Flash"
    llm_router_model: str = "zai-org/GLM-5.3-Flash"
    llm_analysis_model: str = "zai-org/GLM-5.3"
    llm_fallback_model: str = "moonshotai/Kimi-K2.6"
    llm_model_costs_json: str = "{}"
    embeddings_model: str = "Qwen/Qwen3-Embedding-8B"
    embeddings_dimensions: int = 1024
