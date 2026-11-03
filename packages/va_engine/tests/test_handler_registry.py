"""Node type → handler mapping (specs/11 §4, Decisions P07-T06 and P07-T07)."""

from __future__ import annotations

import pytest
from graph_helpers import graph, node, start

from va_engine.handlers import (
    HANDLERS,
    DefaultHandler,
    EndCallHandler,
    PressButtonHandler,
    SchedulingHandler,
    SmsHandler,
    TransferCallHandler,
    TransferPathwayHandler,
    UnsupportedNodeError,
    WaitForResponseHandler,
    WebhookHandler,
    handler_for,
)
from va_engine.models import NODE_TYPES


@pytest.mark.parametrize(
    ("node_type", "handler"),
    [
        ("Default", DefaultHandler),
        ("Wait for Response", WaitForResponseHandler),
        ("End Call", EndCallHandler),
        ("Transfer Call", TransferCallHandler),
        ("Webhook", WebhookHandler),
        ("SMS", SmsHandler),
        ("Press Button", PressButtonHandler),
        ("Transfer Pathway", TransferPathwayHandler),
        ("Scheduling", SchedulingHandler),
    ],
)
def test_each_handled_type_maps_to_its_handler(node_type: str, handler: type) -> None:
    loaded = graph([start(), node("n", node_type=node_type, prompt="p")])
    current = loaded.node("n")
    assert current is not None

    assert isinstance(handler_for(current), handler)


def test_only_knowledge_base_and_custom_code_lack_a_handler() -> None:
    assert set(NODE_TYPES) - set(HANDLERS) == {"Knowledge Base", "Custom Code"}


@pytest.mark.parametrize("node_type", sorted(set(NODE_TYPES) - set(HANDLERS)))
def test_types_without_a_handler_raise(node_type: str) -> None:
    loaded = graph([start(), node("n", node_type=node_type, prompt="p")])
    current = loaded.node("n")
    assert current is not None

    with pytest.raises(UnsupportedNodeError) as caught:
        handler_for(current)

    assert caught.value.node_type == node_type
    assert node_type in str(caught.value)
