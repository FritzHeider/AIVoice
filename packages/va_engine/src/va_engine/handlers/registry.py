"""Node type → handler (specs/11 §4).

Node types whose handler arrives in a later task (Webhook, SMS, Press Button, Transfer Pathway, Scheduling:
P07-T07; Knowledge Base, Custom Code: phase 08) raise :class:`UnsupportedNodeError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from va_engine.handlers.base import NodeHandler
from va_engine.handlers.default import DefaultHandler
from va_engine.handlers.end_call import EndCallHandler
from va_engine.handlers.transfer_call import TransferCallHandler
from va_engine.handlers.wait_for_response import WaitForResponseHandler
from va_engine.models.nodes import PathwayNode

__all__ = ["HANDLERS", "UnsupportedNodeError", "handler_for"]

HANDLERS: Final[Mapping[str, NodeHandler]] = {
    "Default": DefaultHandler(),
    "Wait for Response": WaitForResponseHandler(),
    "End Call": EndCallHandler(),
    "Transfer Call": TransferCallHandler(),
}


class UnsupportedNodeError(LookupError):
    """No handler exists for ``node_type`` yet."""

    def __init__(self, node_type: str) -> None:
        super().__init__(f"no handler for node type {node_type!r}")
        self.node_type = node_type


def handler_for(node: PathwayNode) -> NodeHandler:
    """The handler of ``node``'s type (global nodes are Default nodes)."""
    handler = HANDLERS.get(node.type)
    if handler is None:
        raise UnsupportedNodeError(node.type)
    return handler
