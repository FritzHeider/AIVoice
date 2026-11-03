"""Pathway graph: nodes + edges + ``global_config`` (specs/11-pathways-engine.md §2, §3.4).

:func:`load_graph` validates the stored JSON arrays (raising ``pydantic.ValidationError``; use
:func:`schema_field` to turn an error ``loc`` into an ``INVALID_INPUT`` field path), :func:`dump_graph`
writes them back losslessly, and :func:`graph_hash` (specs/12-pathways-api.md §1) is
``sha256(canonical_json({nodes, edges, global_config}))`` with sorted keys and the editor-only node
keys ``position``, ``width`` and ``height`` removed (Decision P07-T01 under specs/11 §11). Duplicate node
ids are not rejected here; :mod:`va_engine.validation` reports them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, TypedDict

import orjson
from pydantic import Field, JsonValue

from va_engine.models.nodes import (
    NODE_TYPES,
    CamelModel,
    PathwayEdge,
    PathwayNode,
    dump_edge,
    dump_node,
    id_list,
)

__all__ = [
    "LAYOUT_NODE_KEYS",
    "GlobalConfig",
    "GraphJson",
    "PathwayGraph",
    "canonical_json",
    "dump_graph",
    "graph_hash",
    "load_graph",
    "schema_field",
]

LAYOUT_NODE_KEYS: Final = frozenset({"position", "width", "height"})
"""Editor-only node keys left out of :func:`graph_hash`."""


class GraphJson(TypedDict):
    """The stored graph JSON (``pathway_versions.nodes``/``edges``/``global_config``)."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    global_config: dict[str, Any]


class GlobalConfig(CamelModel):
    """``pathway_versions.global_config`` (§3.4)."""

    global_prompt: str | None = None
    apply_global_prompt_to_all_nodes: bool = True
    default_temperature: int | float = Field(default=0.7, ge=0, le=1)
    default_voice: str | None = None
    default_language: str | None = None
    memory_enabled: bool = False
    global_tools: list[str | dict[str, JsonValue]] = Field(default_factory=list)
    global_kb: str | list[str] | None = None
    max_total_turns: int = Field(default=200, ge=1, le=1_000)
    error_phrase: str = "I'm sorry, I'm having trouble right now."
    router_model: str | None = None
    dialogue_model: str | None = None
    no_match_behavior: Literal["stay", "fallback_edge"] = "stay"

    def kb_ids(self) -> list[str]:
        """``globalKb`` as a list."""
        return id_list(self.global_kb)

    def tool_ids(self) -> list[str]:
        """The tool ids in ``globalTools`` (inline tool objects excluded)."""
        return [tool for tool in self.global_tools if isinstance(tool, str)]


class PathwayGraph(CamelModel):
    """A loaded graph; node lookups use the first node with a given id."""

    nodes: list[PathwayNode] = Field(default_factory=list)
    edges: list[PathwayEdge] = Field(default_factory=list)
    global_config: GlobalConfig = Field(default_factory=GlobalConfig, alias="global_config")

    def node(self, node_id: str) -> PathwayNode | None:
        """The first node with ``node_id``, or ``None``."""
        return next((node for node in self.nodes if node.id == node_id), None)

    def start_nodes(self) -> list[PathwayNode]:
        """Nodes with ``isStart`` (exactly one in a valid graph)."""
        return [node for node in self.nodes if node.data.is_start]

    def global_nodes(self) -> list[PathwayNode]:
        """Nodes with ``isGlobal``."""
        return [node for node in self.nodes if node.data.is_global]

    def out_edges(self, node_id: str) -> list[PathwayEdge]:
        """Edges leaving ``node_id`` in array order."""
        return [edge for edge in self.edges if edge.source == node_id]

    def hash(self) -> str:
        """:func:`graph_hash` of the dumped graph."""
        return graph_hash(dump_graph(self))


def load_graph(
    nodes: Sequence[object],
    edges: Sequence[object],
    global_config: Mapping[str, object] | None = None,
) -> PathwayGraph:
    """Validate stored graph JSON (raises ``pydantic.ValidationError``)."""
    return PathwayGraph.model_validate(
        {"nodes": list(nodes), "edges": list(edges), "global_config": dict(global_config or {})}
    )


def dump_graph(graph: PathwayGraph) -> GraphJson:
    """The graph as stored JSON (:func:`~va_engine.models.nodes.dump_node` rules for every part)."""
    return {
        "nodes": [dump_node(node) for node in graph.nodes],
        "edges": [dump_edge(edge) for edge in graph.edges],
        "global_config": graph.global_config.model_dump(mode="json", by_alias=True, exclude_unset=True),
    }


def canonical_json(value: object) -> bytes:
    """Compact UTF-8 JSON with sorted object keys (the hashing form)."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def graph_hash(graph: GraphJson | Mapping[str, Any]) -> str:
    """Hex sha256 of the canonical ``{nodes, edges, global_config}`` JSON without node layout keys."""
    nodes = graph.get("nodes") or []
    hashed = {
        "nodes": [
            {key: value for key, value in node.items() if key not in LAYOUT_NODE_KEYS}
            if isinstance(node, Mapping)
            else node
            for node in nodes
        ],
        "edges": graph.get("edges") or [],
        "global_config": graph.get("global_config") or {},
    }
    return hashlib.sha256(canonical_json(hashed)).hexdigest()


def schema_field(loc: Sequence[int | str]) -> str:
    """A pydantic error ``loc`` from :func:`load_graph` as a field path (``nodes[3].data.name``).

    The node-type tag pydantic inserts after a node index (``("nodes", 3, "Default", "data")``) is dropped.
    """
    path = ""
    previous: int | str | None = None
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif not (isinstance(previous, int) and part in NODE_TYPES and path.startswith("nodes")):
            path = f"{path}.{part}" if path else part
        previous = part
    return path
