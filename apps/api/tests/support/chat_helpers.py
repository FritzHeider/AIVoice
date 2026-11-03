"""Pathway test chat helpers (specs/12 §7, Decision P07-T10).

:func:`chat_stack` serves FakeLLM on a real socket (wall clock, no latency) and the api app whose container
runs on the wall clock with ``LLM_DEFAULT_BASE_URL`` pointing at it, so the chat's decisions and replies go
through the real gateway. :func:`restaurant` is the chat graph the API tests use: FakeLLM routes by edge
label words, so each caller line below lands on one node.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
from graph_helpers import edge, node, start
from llm_client_helpers import served_requests
from llm_helpers import add_rules, realtime_llm_state
from session_helpers import SessionContainer, session_app, session_client

from va_common.time import FrozenClock, SystemClock
from va_sim.server import running_sims
from va_sim.state import SimState

__all__ = [
    "CHAT",
    "GREETING",
    "HOOK_HOST",
    "ChatStack",
    "Chats",
    "WallClock",
    "chat_stack",
    "restaurant",
]

CHAT: Final = "/v1/pathway/chat"
GREETING: Final = "Hi! Thanks for calling Bella Pasta."
HOOK_HOST: Final = "hooks.test"
SECRET_TOKEN: Final = "Bearer chat-test-secret"  # noqa: S105 - a test header value the logs must mask


class WallClock(FrozenClock):
    """The wall clock behind the test container's ``FrozenClock`` type: the chat's gateway deadlines and turn
    cap must not fire at once (``advance`` still works for code that moves time explicitly)."""

    def __init__(self) -> None:
        super().__init__()
        self._wall = SystemClock()
        self._offset = 0.0

    def now(self) -> datetime:
        return self._wall.now() + timedelta(seconds=self._offset)

    def monotonic(self) -> float:
        return self._wall.monotonic() + self._offset

    def advance(self, delta: float | timedelta) -> None:
        self._offset += delta.total_seconds() if isinstance(delta, timedelta) else float(delta)

    async def sleep(self, seconds: float) -> None:
        await self._wall.sleep(seconds)


@dataclass(slots=True)
class ChatStack:
    client: httpx.AsyncClient
    llm: SimState
    control: httpx.AsyncClient

    async def rules(self, yaml_text: str) -> None:
        await add_rules(self.control, yaml_text)

    async def requests(self) -> list[dict[str, Any]]:
        """FakeLLM's request log."""
        return await served_requests(self.control)


@asynccontextmanager
async def chat_stack(container: SessionContainer) -> AsyncIterator[ChatStack]:
    """FakeLLM plus the api app over ``container`` (which must run on a :class:`WallClock`)."""
    state = realtime_llm_state()
    try:
        async with running_sims(state, ports={"llm": 0}) as urls:
            app = session_app(container, llm_default_base_url=f"{urls['llm']}/v1")
            async with (
                session_client(app) as client,
                httpx.AsyncClient(base_url=urls["llm"]) as control,
            ):
                yield ChatStack(client, state, control)
    finally:
        await state.aclose()


class Chats:
    """An org's ``*`` API key against the chat routes."""

    def __init__(self, client: httpx.AsyncClient, key: str) -> None:
        self.client = client
        self.headers = {"Authorization": key}

    async def create(self, pathway_id: str, **body: Any) -> httpx.Response:
        return await self.client.post(
            f"{CHAT}/create", json={"pathway_id": pathway_id, **body}, headers=self.headers
        )

    async def created(self, pathway_id: str, **body: Any) -> dict[str, Any]:
        response = await self.create(pathway_id, **body)
        assert response.status_code == 200, response.text
        created: dict[str, Any] = response.json()
        return created

    async def send(self, chat_id: str, message: str, **body: Any) -> httpx.Response:
        return await self.client.post(
            f"{CHAT}/{chat_id}", json={"message": message, **body}, headers=self.headers
        )

    async def said(self, chat_id: str, message: str, **body: Any) -> dict[str, Any]:
        response = await self.send(chat_id, message, **body)
        assert response.status_code == 200, response.text
        reply: dict[str, Any] = response.json()
        return reply

    async def delete(self, chat_id: str) -> httpx.Response:
        return await self.client.delete(f"{CHAT}/{chat_id}", headers=self.headers)


def restaurant(hook_port: int) -> dict[str, Any]:
    """A pathway graph: the greeting routes to ordering, the manager (cold transfer), the delivery status
    webhook (``http://hooks.test:<port>/status`` with a secret header), a goodbye (end call), and a text
    message (SMS node)."""
    hook = {
        "url": f"http://{HOOK_HOST}:{hook_port}/status",
        "method": "POST",
        "headers": {"Authorization": SECRET_TOKEN, "X-Order": '{{order_id | default:"none"}}'},
        "body": {"caller": "{{from}}"},
        "speech": "Let me check that for you.",
        "responseData": [{"name": "eta", "data": "$.eta"}],
    }
    nodes = [
        start("greet", name="Greeting", text=GREETING),
        node(
            "order",
            name="Take Order",
            text='What would you like to order, {{first_name | default:"friend"}}?',
        ),
        node(
            "manager",
            name="Manager",
            node_type="Transfer Call",
            transfer={"mode": "cold", "number": "+14155550100", "speech": "Connecting you now."},
        ),
        node(
            "hook",
            name="Delivery Status",
            node_type="Webhook",
            webhook=hook,
            text="It arrives in {{eta}} minutes.",
        ),
        node("bye", name="Goodbye", node_type="End Call", endCall={"speech": "Goodbye!"}),
        node(
            "sms",
            name="Text",
            node_type="SMS",
            sms={"message": "Your Bella Pasta order is confirmed."},
            text="I just texted you.",
        ),
    ]
    edges = [
        edge("e_order", "greet", "order", "Caller wants to order food"),
        edge("e_manager", "greet", "manager", "Caller asks for the manager"),
        edge("e_hook", "greet", "hook", "Caller asks about delivery status"),
        edge("e_bye", "greet", "bye", "Caller says goodbye"),
        edge("e_sms", "greet", "sms", "Caller wants a text message"),
        edge("e_order_bye", "order", "bye", "Caller says goodbye"),
    ]
    return {"nodes": nodes, "edges": edges, "global_config": {}}
