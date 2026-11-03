"""Transfer Pathway node handler (specs/11 §4 "Transfer Pathway" row, §3.2.6; Decision P07-T07 under §4).

``on_enter`` asks for a :class:`~va_engine.models.results.SwitchGraph`; the turn orchestration applies it with
:meth:`va_engine.navigation.Navigator.switch_graph`, which loads the target version, pushes the return frame
(``returnOnEnd``) and enters the target node. Everything else is delegated to the target node.
"""

from __future__ import annotations

from typing import Final

from va_common.logging import get_logger
from va_engine.handlers.base import HandlerResult, TurnContext
from va_engine.models.nodes import PathwayNode
from va_engine.models.results import SwitchGraph

__all__ = ["PATHWAY_MISSING", "TransferPathwayHandler"]

PATHWAY_MISSING: Final = "transfer_pathway_missing"

_log = get_logger(__name__)


class TransferPathwayHandler:
    """Transfer Pathway nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """A :class:`SwitchGraph` to the configured pathway; a problem without a pathway id."""
        config = ctx.node.data.transfer_pathway
        pathway_id = config.pathway_id.strip() if config is not None else ""
        if config is None or not pathway_id:
            _log.warning("engine.transfer_pathway_missing", node_id=ctx.node.id)
            return HandlerResult(problem=PATHWAY_MISSING)
        node_id = config.node_id.strip() if config.node_id else None
        switch = SwitchGraph(
            pathway_id=pathway_id,
            version=config.version,
            node_id=node_id or None,
            carry_variables=config.carry_variables,
            return_on_end=config.return_on_end,
        )
        return HandlerResult((switch,))

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """Nothing: the target node responds."""
        del ctx
        return HandlerResult()

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Never: waiting is the target node's business."""
        del node
        return False
