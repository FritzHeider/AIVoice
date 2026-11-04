"""Unit tests of the e2e pathway helpers and the bundled restaurant template (specs/30 §5 Decision
P07-T12c)."""

from __future__ import annotations

from typing import Any, Final

from e2e_pathways import (
    load_template,
    route_targets,
    webhook_graph,
    with_webhook_url,
)

from va_engine.models.graph import load_graph
from va_engine.validation import validate_graph

RECEIVER: Final = "http://host.docker.internal:18080/bookings/abc"


def _errors(graph: dict[str, Any]) -> list[str]:
    loaded = load_graph(graph["nodes"], graph["edges"], graph.get("global_config"))
    issues = validate_graph(loaded, mode="publish", allow_http=True)
    return [f"{issue.code} {issue.node_id or issue.edge_id}" for issue in issues if issue.severity == "error"]


def test_the_restaurant_template_publishes_once_pointed_at_the_receiver() -> None:
    template = load_template("restaurant_template")
    assert _errors(with_webhook_url(template, RECEIVER)) == []
    assert template["global_config"]["globalPrompt"].startswith("You are Ava")


def test_the_restaurant_template_books_through_its_webhook_node() -> None:
    template = load_template("restaurant_template")
    by_id = {node["id"]: node for node in template["nodes"]}
    assert [node["type"] for node in template["nodes"]] == ["Default", "Default", "Webhook", "End Call"]
    assert [var["name"] for var in by_id["reserve"]["data"]["extractVars"]] == ["party_size", "time", "name"]
    webhook = by_id["book"]["data"]["webhook"]
    assert webhook["body"] == {"party_size": "{{party_size}}", "time": "{{time}}", "name": "{{name}}"}
    assert set(webhook["routeOnStatus"].values()) == {"goodbye"}
    always = [edge["target"] for edge in template["edges"] if edge["data"].get("alwaysPick")]
    assert always == ["book"]


def test_with_webhook_url_rewrites_only_webhook_nodes_of_a_copy() -> None:
    template = load_template("restaurant_template")
    rewritten = with_webhook_url(template, RECEIVER)
    changed = [
        node["id"]
        for node, original in zip(rewritten["nodes"], template["nodes"], strict=True)
        if node != original
    ]
    assert changed == ["book"]
    assert rewritten["nodes"][2]["data"]["webhook"]["url"] == RECEIVER
    assert template["nodes"][2]["data"]["webhook"]["url"].startswith("https://reservations.")
    assert rewritten["edges"] == template["edges"]


def test_the_webhook_node_graph_publishes() -> None:
    graph = webhook_graph(RECEIVER)
    assert _errors(graph) == []
    confirm = next(node for node in graph["nodes"] if node["type"] == "Webhook")
    assert confirm["data"]["webhook"]["url"] == RECEIVER


def test_route_targets_reads_route_events_in_log_order() -> None:
    events = [
        {"type": "call.started", "data": {}},
        {"type": "call.route", "data": {"from_node": "greet", "to_node": "reserve"}},
        {"type": "call.extraction", "data": {"node_id": "reserve"}},
        {"type": "call.route", "data": {"from_node": "reserve", "to_node": "book"}},
    ]
    assert route_targets(events) == ["reserve", "book"]
