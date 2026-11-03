"""Node and edge schemas (specs/11-pathways-engine.md §3)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from va_engine.models import (
    NODE_TYPE_ALIASES,
    NODE_TYPES,
    EdgeData,
    PathwayNodeData,
    dump_edge,
    dump_node,
    effective_transfer_number,
    parse_edge,
    parse_node,
)
from va_engine.models.nodes import (
    CustomCodeConfig,
    EndCallConfig,
    ForwardingTarget,
    ModelOptions,
    PressButtonConfig,
    SchedulingConfig,
    SmsConfig,
    TransferConfig,
    TransferPathwayConfig,
    WaitForResponseConfig,
    WebhookConfig,
    normalize_node_raw,
)


def _error_locs(raw: object) -> list[tuple[int | str, ...]]:
    with pytest.raises(ValidationError) as caught:
        parse_node(raw)
    return [tuple(error["loc"]) for error in caught.value.errors()]


def test_node_data_defaults_match_the_spec() -> None:
    data = PathwayNodeData.model_validate({"name": "Greeting"})

    assert data.is_start is False
    assert data.is_global is False
    assert data.global_label is None
    assert data.enable_global_auto_return is True
    assert data.prompt is None
    assert data.text is None
    assert data.condition is None
    assert data.condition_threshold == 6
    assert data.extract_vars == []
    assert data.extract_vars_mode == "before_response"
    assert (data.pathway_examples, data.condition_examples, data.dialogue_examples) == ([], [], [])
    assert data.enable_forwarding is False
    assert data.forwarding_node is None
    assert data.kb is None
    assert data.kb_top_k == 5
    assert data.tools == []
    assert data.transfer_number is None
    assert data.max_visits == 8
    assert data.tag is None
    configs = (data.webhook, data.transfer, data.end_call, data.sms, data.press_button, data.transfer_pathway)
    assert configs == (None,) * 6
    assert (data.scheduling, data.custom_code, data.wait_for_response) == (None, None, None)


def test_model_options_defaults_inherit_except_resumption_speed() -> None:
    options = ModelOptions()

    assert options.temperature is None
    assert options.interruption_threshold is None
    assert options.skip_user_response is False
    assert options.block_interruptions is False
    assert options.resumption_speed == 2
    assert options.interruptibility is None
    assert options.model is None


def test_spec_config_examples_parse() -> None:
    webhook = WebhookConfig.model_validate(
        {
            "url": "https://api.acme.com/lookup?phone={{from}}",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{request_data.token}}"},
            "body": {"name": "{{first_name}}"},
            "timeoutMs": 10000,
            "retries": 0,
            "speech": "One moment while I check that.",
            "responseData": [
                {"name": "account_status", "data": "$.account.status", "context": "Customer status"}
            ],
            "routeOnStatus": {"2xx": None, "4xx": "node_err", "5xx": "node_err", "timeout": "node_err"},
            "responsePathways": [
                {
                    "variable": "account_status",
                    "operator": "==",
                    "value": "active",
                    "targetNodeId": "node_active",
                }
            ],
        }
    )
    assert webhook.route_on_status is not None
    assert webhook.route_on_status.targets() == ["node_err", "node_err", "node_err"]
    assert webhook.response_pathways[0].target_node_id == "node_active"
    assert webhook.response_data[0].data == "$.account.status"

    transfer = TransferConfig.model_validate(
        {
            "mode": "warm",
            "number": "+14155550100",
            "warm": {
                "briefingPrompt": "Brief",
                "onTargetNoAnswer": {"action": "move_to_node", "nodeId": "n2"},
            },
        }
    )
    assert transfer.warm is not None
    assert transfer.warm.hold_audio == "hold_music"
    assert transfer.warm.target_timeout_secs == 30
    assert transfer.warm.on_target_no_answer.node_id == "n2"
    assert transfer.caller_id_mode == "agent_number"

    assert EndCallConfig.model_validate({"speech": "Bye"}).speech_mode == "static"
    sms = SmsConfig.model_validate({"from": "+14155550100", "message": "Link: {{booking_url}}"})
    assert (sms.to, sms.from_, sms.continue_on_failure) == ("{{from}}", "+14155550100", True)
    assert PressButtonConfig.model_validate({"digits": "1w2#"}).duration_millis == 250
    target = TransferPathwayConfig.model_validate({"pathwayId": "p1", "version": 4})
    assert (target.version, target.node_id, target.carry_variables, target.return_on_end) == (
        4,
        None,
        True,
        False,
    )
    scheduling = SchedulingConfig.model_validate({"availabilityUrl": "https://a.test/slots"})
    assert (scheduling.slot_duration_minutes, scheduling.lookahead_days) == (30, 14)
    code = CustomCodeConfig.model_validate({"code": "export default async () => ({})"})
    assert (code.language, code.timeout_ms, code.network_allowlist) == ("typescript", 1000, [])
    wait = WaitForResponseConfig.model_validate({"onTimeout": "move_to_node", "timeoutNodeId": "n9"})
    assert (wait.wait_seconds, wait.max_reprompts, wait.timeout_node_id) == (30, 2, "n9")


@pytest.mark.parametrize(("alias", "canonical"), sorted(NODE_TYPE_ALIASES.items()))
def test_import_aliases_normalize_to_canonical_types(alias: str, canonical: str) -> None:
    node = parse_node({"id": "n1", "type": alias, "data": {"name": "N"}})

    assert node.type == canonical
    assert dump_node(node)["type"] == canonical


def test_global_type_becomes_default_with_is_global() -> None:
    node = parse_node({"id": "g", "type": "Global", "data": {"name": "G", "globalLabel": "Asks hours"}})

    assert node.type == "Default"
    assert node.data.is_global is True
    assert dump_node(node)["data"] == {"name": "G", "globalLabel": "Asks hours", "isGlobal": True}


def test_normalize_leaves_non_mappings_and_canonical_types_alone() -> None:
    raw = {"id": "n", "type": "Default", "data": {"name": "N"}}

    assert normalize_node_raw(raw) is raw
    assert normalize_node_raw([1]) == [1]
    assert normalize_node_raw({"type": 3}) == {"type": 3}
    assert normalize_node_raw({"type": "Global", "data": None}) == {"type": "Default", "data": None}


@pytest.mark.parametrize("node_type", NODE_TYPES)
def test_every_node_type_parses_into_its_own_class(node_type: str) -> None:
    node = parse_node({"id": "n1", "type": node_type, "data": {"name": "N"}})

    assert node.type == node_type
    assert type(node).__name__.endswith("Node")


def test_unknown_keys_are_kept_at_every_level() -> None:
    raw: dict[str, Any] = {
        "id": "n1",
        "type": "Webhook",
        "selected": True,
        "position": {"x": 1, "y": 2.5, "z": 0},
        "data": {
            "name": "Lookup",
            "active": False,
            "webhook": {"url": "https://a.test", "vendorFlag": "x", "routeOnStatus": {"3xx": "n2"}},
            "modelOptions": {"temperature": 1, "legacyKnob": [1, 2]},
        },
    }

    node = parse_node(raw)

    assert dump_node(node) == raw
    assert node.model_extra == {"selected": True}
    assert node.data.model_extra == {"active": False}


def test_dump_keeps_number_types_and_only_present_keys() -> None:
    raw = {
        "id": "n1",
        "type": "Default",
        "width": 320,
        "data": {"name": "N", "modelOptions": {"temperature": 0.5}},
    }

    dumped = dump_node(parse_node(raw))

    assert dumped == raw
    assert isinstance(dumped["width"], int)


@pytest.mark.parametrize(
    ("raw", "loc"),
    [
        ({"id": "", "type": "Default", "data": {"name": "N"}}, ("Default", "id")),
        ({"id": "a b", "type": "Default", "data": {"name": "N"}}, ("Default", "id")),
        ({"id": "x" * 65, "type": "Default", "data": {"name": "N"}}, ("Default", "id")),
        ({"id": "n", "type": "Default", "data": {"name": ""}}, ("Default", "data", "name")),
        ({"id": "n", "type": "Default", "data": {"name": "x" * 121}}, ("Default", "data", "name")),
        ({"id": "n", "type": "Default", "data": {}}, ("Default", "data", "name")),
        (
            {"id": "n", "type": "Default", "data": {"name": "N", "maxVisits": 0}},
            ("Default", "data", "maxVisits"),
        ),
        (
            {"id": "n", "type": "Default", "data": {"name": "N", "conditionThreshold": 11}},
            ("Default", "data", "conditionThreshold"),
        ),
        (
            {"id": "n", "type": "Webhook", "data": {"name": "N", "webhook": {"timeoutMs": 99}}},
            ("Webhook", "data", "webhook", "timeoutMs"),
        ),
        (
            {"id": "n", "type": "Webhook", "data": {"name": "N", "webhook": {"retries": 4}}},
            ("Webhook", "data", "webhook", "retries"),
        ),
        (
            {"id": "n", "type": "Webhook", "data": {"name": "N", "webhook": {"method": "HEAD"}}},
            ("Webhook", "data", "webhook", "method"),
        ),
        (
            {"id": "n", "type": "Press Button", "data": {"name": "N", "pressButton": {"digits": "12x"}}},
            ("Press Button", "data", "pressButton", "digits"),
        ),
        (
            {"id": "n", "type": "Default", "data": {"name": "N", "extractVarsMode": "later"}},
            ("Default", "data", "extractVarsMode"),
        ),
    ],
)
def test_schema_bounds_are_enforced(raw: dict[str, Any], loc: tuple[str, ...]) -> None:
    assert loc in _error_locs(raw)


def test_unknown_node_type_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_node({"id": "n", "type": "Teleport", "data": {"name": "N"}})

    assert caught.value.errors()[0]["type"] == "union_tag_invalid"


def test_forwarding_node_accepts_an_id_or_a_pathway_target() -> None:
    by_id = parse_node({"id": "n", "type": "Default", "data": {"name": "N", "forwardingNode": "other"}})
    by_target = parse_node(
        {
            "id": "n",
            "type": "Default",
            "data": {"name": "N", "forwardingNode": {"pathway_id": "p1", "version": "staging"}},
        }
    )

    assert by_id.data.forwarding_node == "other"
    assert isinstance(by_target.data.forwarding_node, ForwardingTarget)
    assert by_target.data.forwarding_node.node_id is None
    assert dump_node(by_target)["data"]["forwardingNode"] == {"pathway_id": "p1", "version": "staging"}


def test_kb_and_tool_id_helpers() -> None:
    single = PathwayNodeData.model_validate(
        {"name": "N", "kb": "KB-1", "tools": ["TL-1", {"name": "inline"}]}
    )
    many = PathwayNodeData.model_validate({"name": "N", "kb": ["KB-1", "KB-2"]})

    assert single.kb_ids() == ["KB-1"]
    assert many.kb_ids() == ["KB-1", "KB-2"]
    assert PathwayNodeData.model_validate({"name": "N"}).kb_ids() == []
    assert single.tool_ids() == ["TL-1"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"transfer": {"number": "+14155550100"}, "transferNumber": "+14155550199"}, "+14155550100"),
        ({"transferNumber": "+14155550199"}, "+14155550199"),
        ({"transfer": {"number": "  "}, "transferNumber": "+14155550199"}, "+14155550199"),
        ({"transfer": {"mode": "cold"}}, None),
        ({"transferNumber": ""}, None),
    ],
)
def test_effective_transfer_number(data: dict[str, Any], expected: str | None) -> None:
    assert effective_transfer_number(PathwayNodeData.model_validate({"name": "T", **data})) == expected


def test_transfer_number_is_not_rewritten_on_load() -> None:
    raw = {"id": "t", "type": "Transfer Node", "data": {"name": "T", "transferNumber": "+14155550100"}}

    dumped = dump_node(parse_node(raw))

    assert dumped["data"] == {"name": "T", "transferNumber": "+14155550100"}


def test_edge_defaults_and_round_trip() -> None:
    edge = parse_edge({"id": "e1", "source": "a", "target": "b"})

    assert edge.data == EdgeData()
    assert (edge.data.label, edge.data.is_fallback, edge.data.always_pick, edge.data.priority) == (
        "",
        False,
        False,
        0,
    )
    assert dump_edge(edge) == {"id": "e1", "source": "a", "target": "b"}
    raw = {
        "id": "reactflow__edge-a-b",
        "source": "a",
        "target": "b",
        "type": "custom",
        "animated": True,
        "data": {
            "label": "Wants hours",
            "description": "",
            "isFallback": False,
            "alwaysPick": False,
            "priority": 2,
        },
    }
    assert dump_edge(parse_edge(raw)) == raw


def test_edge_id_is_required_and_bounded() -> None:
    for bad in ("", "x" * 257):
        with pytest.raises(ValidationError):
            parse_edge({"id": bad, "source": "a", "target": "b"})
