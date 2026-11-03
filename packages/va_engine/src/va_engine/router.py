"""Pathway routing and the global node check (specs/11 §5.2, §5.5; specs/10 §9.1; Decision P07-T04).

:meth:`Router.choose` decides where the conversation goes from a node: an ``alwaysPick`` edge or a
webhook-selected edge without asking the model, otherwise one ``select_path`` call over the node's candidate
edges (plus the global nodes when asked) whose scores :func:`interpret_route` turns into a
:class:`RouteDecision`. The router never changes engine state; the turn orchestration applies the decision and
emits ``call.route`` from :meth:`RouteDecision.event_data`. When the model is unavailable or its output stays
invalid, :func:`heuristic_route` (shared content words) decides instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.decision_schemas import route_schema
from va_engine.decisions import (
    DecisionInputs,
    StructuredPort,
    content_words,
    elapsed_ms,
    known_information,
    latest_line,
    one_line,
    transcript_lines,
)
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayEdge, PathwayExample, PathwayNode
from va_llm.errors import LlmUnavailableError, StructuredOutputError
from va_llm.models import ChatMessage
from va_llm.prompts import render_prompt

__all__ = [
    "GLOBAL_MIN_SCORE",
    "GLOBAL_PREFIX",
    "LOCAL_MIN_SCORE",
    "ROUTER_HISTORY",
    "STAY",
    "GlobalCandidate",
    "RouteDecision",
    "RouteKind",
    "Router",
    "RouterOutput",
    "ScoredOption",
    "heuristic_route",
    "interpret_route",
    "render_router_prompt",
]

STAY: Final = "stay"
GLOBAL_PREFIX: Final = "global:"
LOCAL_MIN_SCORE: Final = 5
GLOBAL_MIN_SCORE: Final = 6
ROUTER_HISTORY: Final = 8
PURPOSE_CHARS: Final = 300
HEURISTIC_MIN_SHARED: Final = 2
ROUTER_DESCRIPTION: Final = "Choose the next path of the phone conversation."

type RouteKind = Literal["edge", "global", "fallback", "stay"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScoredOption:
    """A path the router considered (edge label or global label) and its 0-10 score."""

    label: str
    score: int


@dataclass(frozen=True, slots=True)
class GlobalCandidate:
    """A global node offered to the router as ``global:{node_id}``."""

    node_id: str
    label: str

    @property
    def path_id(self) -> str:
        return f"{GLOBAL_PREFIX}{self.node_id}"


@dataclass(frozen=True, slots=True)
class RouterOutput:
    """The fields of a ``select_path`` output the interpretation reads."""

    path_id: str
    confidence: int
    alternatives: tuple[tuple[str, int], ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Where the conversation goes from ``from_node``.

    ``edge`` / ``fallback``: follow ``edge``; ``global``: push the current node and jump to
    ``global_node_id``; ``stay``: remain (``to_node == from_node``). ``llm_called`` tells whether a router
    request was made, ``degraded`` that it failed and the heuristic decided.
    """

    kind: RouteKind
    from_node: str
    to_node: str
    edge: PathwayEdge | None = None
    global_node_id: str | None = None
    label: str | None = None
    alternatives: tuple[ScoredOption, ...] = ()
    reason: str = ""
    router_ms: int = 0
    llm_called: bool = False
    degraded: bool = False

    @property
    def moves(self) -> bool:
        """True when the decision leaves the current node."""
        return self.kind != "stay"

    def event_data(self) -> dict[str, JsonValue]:
        """``call.route`` data (specs/19 §2, specs/11 §5.5)."""
        return {
            "from_node": self.from_node,
            "to_node": self.to_node,
            "label": self.label,
            "alternatives": [{"label": option.label, "score": option.score} for option in self.alternatives],
            "global": self.kind == "global",
            "router_ms": self.router_ms,
        }


def _edge_decision(
    kind: RouteKind,
    node: PathwayNode,
    edge: PathwayEdge,
    *,
    alternatives: tuple[ScoredOption, ...] = (),
    reason: str = "",
) -> RouteDecision:
    return RouteDecision(
        kind=kind,
        from_node=node.id,
        to_node=edge.target,
        edge=edge,
        label=edge.data.label or None,
        alternatives=alternatives,
        reason=reason,
    )


def _no_match(
    node: PathwayNode,
    fallback: PathwayEdge | None,
    *,
    alternatives: tuple[ScoredOption, ...] = (),
    reason: str = "",
) -> RouteDecision:
    if fallback is not None:
        return _edge_decision("fallback", node, fallback, alternatives=alternatives, reason=reason)
    return RouteDecision(
        kind="stay", from_node=node.id, to_node=node.id, alternatives=alternatives, reason=reason
    )


def _scores(output: RouterOutput, known: frozenset[str]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for path_id, confidence in ((output.path_id, output.confidence), *output.alternatives):
        if path_id in known:
            scores.setdefault(path_id, confidence)
    return scores


def interpret_route(
    node: PathwayNode,
    candidates: Sequence[PathwayEdge],
    fallback: PathwayEdge | None,
    globals_: Sequence[GlobalCandidate],
    output: RouterOutput,
) -> RouteDecision:
    """The decision for a router output (specs/11 §5.5 Decision P07-T04 rule 3); timing fields left 0."""
    known = frozenset({edge.id for edge in candidates} | {g.path_id for g in globals_} | {STAY})
    scores = _scores(output, known)
    stay_score = scores.get(STAY, 0)
    best_edge = max(
        enumerate(candidates),
        key=lambda item: (scores.get(item[1].id, 0), item[1].data.priority, -item[0]),
        default=None,
    )
    edge_score = scores.get(best_edge[1].id, 0) if best_edge is not None else 0
    best_global = max(
        enumerate(globals_), key=lambda item: (scores.get(item[1].path_id, 0), -item[0]), default=None
    )
    reason = output.reason
    if best_global is not None:
        candidate = best_global[1]
        score = scores.get(candidate.path_id, 0)
        if score >= GLOBAL_MIN_SCORE and score >= edge_score and score >= stay_score:
            return RouteDecision(
                kind="global",
                from_node=node.id,
                to_node=candidate.node_id,
                global_node_id=candidate.node_id,
                label=candidate.label or None,
                alternatives=_alternatives(scores, candidates, globals_, winner=candidate.path_id),
                reason=reason,
            )
    if best_edge is not None and edge_score >= LOCAL_MIN_SCORE and edge_score > stay_score:
        edge = best_edge[1]
        alternatives = _alternatives(scores, candidates, globals_, winner=edge.id)
        return _edge_decision("edge", node, edge, alternatives=alternatives, reason=reason)
    alternatives = _alternatives(scores, candidates, globals_, winner=None)
    return _no_match(node, fallback, alternatives=alternatives, reason=reason)


def _alternatives(
    scores: Mapping[str, int],
    candidates: Sequence[PathwayEdge],
    globals_: Sequence[GlobalCandidate],
    *,
    winner: str | None,
) -> tuple[ScoredOption, ...]:
    options = [(edge.id, edge.data.label) for edge in candidates]
    options.extend((candidate.path_id, candidate.label) for candidate in globals_)
    ranked = sorted(
        (
            (index, label, scores.get(path_id, 0))
            for index, (path_id, label) in enumerate(options)
            if path_id != winner
        ),
        key=lambda item: (-item[2], item[0]),
    )
    return tuple(ScoredOption(label, score) for _, label, score in ranked)


def heuristic_route(
    node: PathwayNode, candidates: Sequence[PathwayEdge], fallback: PathwayEdge | None, latest: str
) -> RouteDecision:
    """The specs/10 §9.1 fallback: the one candidate edge whose label shares ≥ 2 content words with
    ``latest``, else the fallback edge or stay (timing and ``degraded`` left to the caller)."""
    words = content_words(latest)
    matching = [
        edge for edge in candidates if len(content_words(edge.data.label) & words) >= HEURISTIC_MIN_SHARED
    ]
    if len(matching) == 1:
        return _edge_decision("edge", node, matching[0], reason="heuristic")
    return _no_match(node, fallback, reason="heuristic")


def _path_line(edge: PathwayEdge) -> str:
    line = f"- id: {edge.id} | condition: {one_line(edge.data.label)}"
    description = one_line(edge.data.description)
    return f"{line} | description: {description}" if description else line


def _example_line(example: PathwayExample, candidates: Sequence[PathwayEdge]) -> str | None:
    wanted = example.chosen_label.strip().lower()
    if not wanted:
        return None
    edge = next((edge for edge in candidates if edge.data.label.strip().lower() == wanted), None)
    said = next(
        (message.content for message in reversed(example.conversation) if message.role == "user"), None
    )
    if edge is None or said is None:
        return None
    return f'Caller said: "{one_line(said)}" → path {edge.id}'


def _purpose(node: PathwayNode) -> str:
    body = node.data.prompt or node.data.text or ""
    clipped = one_line(one_line(body)[:PURPOSE_CHARS])
    return f"{node.data.name}: {clipped}" if clipped else node.data.name


def render_router_prompt(
    node: PathwayNode, candidates: Sequence[PathwayEdge], globals_: Sequence[GlobalCandidate]
) -> str:
    """The specs/10 §9.1 system prompt for ``node``."""
    examples = [
        line for ex in node.data.pathway_examples if (line := _example_line(ex, candidates)) is not None
    ]
    global_lines = [f"- id: {g.path_id} | condition: {one_line(g.label)}" for g in globals_]
    return render_prompt(
        "router",
        {
            "node_name": node.data.name,
            "node_purpose": _purpose(node),
            "paths": "\n".join(_path_line(edge) for edge in candidates),
            "global_paths": "\n".join(global_lines) if global_lines else "None",
            "examples_block": "Examples:\n" + "\n".join(examples) + "\n\n" if examples else "",
        },
    )


def _user_message(inputs: DecisionInputs) -> str:
    lines = transcript_lines(inputs.history, ROUTER_HISTORY)
    lines.append(f"Known information: {known_information(inputs.variables)}")
    lines.append(latest_line(inputs.latest))
    return "\n".join(lines)


def _global_candidates(graph: PathwayGraph, node: PathwayNode) -> list[GlobalCandidate]:
    if node.data.is_global:
        return []
    return [
        GlobalCandidate(other.id, other.data.global_label or "")
        for other in graph.global_nodes()
        if other.id != node.id
    ]


def _output_of(raw: Mapping[str, object]) -> RouterOutput:
    alternatives = raw.get("alternatives")
    pairs: list[tuple[str, int]] = []
    if isinstance(alternatives, list):
        for item in alternatives:
            if isinstance(item, Mapping):
                path_id, confidence = item.get("path_id"), item.get("confidence")
                if isinstance(path_id, str) and isinstance(confidence, int):
                    pairs.append((path_id, confidence))
    path_id, confidence = raw.get("path_id"), raw.get("confidence")
    return RouterOutput(
        path_id=path_id if isinstance(path_id, str) else STAY,
        confidence=confidence if isinstance(confidence, int) else 0,
        alternatives=tuple(pairs),
        reason=str(raw.get("reason", "")),
    )


class Router:
    """Routing decisions over the router model (one ``select_path`` call at most per decision)."""

    def __init__(self, gateway: StructuredPort, *, clock: Clock) -> None:
        self._gateway = gateway
        self._clock = clock

    async def choose(
        self,
        graph: PathwayGraph,
        node: PathwayNode,
        inputs: DecisionInputs,
        *,
        include_globals: bool = False,
        webhook_edge: PathwayEdge | None = None,
    ) -> RouteDecision:
        """The next step from ``node`` (specs/11 §5.5 Decision P07-T04 rules 3 and 6); never raises for LLM
        failures. ``include_globals`` adds the graph's global nodes as candidates (the §5.2 combined call)."""
        edges = graph.out_edges(node.id)
        always = next((edge for edge in edges if edge.data.always_pick), None)
        if always is not None:
            return _edge_decision("edge", node, always)
        if webhook_edge is not None:
            return _edge_decision("edge", node, webhook_edge)
        candidates = [edge for edge in edges if not edge.data.is_fallback]
        fallback = next((edge for edge in edges if edge.data.is_fallback), None)
        globals_ = _global_candidates(graph, node) if include_globals else []
        if not candidates and not globals_:
            return _no_match(node, fallback)
        options = (*(edge.id for edge in candidates), STAY, *(g.path_id for g in globals_))
        messages = [
            ChatMessage(role="system", content=render_router_prompt(node, candidates, globals_)),
            ChatMessage(role="user", content=_user_message(inputs)),
        ]
        started = self._clock.monotonic()
        try:
            output = await self._gateway.call_structured(
                "router",
                route_schema(options),
                org_id=inputs.org_id,
                messages=messages,
                function_name="select_path",
                description=ROUTER_DESCRIPTION,
                call_id=inputs.call_id,
            )
        except (LlmUnavailableError, StructuredOutputError) as error:
            router_ms = elapsed_ms(started, self._clock.monotonic())
            _logger.warning("engine.router_degraded", node_id=node.id, reason=type(error).__name__)
            decision = heuristic_route(node, candidates, fallback, inputs.latest)
            return replace(decision, router_ms=router_ms, llm_called=True, degraded=True)
        router_ms = elapsed_ms(started, self._clock.monotonic())
        decision = interpret_route(node, candidates, fallback, globals_, _output_of(output.model_dump()))
        return replace(decision, router_ms=router_ms, llm_called=True)
