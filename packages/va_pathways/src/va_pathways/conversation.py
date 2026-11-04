"""One text run of a pathway engine (specs/12 §7 Decision P07-T10; specs/12 §8 Decision P07-T11a).

:class:`TextConversation` builds a :class:`~va_engine.engine.PathwayEngine` over a stored or fresh
:class:`~va_engine.models.EngineState` on channel ``voice`` (a text run simulates a call), with the real
gateway behind decisions, replies and composed final lines, a :class:`~va_pathways.recording.TextRecorder` on
the event, HTTP and SMS ports, and the given :class:`~va_engine.navigation.PathwayLoader` for forwarding and
Transfer Pathway nodes. :meth:`TextConversation.run` consumes one engine generator (``start`` or
``handle_user_turn``) within a time cap, settles the engine's background work and closes it; a terminal
action marks the state ended. The test chat (api) and the unit-test runner (api and worker) share it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from typing import Final

import httpx
from pydantic import JsonValue

from va_common.time import Clock
from va_common.waits import within
from va_domain.org import OrgSettings
from va_engine.builtins import builtin_values
from va_engine.dialogue import GatewayDialogue
from va_engine.engine import ConversationFacts, EnginePorts, PathwayEngine
from va_engine.engine_decide import Decider
from va_engine.final_lines import GatewayLinePort
from va_engine.models import EngineState
from va_engine.models.results import Action
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_llm import LlmGateway
from va_llm.tokens import TokenCounter
from va_pathways.recording import MockResponse, TextRecorder
from va_tools.http import HttpExecutor

__all__ = [
    "DEFAULT_SIMULATE_FROM",
    "DEFAULT_SIMULATE_TO",
    "TEXT_LANGUAGE",
    "TEXT_TIMEZONE",
    "TextConversation",
    "TextDeps",
    "TextSetup",
]

DEFAULT_SIMULATE_FROM: Final = "+14155550123"
DEFAULT_SIMULATE_TO: Final = "+14155550100"
TEXT_LANGUAGE: Final = "en-US"
TEXT_TIMEZONE: Final = "UTC"


@dataclass(frozen=True, slots=True, kw_only=True)
class TextDeps:
    """The process pieces a text run uses: the LLM gateway, the shared tool HTTP client, the clock (turn
    caps, decision timing), and whether webhook URLs may be ``http://``."""

    gateway: LlmGateway
    tools_http: httpx.AsyncClient
    clock: Clock
    allow_http: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class TextSetup:
    """What one run is about: ``conversation_id`` stands in for the call id (``X-VA-Call-Id``, LLM call id),
    ``simulate_from`` is the simulated caller."""

    conversation_id: uuid.UUID
    org_id: uuid.UUID
    active: ActiveGraph
    org: OrgSettings
    loader: PathwayLoader
    request_data: Mapping[str, JsonValue] = field(default_factory=dict)
    simulate_from: str = DEFAULT_SIMULATE_FROM
    simulate_to: str = DEFAULT_SIMULATE_TO
    mocks: Mapping[str, MockResponse] = field(default_factory=dict)


class TextConversation:
    """One run's engine over ``state`` with the recorder on every port."""

    def __init__(
        self, deps: TextDeps, setup: TextSetup, state: EngineState, *, counter: TokenCounter
    ) -> None:
        conversation_id = setup.conversation_id
        self.recorder = TextRecorder(
            HttpExecutor(
                deps.tools_http, clock=deps.clock, allow_http=deps.allow_http, call_id=str(conversation_id)
            ),
            mocks=setup.mocks,
        )
        self._clock = deps.clock
        settings = DialogueSettings(
            org_id=setup.org_id,
            direction="inbound",
            call_id=conversation_id,
            counterpart=setup.simulate_from,
            context_budget_tokens=setup.org.llm.context_budget_tokens,
            speculative=setup.org.engine.speculative,
        )
        prompt = PathwayPrompt(settings, counter=counter, clock=deps.clock)
        self.engine = PathwayEngine(
            state,
            setup.active,
            ports=EnginePorts(
                decider=Decider(deps.gateway, clock=deps.clock),
                dialogue=GatewayDialogue(deps.gateway, prompt),
                lines=GatewayLinePort(deps.gateway, org_id=setup.org_id, call_id=conversation_id),
                events=self.recorder,
                loader=setup.loader,
                http=self.recorder,
                sms=self.recorder,
            ),
            facts=ConversationFacts(
                org_id=setup.org_id,
                call_id=conversation_id,
                channel="voice",
                request_data=setup.request_data,
                builtins=builtin_values(
                    now=deps.clock.now(),
                    timezone=TEXT_TIMEZONE,
                    from_number=setup.simulate_from,
                    to_number=setup.simulate_to,
                    call_id=str(conversation_id),
                    voice_id=None,
                    language=TEXT_LANGUAGE,
                    inbound=True,
                ),
                timezone=TEXT_TIMEZONE,
            ),
            clock=deps.clock,
        )

    @property
    def state(self) -> EngineState:
        return self.engine.state

    async def run(self, actions: AsyncGenerator[Action], *, timeout_s: float) -> bool:
        """Consume ``actions`` within ``timeout_s`` of the clock, then settle and close the engine; ``False``
        when the cap cut the run short."""
        try:
            finished = await within(self.recorder.consume(actions), self._clock, timeout_s)
            await self.engine.settle_background()
        finally:
            await self.engine.aclose()
        if self.recorder.terminal:
            self.engine.state.ended = True
        return finished

    def reply(self) -> str:
        """Every line the agent said in this run, joined by single spaces."""
        return " ".join(self.recorder.messages)
