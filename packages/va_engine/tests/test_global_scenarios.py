"""Nested global node triggers over the real router (specs/11 §4, §5.2; Decision P07-T05).

Each turn: the router (scripted ``select_path`` outputs) decides from the current node, with global candidates
only when :meth:`Navigator.global_check_enabled`; the navigator applies the decision, then finishes the
entered node (auto-return / forwarding). The state round-trips through its JSON between turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decision_helpers import ScriptedPort, inputs
from navigation_helpers import P1, P2, MemoryLoader, active, nav_state

from va_engine.models.state import EngineState, dump_engine_state, load_engine_state
from va_engine.navigation import Navigator, Transition
from va_engine.router import Router


def pick(path_id: str, confidence: int, *alternatives: tuple[str, int]) -> dict[str, Any]:
    return {
        "path_id": path_id,
        "confidence": confidence,
        "alternatives": [{"path_id": p, "confidence": c} for p, c in alternatives],
        "reason": "scripted",
    }


@dataclass(slots=True)
class Conversation:
    """Drives one pathway conversation turn by turn."""

    port: ScriptedPort = field(default_factory=ScriptedPort)
    loader: MemoryLoader = field(default_factory=MemoryLoader.with_other)
    state: EngineState = field(default_factory=nav_state)
    nav: Navigator | None = None
    trace: list[tuple[str, str, int]] = field(default_factory=list)

    def navigator(self) -> Navigator:
        if self.nav is None:
            self.nav = Navigator(self.state, active(), loader=self.loader)
        return self.nav

    async def turn(self, said: str, reply: dict[str, Any]) -> list[Transition]:
        nav = self.navigator()
        self.port.script.append(reply)
        router = Router(self.port, clock=self.port.clock)
        decision = await router.choose(
            nav.graph, nav.node, inputs(said), include_globals=nav.global_check_enabled()
        )
        steps = [nav.apply(decision)]
        if steps[-1].speaks:
            steps.append(await nav.finish_node())
        if steps[-1].kind == "forward":
            steps.append(await nav.finish_node())
        self._persist(nav)
        self.trace.append(
            (self.state.current_node_id, str(self.state.pathway_id), len(self.state.return_stack))
        )
        return steps

    def _persist(self, nav: Navigator) -> None:
        restored = load_engine_state(dump_engine_state(nav.state))
        assert restored == nav.state
        nav.state = restored
        self.state = restored

    def offered_globals(self) -> list[bool]:
        return ["- id: global:" in request.system for request in self.port.requests]


async def test_global_auto_returns_to_the_node_it_interrupted() -> None:
    talk = Conversation()

    hours = await talk.turn("What are your hours?", pick("global:hours", 8, ("e_order", 2)))
    ordering = await talk.turn("I want to order", pick("e_order", 9))

    assert [(t.kind, t.to_node) for t in hours] == [("global", "hours"), ("return", "greet")]
    assert [(t.kind, t.to_node) for t in ordering] == [("enter", "order"), ("stay", "order")]
    assert talk.trace == [("greet", str(P1), 0), ("order", str(P1), 0)]
    assert talk.state.visit_counts == {"hours": 1, "order": 1}


async def test_nested_global_returns_to_the_forwarded_node() -> None:
    talk = Conversation()

    menu = await talk.turn("What's on the menu?", pick("global:menu", 9))
    hours = await talk.turn("And when do you close?", pick("global:hours", 7, ("e_confirm", 3)))
    done = await talk.turn("That's all", pick("e_confirm", 8))

    assert [(t.kind, t.to_node) for t in menu] == [
        ("global", "menu"),
        ("forward", "order"),
        ("stay", "order"),
    ]
    assert [(t.kind, t.to_node) for t in hours] == [("global", "hours"), ("return", "order")]
    assert done[0].to_node == "confirm"
    assert talk.trace == [("order", str(P1), 0), ("order", str(P1), 0), ("confirm", str(P1), 0)]


async def test_global_precedence_on_a_tie_and_threshold() -> None:
    tie = Conversation()
    low = Conversation()

    tied = await tie.turn("hours, and I'd like to order", pick("global:hours", 7, ("e_order", 7)))
    below = await low.turn("maybe hours?", pick("global:hours", 5, ("e_order", 6)))

    assert tied[0].kind == "global"
    assert (below[0].kind, below[0].to_node) == ("enter", "order")
    assert low.state.return_stack == []


async def test_global_without_auto_return_then_a_new_global_returns_to_the_new_node() -> None:
    talk = Conversation()

    human = await talk.turn("Let me talk to a person", pick("global:human", 9))
    # On the global node no global candidates are offered: the human node's own edges decide.
    left = await talk.turn("Thanks, bye", pick("e_goodbye", 8))
    hours = await talk.turn("Wait, what are your hours?", pick("global:hours", 9))

    assert [(t.kind, t.to_node) for t in human] == [("global", "human"), ("stay", "human")]
    assert talk.trace[0] == ("human", str(P1), 1)
    assert (left[0].kind, left[0].to_node) == ("enter", "goodbye")
    assert [(t.kind, t.to_node) for t in hours] == [("global", "hours"), ("return", "goodbye")]
    assert [depth for _, _, depth in talk.trace] == [1, 0, 0]
    assert talk.offered_globals() == [True, False, True]


async def test_global_of_the_forwarded_pathway_returns_inside_it() -> None:
    talk = Conversation()

    billing = await talk.turn("I have a billing question", pick("global:other_pathway", 9))
    assert talk.nav is not None
    assert talk.nav.prev_node_prompt() is None
    agent = await talk.turn("Get me an agent", pick("global:agent", 8, ("e_refund", 1)))
    refund = await talk.turn("I want a refund", pick("e_refund", 9))

    assert [(t.kind, t.to_node, t.pathway_changed) for t in billing][:2] == [
        ("global", "other_pathway", False),
        ("forward", "billing", True),
    ]
    assert [(t.kind, t.to_node) for t in agent] == [("global", "agent"), ("return", "billing")]
    assert refund[0].to_node == "refund"
    assert talk.trace == [("billing", str(P2), 0), ("billing", str(P2), 0), ("refund", str(P2), 0)]
    assert talk.loader.loads == [(str(P2), 3)]
    assert talk.state.pathway_version == 3


async def test_prev_node_prompt_is_available_while_the_global_speaks() -> None:
    talk = Conversation()
    nav = talk.navigator()
    talk.port.script.append(pick("global:human", 9))
    decision = await Router(talk.port, clock=talk.port.clock).choose(
        nav.graph, nav.node, inputs("A human please"), include_globals=True
    )

    nav.apply(decision)

    assert nav.prev_node_prompt() == "Greet the caller."
