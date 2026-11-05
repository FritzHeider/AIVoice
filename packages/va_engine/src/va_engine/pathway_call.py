"""What a pathway call needs besides its graph (specs/11 §10; Decision P07-T12a under specs/11 §10).

:func:`pathway_facts` builds the engine's :class:`~va_engine.engine.ConversationFacts` from the call's
:class:`~va_domain.call_context.CallContext` (``transfer_list`` stays empty: pathways ignore it, specs/05
§3) plus the system tools the runtime allows its reply nodes and the org's ``engine.tool_fillers``
(specs/13 §4 Decision P08-T02c2). :func:`start_state` is the fresh :class:`~va_engine.models.EngineState` of
the call: the graph's start node, or the call's ``start_node_id`` when the graph has that node (an unknown id
falls back to the start node, logged), seeded with the context's variables.

:func:`opener_text` is the line the engine says first when the call's start node is a Default node with a
static ``text``: ``prefetch_call_context`` stores it as ``CallContext.opener_text`` and pre-renders it into
the phrase cache, so the engine's start ``Say`` (rendered by the same rules at call time) hits the cache
(specs/07 §11 step 5; Decision P07-T12b under specs/11 §10). :func:`engine_state_key` names the Redis snapshot
of a running call's :class:`~va_engine.models.EngineState` (specs/03 §11 ``call:{id}:engine``, kept
:data:`ENGINE_STATE_TTL_S`; specs/11 §9).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from va_common.logging import get_logger
from va_domain.call_context import CallContext
from va_engine.builtins import call_builtins
from va_engine.engine import ConversationFacts, new_pathway_state
from va_engine.handlers.base import has_content
from va_engine.models import EngineState
from va_engine.navigation import ActiveGraph
from va_engine.system_tools import SystemToolFacts
from va_engine.templating import TemplateScope, TemplateSyntaxError, render

__all__ = ["ENGINE_STATE_TTL_S", "engine_state_key", "opener_text", "pathway_facts", "start_state"]

ENGINE_STATE_TTL_S: Final = 7200
"""Lifetime of a call's engine snapshot in Redis (2 h, specs/03 §11)."""

_log = get_logger(__name__)


def pathway_facts(
    context: CallContext,
    *,
    now: datetime,
    system_tools: SystemToolFacts | None = None,
    tool_fillers: bool = True,
) -> ConversationFacts:
    """The facts of a voice pathway call at ``now`` (an aware datetime). ``system_tools``: what the call
    allows its reply nodes to offer (``None`` = no system tool); ``tool_fillers``: ``engine.tool_fillers``."""
    config = context.config
    return ConversationFacts(
        org_id=context.org_id,
        call_id=context.call_id,
        channel="voice",
        request_data=dict(config.request_data),
        builtins=call_builtins(context, now=now),
        timezone=config.timezone,
        system_tools=system_tools,
        tool_fillers=tool_fillers,
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


def engine_state_key(call_id: uuid.UUID) -> str:
    """``call:{<id>}:engine``: the Redis key of a running call's engine snapshot (hash-tag form)."""
    return f"call:{{{call_id}}}:engine"


def opener_text(active: ActiveGraph, context: CallContext, *, now: datetime) -> str | None:
    """The start node's static line at ``now``, rendered like the engine's ``Say`` (variables, request data,
    built-ins, timezone; a template syntax error keeps the raw text), stripped. ``None`` with
    ``wait_for_greeting`` (the greeting may change the first reply), when the start node is not a Default node
    (global nodes included) or has no static ``text``, and for a blank render."""
    if context.config.wait_for_greeting:
        return None
    state = start_state(active, context)
    node = active.graph.node(state.current_node_id)
    if node is None or node.type != "Default" or node.data.is_global or not has_content(node.data.text):
        return None
    source = node.data.text or ""
    config = context.config
    scope = TemplateScope(
        variables=state.variables,
        request_data=config.request_data,
        builtins=call_builtins(context, now=now),
        timezone=config.timezone,
    )
    try:
        text = render(source, scope)
    except TemplateSyntaxError:
        text = source
    return text.strip() or None
