"""What a pathway call needs besides its graph (specs/11 §10; Decision P07-T12a under specs/11 §10).

:func:`pathway_facts` builds the engine's :class:`~va_engine.engine.ConversationFacts` from the call's
:class:`~va_domain.call_context.CallContext` (``transfer_list`` stays empty: pathways ignore it, specs/05
§3). :func:`start_state` is the fresh :class:`~va_engine.models.EngineState` of the call: the graph's start
node, or the call's ``start_node_id`` when the graph has that node (an unknown id falls back to the start
node, logged), seeded with the context's variables.
"""

from __future__ import annotations

from datetime import datetime

from va_common.logging import get_logger
from va_domain.call_context import CallContext
from va_engine.builtins import call_builtins
from va_engine.engine import ConversationFacts, new_pathway_state
from va_engine.models import EngineState
from va_engine.navigation import ActiveGraph

__all__ = ["pathway_facts", "start_state"]

_log = get_logger(__name__)


def pathway_facts(context: CallContext, *, now: datetime) -> ConversationFacts:
    """The facts of a voice pathway call at ``now`` (an aware datetime)."""
    config = context.config
    return ConversationFacts(
        org_id=context.org_id,
        call_id=context.call_id,
        channel="voice",
        request_data=dict(config.request_data),
        builtins=call_builtins(context, now=now),
        timezone=config.timezone,
    )


def start_state(active: ActiveGraph, context: CallContext) -> EngineState:
    """The call's first state on ``active`` (see the module docstring)."""
    state = new_pathway_state(active)
    state.variables.update(context.variables)
    start_node_id = context.config.start_node_id
    if start_node_id is None or start_node_id == state.current_node_id:
        return state
    if active.graph.node(start_node_id) is None:
        _log.warning("pathway_call.start_node_missing", node_id=start_node_id)
        return state
    state.current_node_id = start_node_id
    state.visit_counts = {start_node_id: 1}
    return state
