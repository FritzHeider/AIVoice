"""FakeLLM test helpers: states, an in-process client, a served simulator, request bodies, SSE parsing.

Endpoint tests run in-process (``httpx.ASGITransport``) on an accelerated clock, so pacing costs no wall time
and can be measured in virtual milliseconds; fault tests that need real sockets (timeouts, disconnects) and
the ``openai`` SDK tests use :func:`served_llm` on a wall clock with zero latency.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
import orjson
from starlette.applications import Starlette

from va_sim.clock import AcceleratedClock, RealtimeClock
from va_sim.server import running_sims, simulator_routes
from va_sim.settings import DEFAULT_LLM_MODELS, Latencies
from va_sim.state import SimState

__all__ = [
    "API_KEY",
    "DONE",
    "MODEL",
    "add_rules",
    "chat_body",
    "deltas",
    "llm_client",
    "llm_state",
    "realtime_llm_state",
    "served_llm",
    "sse_events",
    "streamed_text",
    "wait_held",
]

API_KEY: Final = "sim-llm-key"
MODEL: Final = DEFAULT_LLM_MODELS[0]
DONE: Final = "[DONE]"
WAIT_TIMEOUT_S: Final = 5.0

type Event = dict[str, Any] | str


def llm_state(ttft_ms: int = 150, tokens_per_s: float = 80.0) -> SimState:
    """A state on the accelerated clock with the given FakeLLM latencies."""
    latencies = Latencies(stt_final_ms=60, tts_ttfa_ms=80, ttft_ms=ttft_ms, tokens_per_s=tokens_per_s)
    return SimState(clock=AcceleratedClock(), latencies=latencies)


def realtime_llm_state() -> SimState:
    """A state on the wall clock with no FakeLLM latency (real-socket tests)."""
    latencies = Latencies(stt_final_ms=10, tts_ttfa_ms=20, ttft_ms=0, tokens_per_s=100_000.0)
    return SimState(clock=RealtimeClock(), latencies=latencies)


@asynccontextmanager
async def llm_client(state: SimState, key: str | None = API_KEY) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process FakeLLM client (``Authorization: Bearer <key>`` unless None); closes the state after."""
    app = Starlette(routes=simulator_routes("llm", state))
    headers = {"authorization": f"Bearer {key}"} if key is not None else {}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://llm", headers=headers
        ) as client:
            yield client
    finally:
        await state.aclose()


@asynccontextmanager
async def served_llm(state: SimState) -> AsyncIterator[str]:
    """FakeLLM on an ephemeral port; yields its base URL."""
    async with running_sims(state, ports={"llm": 0}) as urls:
        yield urls["llm"]


def chat_body(*user_texts: str, system: str | None = None, **extra: Any) -> dict[str, Any]:
    """A chat request: an optional system message, then one user message per text."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system is not None else []
    messages.extend({"role": "user", "content": text} for text in user_texts)
    return {"model": MODEL, "messages": messages, **extra}


def sse_events(text: str) -> list[Event]:
    """Each ``data:`` event of an SSE body: the parsed JSON chunk, or ``"[DONE]"``."""
    events: list[Event] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        assert block.startswith("data: "), block
        data = block.removeprefix("data: ")
        events.append(data if data == DONE else orjson.loads(data))
    return events


def deltas(events: list[Event]) -> list[dict[str, Any]]:
    """The ``choices[0].delta`` of every chunk that has a choice."""
    return [event["choices"][0]["delta"] for event in events if isinstance(event, dict) and event["choices"]]


def streamed_text(events: list[Event]) -> str:
    return "".join(delta.get("content") or "" for delta in deltas(events))


async def add_rules(client: httpx.AsyncClient, yaml_text: str) -> list[str]:
    response = await client.post(
        "/sim/llm/rules", content=yaml_text, headers={"content-type": "application/yaml"}
    )
    assert response.status_code == 201, response.text
    ids: list[str] = response.json()["ids"]
    return ids


async def wait_held(state: SimState, count: int) -> None:
    """Wait (bounded, wall clock) until FakeLLM holds exactly ``count`` requests."""
    async with asyncio.timeout(WAIT_TIMEOUT_S):
        await state.llm.wait_held(count)
