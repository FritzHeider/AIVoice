"""Live smoke fixtures (specs/30 §7 Decision P03-T16): real vendor clients on the production endpoints of
specs/02 §6, each skipped when its ``LIVE_*_API_KEY`` is empty."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Final

import fakeredis
import httpx
import pytest
from live_clients import (
    ELEVENLABS_API_BASE,
    LLM_BASE_URL,
    TELNYX_API_BASE,
    embeddings_dimensions,
    llm_default,
)
from live_env import ELEVENLABS_KEY, LLM_KEY, TELNYX_KEY, LiveEnv, require
from pydantic import SecretStr

from va_common.metrics import new_registry
from va_common.time import SystemClock
from va_elevenlabs import ElevenLabsRest
from va_llm import Embeddings, LlmGateway, LlmMetrics, LlmProvider, ProviderRegistry
from va_llm.providers import ModelRole
from va_telnyx import TelnyxClient, TelnyxRateLimiter

HTTP_TIMEOUT_S: Final = 60.0


@pytest.fixture
def live_env() -> LiveEnv:
    return LiveEnv.from_environ(os.environ)


@pytest.fixture
def elevenlabs_key(live_env: LiveEnv) -> str:
    return require(live_env.elevenlabs_api_key, ELEVENLABS_KEY)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        yield client


@pytest.fixture
async def telnyx(live_env: LiveEnv, http: httpx.AsyncClient) -> AsyncIterator[TelnyxClient]:
    key = require(live_env.telnyx_api_key, TELNYX_KEY)
    redis = fakeredis.FakeAsyncRedis()
    yield TelnyxClient(key, TELNYX_API_BASE, http, TelnyxRateLimiter(redis))
    await redis.aclose()


@pytest.fixture
def eleven_rest(elevenlabs_key: str, http: httpx.AsyncClient) -> ElevenLabsRest:
    return ElevenLabsRest(elevenlabs_key, ELEVENLABS_API_BASE, http, clock=SystemClock())


@pytest.fixture
def llm_provider(live_env: LiveEnv) -> LlmProvider:
    key = require(live_env.llm_api_key, LLM_KEY)
    models: dict[ModelRole, str] = {
        "dialogue": llm_default("llm_dialogue_model"),
        "router": llm_default("llm_router_model"),
        "analysis": llm_default("llm_analysis_model"),
        "fallback": llm_default("llm_fallback_model"),
    }
    return LlmProvider(id="platform", base_url=LLM_BASE_URL, api_key=SecretStr(key), models=models)


@pytest.fixture
def gateway(llm_provider: LlmProvider, http: httpx.AsyncClient) -> LlmGateway:
    metrics = LlmMetrics(new_registry(process_collectors=False))
    return LlmGateway(ProviderRegistry(llm_provider), http, clock=SystemClock(), metrics=metrics)


@pytest.fixture
async def embeddings(llm_provider: LlmProvider, http: httpx.AsyncClient) -> AsyncIterator[Embeddings]:
    redis = fakeredis.FakeAsyncRedis()
    yield Embeddings(
        llm_provider,
        http,
        redis=redis,
        clock=SystemClock(),
        metrics=LlmMetrics(new_registry(process_collectors=False)),
        model=llm_default("embeddings_model"),
        dimensions=embeddings_dimensions(),
    )
    await redis.aclose()
