"""Pathway graph loading, lossless dumping, and the graph hash (specs/11 §2/§3.4/§14; specs/12 §1)."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from graph_helpers import CANONICAL_FIXTURES, edge, fixture, graph, node, start
from pydantic import ValidationError

from va_engine.models import GlobalConfig, canonical_json, dump_graph, graph_hash, load_graph, schema_field


@pytest.mark.parametrize("name", CANONICAL_FIXTURES)
def test_bland_fixtures_round_trip_losslessly(name: str) -> None:
    raw = fixture(name)

    loaded = load_graph(raw["nodes"], raw["edges"], raw["global_config"])

    assert dump_graph(loaded) == raw
    assert canonical_json(dump_graph(loaded)) == canonical_json(raw)


def test_fixtures_cover_every_node_type_and_global_nodes() -> None:
    types: set[str] = set()
    globals_seen = 0
    for name in CANONICAL_FIXTURES:
        raw = fixture(name)
        loaded = load_graph(raw["nodes"], raw["edges"], raw["global_config"])
        types.update(node.type for node in loaded.nodes)
        globals_seen += len(loaded.global_nodes())

    assert types == {
        "Default",
        "Wait for Response",
        "Webhook",
        "Knowledge Base",
        "Transfer Call",
        "End Call",
        "SMS",
        "Custom Code",
        "Press Button",
        "Transfer Pathway",
        "Scheduling",
    }
    assert globals_seen == len(CANONICAL_FIXTURES)


def test_legacy_import_normalizes_and_is_idempotent() -> None:
    raw = fixture("legacy_import")

    loaded = load_graph(raw["nodes"], raw["edges"], raw["global_config"])
    dumped = dump_graph(loaded)
    reloaded = load_graph(dumped["nodes"], dumped["edges"], dumped["global_config"])

    assert [node.type for node in loaded.nodes] == [
        "Default",
        "Default",
        "Transfer Call",
        "End Call",
        "Webhook",
        "Knowledge Base",
    ]
    assert loaded.nodes[1].data.is_global is True
    assert dump_graph(reloaded) == dumped
    assert reloaded.hash() == loaded.hash()


def test_graph_helpers_use_the_first_node_with_an_id() -> None:
    loaded = graph(
        [start("a"), node("b", isGlobal=True, globalLabel="x"), node("a", "Again")],
        [edge("e1", "a", "b"), edge("e2", "b", "a"), edge("e3", "a", "a")],
    )

    first = loaded.node("a")
    assert first is not None
    assert first.data.name == "a"
    assert loaded.node("zz") is None
    assert [n.id for n in loaded.start_nodes()] == ["a"]
    assert [n.id for n in loaded.global_nodes()] == ["b"]
    assert [e.id for e in loaded.out_edges("a")] == ["e1", "e3"]


def test_missing_global_config_loads_defaults_and_dumps_empty() -> None:
    loaded = load_graph([start()], [])

    assert loaded.global_config == GlobalConfig()
    assert loaded.global_config.max_total_turns == 200
    assert loaded.global_config.no_match_behavior == "stay"
    assert loaded.global_config.error_phrase == "I'm sorry, I'm having trouble right now."
    assert dump_graph(loaded)["global_config"] == {}


def test_global_config_id_helpers() -> None:
    config = GlobalConfig.model_validate({"globalKb": "KB-1", "globalTools": ["TL-1", {"name": "inline"}]})

    assert config.kb_ids() == ["KB-1"]
    assert config.tool_ids() == ["TL-1"]


def test_allow_end_call_tool_defaults_true_and_dumps_only_when_set() -> None:
    assert load_graph([start()], []).global_config.allow_end_call_tool is True

    loaded = load_graph([start()], [], {"allowEndCallTool": False})

    assert loaded.global_config.allow_end_call_tool is False
    assert dump_graph(loaded)["global_config"] == {"allowEndCallTool": False}


def test_schema_errors_map_to_field_paths() -> None:
    with pytest.raises(ValidationError) as caught:
        load_graph(
            [start(), {"id": "n2", "type": "Default", "data": {"name": ""}}],
            [{"id": "e1"}],
            {"maxTotalTurns": 0},
        )

    fields = sorted(schema_field(error["loc"]) for error in caught.value.errors())

    assert fields == [
        "edges[0].source",
        "edges[0].target",
        "global_config.maxTotalTurns",
        "nodes[1].data.name",
    ]


def test_schema_field_keeps_names_that_only_look_like_tags() -> None:
    assert schema_field(("global_config", "Default")) == "global_config.Default"
    assert schema_field(("nodes", 0)) == "nodes[0]"


def _reference_hash(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def test_graph_hash_is_sha256_of_sorted_compact_json_without_layout() -> None:
    raw = {
        "nodes": [
            {"id": "a", "type": "Default", "position": {"x": 1, "y": 2}, "width": 3, "height": 4, "data": {}}
        ],
        "edges": [{"id": "e", "source": "a", "target": "a", "data": {"label": "é"}}],
        "global_config": {"globalPrompt": "Hi", "defaultTemperature": 0.7},
    }
    expected = _reference_hash(
        {
            "nodes": [{"id": "a", "type": "Default", "data": {}}],
            "edges": raw["edges"],
            "global_config": raw["global_config"],
        }
    )

    assert graph_hash(raw) == expected


def test_graph_hash_ignores_key_order_and_layout_but_not_content() -> None:
    raw = fixture("restaurant_ordering")
    base = graph_hash(raw)

    reordered = {
        "global_config": dict(reversed(list(raw["global_config"].items()))),
        "edges": raw["edges"],
        "nodes": [dict(reversed(list(n.items()))) for n in raw["nodes"]],
    }
    moved = copy.deepcopy(raw)
    for n in moved["nodes"]:
        n["position"] = {"x": 999, "y": -1}
        n["width"] = 1
        n.pop("height")
    edited = copy.deepcopy(raw)
    edited["nodes"][0]["data"]["prompt"] += " Be brief."

    assert graph_hash(reordered) == base
    assert graph_hash(moved) == base
    assert graph_hash(edited) != base
    loaded = load_graph(raw["nodes"], raw["edges"], raw["global_config"])
    assert loaded.hash() == base


def test_graph_hash_of_empty_parts() -> None:
    assert graph_hash({}) == _reference_hash({"nodes": [], "edges": [], "global_config": {}})
