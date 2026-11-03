"""One turn's decision calls (specs/11 §5.2-§5.5; Decision P07-T08b rule 2 under specs/11 §5.9).

:meth:`Decider.decide` runs the node's ``before_response`` extraction, its loop condition and the router (the
§5.2 combined call when global candidates are offered) concurrently, so none waits for another; the router
therefore sees the variables from before this turn's extraction. :class:`TurnDecision` says how the results
combine: a ``global`` route wins over everything, else an unmet condition holds the conversation on the node
(the router result is discarded), else the route applies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from va_common.time import Clock
from va_engine.decisions import DecisionInputs, StructuredPort
from va_engine.extraction import ExtractionResult, Extractor
from va_engine.loop_condition import ConditionResult, LoopCondition, not_met_suffix
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayNode
from va_engine.router import RouteDecision, Router

__all__ = ["Decider", "TurnDecision"]


@dataclass(frozen=True, slots=True)
class TurnDecision:
    """The three decision results of one pass (``condition``/``extraction`` ``None`` when the node has
    none or extracts in the background)."""

    route: RouteDecision
    condition: ConditionResult | None = None
    extraction: ExtractionResult | None = None

    @property
    def held(self) -> bool:
        """True when an unmet condition keeps the conversation on the node (a global route still wins)."""
        if self.route.kind == "global" or self.condition is None:
            return False
        return not self.condition.met

    @property
    def suffix(self) -> str | None:
        """The dialogue prompt suffix while the conversation is held by the condition."""
        if not self.held or self.condition is None:
            return None
        return not_met_suffix(self.condition.condition)


class Decider:
    """The router, loop-condition and extraction components over one structured port."""

    def __init__(self, gateway: StructuredPort, *, clock: Clock) -> None:
        self._router = Router(gateway, clock=clock)
        self._condition = LoopCondition(gateway, clock=clock)
        self._extractor = Extractor(gateway, clock=clock)

    async def decide(  # noqa: PLR0913 - the pass inputs, all but three keyword-only
        self,
        graph: PathwayGraph,
        node: PathwayNode,
        inputs: DecisionInputs,
        *,
        include_globals: bool,
        now: datetime,
        timezone: str,
    ) -> TurnDecision:
        """Route, score the condition and (``before_response``) extract, all at once."""
        extracts = node.data.extract_vars_mode == "before_response"
        async with asyncio.TaskGroup() as group:
            route = group.create_task(
                self._router.choose(graph, node, inputs, include_globals=include_globals)
            )
            condition = group.create_task(self._condition.score(node, inputs))
            extraction = (
                group.create_task(self._extractor.extract(node, inputs, now=now, timezone=timezone))
                if extracts
                else None
            )
        return TurnDecision(
            route=route.result(),
            condition=condition.result(),
            extraction=extraction.result() if extraction is not None else None,
        )

    async def extract(
        self, node: PathwayNode, inputs: DecisionInputs, *, now: datetime, timezone: str
    ) -> ExtractionResult | None:
        """The node's ``extractVars`` over ``inputs`` (background extraction, webhook nodes)."""
        return await self._extractor.extract(node, inputs, now=now, timezone=timezone)
