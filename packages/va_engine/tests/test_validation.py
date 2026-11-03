"""Save/publish validation (specs/11-pathways-engine.md §11): one test per rule, both modes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pytest
from graph_helpers import (
    CANONICAL_FIXTURES,
    FIXTURE_REFS,
    KB_MENU,
    PW_BILLING,
    PW_SELF,
    TL_CRM,
    edge,
    fixture,
    graph,
    node,
    start,
)

from va_engine.models import PathwayGraph, load_graph
from va_engine.models.nodes import CustomCodeConfig
from va_engine.validation import (
    MAX_EDGES,
    MAX_NODES,
    MESSAGES,
    RULES,
    GraphIssue,
    GraphRefs,
    Mode,
    validate_graph,
    validation_report,
)

SPEC: Final = Path(__file__).resolve().parents[3] / "specs" / "11-pathways-engine.md"
_ROW: Final = re.compile(
    r'^\| [^|]+ \| `(?P<code>[A-Z_]+)` \| "(?P<message>[^"]+)" \| (?P<save>\w+) \| (?P<publish>\w+) \|$'
)
MODES: Final[tuple[Mode, ...]] = ("save", "publish")
SEVERITY: Final = {"warn": "warning", "error": "error"}


def _spec_rows() -> list[tuple[str, str, str, str]]:
    section = SPEC.read_text(encoding="utf-8").split("## 11. Validation", 1)[1].split("\n## 12.", 1)[0]
    rows = []
    for line in section.splitlines():
        match = _ROW.match(line)
        if match:
            rows.append(
                (match["code"], match["message"], SEVERITY[match["save"]], SEVERITY[match["publish"]])
            )
    return rows


def test_rules_and_messages_mirror_the_spec_table() -> None:
    rows = _spec_rows()

    assert len(rows) == 18
    assert [row[0] for row in rows] == list(RULES)
    for code, message, save, publish in rows:
        assert MESSAGES[code] == message
        assert RULES[code] == (save, publish)


def _issues(loaded: PathwayGraph, mode: Mode, **kwargs: Any) -> list[GraphIssue]:
    return validate_graph(loaded, mode=mode, **kwargs)


def _only(issues: list[GraphIssue], code: str) -> list[GraphIssue]:
    return [issue for issue in issues if issue.code == code]


def _expect(
    loaded: PathwayGraph,
    code: str,
    expected: Sequence[tuple[str, str | None, str | None]],
    **kwargs: Any,
) -> None:
    """Both modes report exactly ``expected`` (message, node_id, edge_id) for ``code``, table severities."""
    for index, mode in enumerate(MODES):
        found = _only(_issues(loaded, mode, **kwargs), code)
        assert [(i.message, i.node_id, i.edge_id) for i in found] == list(expected)
        assert {i.severity for i in found} == ({RULES[code][index]} if expected else set())


@pytest.mark.parametrize("name", CANONICAL_FIXTURES)
@pytest.mark.parametrize("mode", MODES)
def test_fixtures_are_clean(name: str, mode: Mode) -> None:
    raw = fixture(name)
    loaded = load_graph(raw["nodes"], raw["edges"], raw["global_config"])

    assert _issues(loaded, mode, refs=FIXTURE_REFS, code_checker=lambda _config: None) == []


@pytest.mark.parametrize("count", [0, 2])
def test_start_node(count: int) -> None:
    nodes = [node(f"n{i}", isStart=True) for i in range(count)] + [node("other")]

    _expect(
        graph(nodes),
        "PATHWAY_START_NODE",
        [(f"Pathway must have exactly one start node (found {count}).", None, None)],
    )


def test_duplicate_node_id() -> None:
    loaded = graph([start("a"), node("a", "Second"), node("a", "Third")])

    _expect(loaded, "PATHWAY_DUPLICATE_NODE_ID", [("Duplicate node id 'a'.", "a", None)] * 2)


def test_duplicate_node_name_is_case_insensitive() -> None:
    loaded = graph(
        [start("a", name="Greeting"), node("b", "GREETING"), node("c", "greeting ")], [edge("e", "a", "b")]
    )

    _expect(loaded, "PATHWAY_DUPLICATE_NODE_NAME", [("Duplicate node name 'GREETING'.", "b", None)])


def test_edge_dangling_reports_each_missing_endpoint() -> None:
    loaded = graph([start("a")], [edge("e1", "ghost", "a"), edge("e2", "a", "nowhere"), edge("e3", "x", "y")])

    _expect(
        loaded,
        "PATHWAY_EDGE_DANGLING",
        [
            ("Edge 'e1' references missing node 'ghost'.", None, "e1"),
            ("Edge 'e2' references missing node 'nowhere'.", None, "e2"),
            ("Edge 'e3' references missing node 'x'.", None, "e3"),
            ("Edge 'e3' references missing node 'y'.", None, "e3"),
        ],
    )


def test_edge_label_required_unless_fallback() -> None:
    loaded = graph(
        [start("a"), node("b"), node("c"), node("d")],
        [edge("e1", "a", "b", ""), edge("e2", "a", "c", "  "), edge("e3", "a", "d", "", isFallback=True)],
    )

    _expect(
        loaded,
        "PATHWAY_EDGE_LABEL",
        [
            ("Edge 'e1' needs a condition label.", None, "e1"),
            ("Edge 'e2' needs a condition label.", None, "e2"),
        ],
    )


@pytest.mark.parametrize(
    "out_edges",
    [
        [{"isFallback": True}, {"isFallback": True}],
        [{"alwaysPick": True}, {"alwaysPick": True}],
        [{"alwaysPick": True}, {}],
        [{"alwaysPick": True}, {"isFallback": True}],
    ],
)
def test_edge_conflict(out_edges: list[dict[str, Any]]) -> None:
    edges = [edge(f"e{i}", "a", f"t{i}", **flags) for i, flags in enumerate(out_edges)]
    targets = [node(f"t{i}") for i in range(len(out_edges))]

    _expect(
        graph([start("a", name="Hub"), *targets], edges),
        "PATHWAY_EDGE_CONFLICT",
        [("Node 'Hub' has conflicting fallback/always edges.", "a", None)],
    )


def test_single_fallback_or_always_edge_is_fine() -> None:
    loaded = graph(
        [start("a"), node("b"), node("c"), node("d")],
        [edge("e1", "a", "b"), edge("e2", "a", "c", isFallback=True), edge("e3", "b", "d", alwaysPick=True)],
    )

    _expect(loaded, "PATHWAY_EDGE_CONFLICT", [])


def test_unreachable_counts_globals_and_node_jumps() -> None:
    nodes = [
        start("a"),
        node(
            "g", isGlobal=True, globalLabel="Asks hours", enableForwarding=True, forwardingNode="from_global"
        ),
        node("from_global"),
        node(
            "hook",
            node_type="Webhook",
            webhook={
                "url": "https://a.test",
                "routeOnStatus": {"5xx": "on_error"},
                "responsePathways": [{"variable": "v", "operator": "is_empty", "targetNodeId": "on_empty"}],
            },
        ),
        node("on_error"),
        node("on_empty"),
        node(
            "wait", node_type="Wait for Response", prompt="Wait", waitForResponse={"timeoutNodeId": "timeout"}
        ),
        node("timeout"),
        node("book", node_type="Scheduling", scheduling={"onFailureNodeId": "failed"}),
        node("failed"),
        node(
            "warm",
            node_type="Transfer Call",
            transfer={"number": "+14155550100", "warm": {"onTargetNoAnswer": {"nodeId": "no_answer"}}},
        ),
        node("no_answer"),
        node("island", "Island"),
        node("island2", "Island Two"),
    ]
    edges = [
        edge("e1", "a", "hook"),
        edge("e2", "a", "wait"),
        edge("e3", "a", "book"),
        edge("e4", "a", "warm"),
        edge("e5", "island", "island2"),
        edge("e6", "a", "missing"),
    ]

    _expect(
        graph(nodes, edges),
        "PATHWAY_UNREACHABLE",
        [
            ("Node 'Island' is unreachable.", "island", None),
            ("Node 'Island Two' is unreachable.", "island2", None),
        ],
    )


def test_unreachable_is_skipped_without_exactly_one_start() -> None:
    _expect(graph([node("a"), node("b")]), "PATHWAY_UNREACHABLE", [])


def test_global_label() -> None:
    loaded = graph(
        [start("a"), node("g", "Hours", isGlobal=True), node("h", "Help", isGlobal=True, globalLabel=" ")]
    )

    _expect(
        loaded,
        "PATHWAY_GLOBAL_LABEL",
        [
            ("Global node 'Hours' needs a global label.", "g", None),
            ("Global node 'Help' needs a global label.", "h", None),
        ],
    )


@pytest.mark.parametrize("node_type", ["Default", "Knowledge Base", "Wait for Response"])
def test_node_empty(node_type: str) -> None:
    empty = {"id": "e", "type": node_type, "data": {"name": "Empty", "prompt": " ", "text": None}}
    loaded = graph(
        [start("a"), empty, node("t", node_type="End Call")], [edge("x", "a", "e"), edge("y", "a", "t")]
    )

    _expect(loaded, "PATHWAY_NODE_EMPTY", [("Node 'Empty' needs a prompt or static text.", "e", None)])


def test_node_with_static_text_is_not_empty() -> None:
    _expect(graph([start("a", prompt=None, text="Hello")]), "PATHWAY_NODE_EMPTY", [])


@pytest.mark.parametrize(
    ("webhook", "allow_http", "valid"),
    [
        (None, False, False),
        ({}, False, False),
        ({"url": "https://api.acme.test/x?phone={{from}}"}, False, True),
        ({"url": "http://localhost:9300/hook"}, False, False),
        ({"url": "http://localhost:9300/hook"}, True, True),
        ({"url": "ftp://api.acme.test/x"}, True, False),
        ({"url": "https:///nohost"}, False, False),
        ({"url": "{{base_url}}/x"}, False, False),
        ({"url": "https://{{host}}/x"}, False, False),
        ({"url": "https://[::1"}, False, False),
    ],
)
def test_webhook_url(webhook: dict[str, str] | None, allow_http: bool, valid: bool) -> None:
    data: dict[str, Any] = {} if webhook is None else {"webhook": webhook}
    loaded = graph([start("a"), node("w", "Hook", node_type="Webhook", **data)], [edge("e", "a", "w")])

    expected = [] if valid else [("Webhook node 'Hook' has an invalid URL.", "w", None)]
    _expect(loaded, "PATHWAY_WEBHOOK_URL", expected, allow_http=allow_http)


@pytest.mark.parametrize(
    ("data", "valid"),
    [
        ({}, False),
        ({"transfer": {"number": " "}}, False),
        ({"transfer": {"number": "{{manager_number}}"}}, True),
        ({"transferNumber": "+14155550100"}, True),
    ],
)
def test_transfer_target(data: dict[str, Any], valid: bool) -> None:
    loaded = graph([start("a"), node("t", "Agent", node_type="Transfer Call", **data)], [edge("e", "a", "t")])

    expected = [] if valid else [("Transfer node 'Agent' needs a destination.", "t", None)]
    _expect(loaded, "PATHWAY_TRANSFER_TARGET", expected)


def test_kb_missing_checks_nodes_then_global_config() -> None:
    loaded = graph(
        [
            start("a", node_type="Default", kb=["KB-gone", KB_MENU]),
            node("k", node_type="Knowledge Base", kb="KB-x"),
        ],
        [edge("e", "a", "k")],
        {"globalKb": "KB-global"},
    )

    _expect(
        loaded,
        "PATHWAY_KB_MISSING",
        [
            ("Knowledge base 'KB-gone' not found.", "a", None),
            ("Knowledge base 'KB-x' not found.", "k", None),
            ("Knowledge base 'KB-global' not found.", None, None),
        ],
        refs=GraphRefs(kb_ids=frozenset({KB_MENU})),
    )


def test_tool_missing_skips_inline_tools() -> None:
    loaded = graph(
        [start("a", tools=["TL-gone", TL_CRM, {"name": "inline", "url": "https://a.test"}])],
        global_config={"globalTools": ["TL-global"]},
    )

    _expect(
        loaded,
        "PATHWAY_TOOL_MISSING",
        [("Tool 'TL-gone' not found.", "a", None), ("Tool 'TL-global' not found.", None, None)],
        refs=GraphRefs(tool_ids=frozenset({TL_CRM})),
    )


def test_reference_checks_are_skipped_without_refs() -> None:
    loaded = graph(
        [
            start("a", kb="KB-1", tools=["TL-1"]),
            node("p", node_type="Transfer Pathway", transferPathway={"pathwayId": "anything"}),
        ],
        [edge("e", "a", "p")],
    )

    for code in ("PATHWAY_KB_MISSING", "PATHWAY_TOOL_MISSING", "PATHWAY_TARGET_MISSING"):
        _expect(loaded, code, [])


@pytest.mark.parametrize(
    ("data", "target"),
    [
        ({}, ""),
        ({"transferPathway": {"pathwayId": " "}}, " "),
        ({"transferPathway": {"pathwayId": "unknown"}}, "unknown"),
        ({"transferPathway": {"pathwayId": PW_SELF}}, PW_SELF),
    ],
)
def test_target_missing(data: dict[str, Any], target: str) -> None:
    loaded = graph(
        [start("a"), node("p", "Billing", node_type="Transfer Pathway", **data)], [edge("e", "a", "p")]
    )
    refs = GraphRefs(pathway_id=PW_SELF, pathway_ids=frozenset({PW_BILLING, PW_SELF}))

    _expect(
        loaded, "PATHWAY_TARGET_MISSING", [(f"Target pathway '{target}' not found.", "p", None)], refs=refs
    )


def test_target_self_with_node_id_and_known_targets_are_fine() -> None:
    loaded = graph(
        [
            start("a"),
            node("p", node_type="Transfer Pathway", transferPathway={"pathwayId": PW_SELF, "nodeId": "a"}),
            node("q", node_type="Transfer Pathway", transferPathway={"pathwayId": PW_BILLING}),
        ],
        [edge("e1", "a", "p"), edge("e2", "a", "q")],
    )
    refs = GraphRefs(pathway_id=PW_SELF, pathway_ids=frozenset({PW_BILLING, PW_SELF}))

    _expect(loaded, "PATHWAY_TARGET_MISSING", [], refs=refs)


def test_forwarding_to_another_pathway_is_a_target() -> None:
    loaded = graph([start("a", enableForwarding=True, forwardingNode={"pathway_id": "gone"})])

    _expect(
        loaded,
        "PATHWAY_TARGET_MISSING",
        [("Target pathway 'gone' not found.", "a", None)],
        refs=GraphRefs(pathway_ids=frozenset()),
    )


def test_custom_code_uses_the_checker() -> None:
    seen: list[str] = []

    def checker(config: CustomCodeConfig) -> str | None:
        seen.append(config.code)
        return "TS2304: Cannot find name 'foo'." if "foo" in config.code else None

    loaded = graph(
        [
            start("a"),
            node("bad", "Bad Code", node_type="Custom Code", customCode={"code": "foo()"}),
            node(
                "ok", "Good Code", node_type="Custom Code", customCode={"code": "export default () => ({})"}
            ),
        ],
        [edge("e1", "a", "bad"), edge("e2", "a", "ok")],
    )

    _expect(
        loaded,
        "CUSTOM_CODE_ERROR",
        [("Custom code in 'Bad Code': TS2304: Cannot find name 'foo'.", "bad", None)],
        code_checker=checker,
    )
    assert seen == ["foo()", "export default () => ({})"] * 2
    _expect(loaded, "CUSTOM_CODE_ERROR", [])


@pytest.mark.parametrize("data", [{}, {"customCode": {"code": "  "}}])
def test_custom_code_empty_is_reported_without_a_checker(data: dict[str, Any]) -> None:
    loaded = graph([start("a"), node("c", "Calc", node_type="Custom Code", **data)], [edge("e", "a", "c")])

    _expect(loaded, "CUSTOM_CODE_ERROR", [("Custom code in 'Calc': code is empty", "c", None)])


def test_template_syntax_reports_the_field_path() -> None:
    loaded = graph(
        [
            start("a", prompt="Hello {{name"),
            node(
                "w",
                "Hook",
                node_type="Webhook",
                webhook={
                    "url": "https://a.test",
                    "headers": {"Authorization": "Bearer {{ }}"},
                    "body": {"x": ["{{a|nope}}"]},
                },
            ),
            node(
                "c", "Calc", node_type="Custom Code", customCode={"code": "const s = '{{';", "speech": "{{"}
            ),
        ],
        [edge("e1", "a", "w"), edge("e2", "a", "c")],
        {"globalPrompt": "You are {{", "errorPhrase": "Sorry"},
    )

    _expect(
        loaded,
        "PATHWAY_TEMPLATE_SYNTAX",
        [
            ("Invalid template in 'a': prompt: unterminated expression at position 6", "a", None),
            (
                "Invalid template in 'Hook': webhook.headers.Authorization: empty expression at position 7",
                "w",
                None,
            ),
            ("Invalid template in 'Hook': webhook.body.x[0]: unknown filter 'nope' at position 4", "w", None),
            (
                "Invalid template in 'Calc': customCode.speech: unterminated expression at position 0",
                "c",
                None,
            ),
            (
                "Invalid template in 'global_config': globalPrompt: unterminated expression at position 8",
                None,
                None,
            ),
        ],
    )


def test_var_name_rules() -> None:
    loaded = graph(
        [
            start(
                "a",
                name="Collect",
                extractVars=[
                    {"name": "first_name"},
                    {"name": "First"},
                    {"name": "first_name"},
                    {"name": "x" * 65},
                ],
            ),
            node(
                "w",
                "Hook",
                node_type="Webhook",
                extractVars=[{"name": "status"}],
                webhook={
                    "url": "https://a.test",
                    "responseData": [{"name": "status", "data": "$.s"}, {"name": "9x", "data": "$"}],
                },
            ),
        ],
        [edge("e", "a", "w")],
    )

    _expect(
        loaded,
        "PATHWAY_VAR_NAME",
        [
            ("Invalid variable name 'First' in 'Collect'.", "a", None),
            ("Invalid variable name 'first_name' in 'Collect'.", "a", None),
            (f"Invalid variable name '{'x' * 65}' in 'Collect'.", "a", None),
            ("Invalid variable name '9x' in 'Hook'.", "w", None),
        ],
    )


def _many(count: int) -> list[dict[str, Any]]:
    return [start("n0")] + [node(f"n{i}") for i in range(1, count)]


@pytest.mark.parametrize(
    "build",
    [
        lambda: graph(_many(MAX_NODES + 1)),
        lambda: graph(_many(2), [edge(f"e{i}", "n0", "n1") for i in range(MAX_EDGES + 1)]),
        lambda: graph([start("n0", prompt="x" * 5_000_001)]),
    ],
    ids=["nodes", "edges", "bytes"],
)
def test_too_large_short_circuits(build: Any) -> None:
    loaded = build()

    for mode in MODES:
        assert _issues(loaded, mode) == [
            GraphIssue("PATHWAY_TOO_LARGE", "Pathway exceeds size limits.", "error")
        ]


def test_limits_themselves_are_allowed() -> None:
    loaded = graph(_many(MAX_NODES), [edge(f"e{i}", "n0", f"n{i % MAX_NODES}") for i in range(MAX_EDGES)])

    assert _only(_issues(loaded, "publish"), "PATHWAY_TOO_LARGE") == []


def test_issues_follow_table_order_then_array_order() -> None:
    loaded = graph(
        [
            node("b", "Same", prompt="{{"),
            node("a", "same", isGlobal=True),
            node("c", "Third", prompt=" "),
        ],
        [edge("e1", "b", "zz", "")],
    )

    codes = [issue.code for issue in _issues(loaded, "save")]

    assert codes == [
        "PATHWAY_START_NODE",
        "PATHWAY_DUPLICATE_NODE_NAME",
        "PATHWAY_EDGE_DANGLING",
        "PATHWAY_EDGE_LABEL",
        "PATHWAY_GLOBAL_LABEL",
        "PATHWAY_NODE_EMPTY",
        "PATHWAY_TEMPLATE_SYNTAX",
    ]
    assert codes == sorted(codes, key=list(RULES).index)


def test_validation_report_shape() -> None:
    warnings_only = graph([start("a"), node("island")])
    with_errors = graph([start("a"), node("a", "B")], [edge("e", "a", "ghost")])

    assert validation_report(_issues(warnings_only, "save")) == {
        "valid": True,
        "errors": [],
        "warnings": [
            {"code": "PATHWAY_UNREACHABLE", "message": "Node 'island' is unreachable.", "node_id": "island"}
        ],
    }
    report = validation_report(_issues(with_errors, "publish"))
    assert report["valid"] is False
    assert report["errors"] == [
        {"code": "PATHWAY_DUPLICATE_NODE_ID", "message": "Duplicate node id 'a'.", "node_id": "a"},
        {
            "code": "PATHWAY_EDGE_DANGLING",
            "message": "Edge 'e' references missing node 'ghost'.",
            "edge_id": "e",
        },
    ]
    assert report["warnings"] == []
    assert validation_report([]) == {"valid": True, "errors": [], "warnings": []}
