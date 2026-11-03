"""Builders for pathway graph tests (specs/11 §3, §11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from va_engine.models import PathwayGraph, load_graph
from va_engine.validation import GraphRefs

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "bland"
CANONICAL_FIXTURES: Final = ("restaurant_ordering", "appointment_booking", "lead_qualification")

KB_MENU: Final = "KB-0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a01"
TL_HOURS: Final = "TL-0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a02"
TL_CRM: Final = "TL-0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a03"
PW_BILLING: Final = "0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a04"
PW_SURVEY: Final = "0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a05"
PW_SELF: Final = "0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a99"

FIXTURE_REFS: Final = GraphRefs(
    pathway_id=PW_SELF,
    kb_ids=frozenset({KB_MENU}),
    tool_ids=frozenset({TL_HOURS, TL_CRM}),
    pathway_ids=frozenset({PW_BILLING, PW_SURVEY}),
)


def fixture(name: str) -> dict[str, Any]:
    """A raw fixture graph ``{nodes, edges, global_config}``."""
    raw: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return raw


def node(node_id: str, name: str | None = None, node_type: str = "Default", **data: Any) -> dict[str, Any]:
    """A raw node; ``data`` keys are Bland camelCase. Default nodes get a prompt unless one is given."""
    body: dict[str, Any] = {"name": name or node_id, **data}
    if node_type == "Default" and "prompt" not in body and "text" not in body:
        body["prompt"] = f"Talk about {node_id}."
    return {"id": node_id, "type": node_type, "data": body}


def edge(edge_id: str, source: str, target: str, label: str = "go", **data: Any) -> dict[str, Any]:
    """A raw edge with a label."""
    return {"id": edge_id, "source": source, "target": target, "data": {"label": label, **data}}


def start(node_id: str = "start", **data: Any) -> dict[str, Any]:
    """A raw start node."""
    return node(node_id, isStart=True, **data)


def graph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    global_config: dict[str, Any] | None = None,
) -> PathwayGraph:
    """A loaded graph from raw parts."""
    return load_graph(nodes, edges or [], global_config)
