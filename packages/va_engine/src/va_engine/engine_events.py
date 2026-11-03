"""The events a pathway turn emits (specs/11 §5.9, specs/19 §2; Decision P07-T08b under specs/11 §5.9).

The engine hands each event to an :class:`EngineEvents` sink as ``(event_type, data)``; the transport adapter
(media worker, text channels) publishes it. The helpers here build the ``data`` of the decision events and of
the routes a node handler chose itself (webhook response routing, scheduling failure, SMS fallback).
"""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from va_engine.extraction import ExtractionResult
from va_engine.handlers.reports import NodeReport
from va_engine.loop_condition import ConditionResult
from va_engine.models.graph import PathwayGraph

__all__ = [
    "CONDITION_EVENT",
    "EXTRACTION_EVENT",
    "ROUTE_EVENT",
    "EngineEvents",
    "condition_event_data",
    "extraction_event_data",
    "extraction_worth_reporting",
    "handler_route_data",
    "report_event",
]

ROUTE_EVENT = "call.route"
EXTRACTION_EVENT = "call.extraction"
CONDITION_EVENT = "call.loop_condition"


class EngineEvents(Protocol):
    """Receives the turn's events in order; must not block (the adapter queues the publication)."""

    def emit(self, event_type: str, data: dict[str, JsonValue]) -> None: ...


def report_event(report: NodeReport) -> tuple[str, dict[str, JsonValue]]:
    """The event of a handler report (``call.extraction`` / ``call.webhook_node`` / ``call.sms_sent``)."""
    return report.event_type, report.event_data()


def extraction_worth_reporting(result: ExtractionResult) -> bool:
    """Whether an extraction produced anything to report (values, nulls, rejected values, or a failure)."""
    return bool(result.values or result.nulls or result.rejected or result.degraded)


def extraction_event_data(node_id: str, result: ExtractionResult) -> dict[str, JsonValue]:
    """``call.extraction`` data of a node's ``extractVars`` run."""
    return {"node_id": node_id, **result.event_data()}


def condition_event_data(node_id: str, result: ConditionResult) -> dict[str, JsonValue]:
    """``call.loop_condition`` data (score, met) plus the node and the ``Likely n/10`` message."""
    return {"node_id": node_id, **result.event_data(), "message": result.message}


def handler_route_data(graph: PathwayGraph, from_node: str, to_node: str) -> dict[str, JsonValue]:
    """``call.route`` data of a route a handler chose: the label of the first edge ``from_node → to_node``
    (``None`` without one), no alternatives, no router time."""
    label = next(
        (edge.data.label for edge in graph.out_edges(from_node) if edge.target == to_node),
        None,
    )
    return {
        "from_node": from_node,
        "to_node": to_node,
        "label": label,
        "alternatives": [],
        "global": False,
        "router_ms": 0,
    }
