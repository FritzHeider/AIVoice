"""Save/publish validation of a pathway graph (specs/11-pathways-engine.md §11).

:func:`validate_graph` returns every :class:`GraphIssue` in §11 table row order, and within a rule in
node/edge array order; the severity of each code depends on the mode (``save`` allows warnings,
``publish`` blocks on errors). Existence checks against org data (knowledge bases, tools, pathways) run
only when :class:`GraphRefs` carries the corresponding id set, and the Custom Code ``deno check`` runs
only when a ``code_checker`` is supplied (Decision P07-T01 under specs/11 §11).
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypedDict
from urllib.parse import urlsplit

from pydantic import BaseModel

from va_engine.models.graph import PathwayGraph, canonical_json, dump_graph
from va_engine.models.nodes import (
    CustomCodeConfig,
    ForwardingTarget,
    PathwayNode,
    effective_transfer_number,
)
from va_engine.templating import TemplateSyntaxError, parse_template

__all__ = [
    "MAX_EDGES",
    "MAX_GRAPH_BYTES",
    "MAX_NODES",
    "MESSAGES",
    "RULES",
    "VAR_NAME_PATTERN",
    "CodeChecker",
    "GraphIssue",
    "GraphRefs",
    "IssueItem",
    "Mode",
    "Severity",
    "ValidationReport",
    "validate_graph",
    "validation_report",
]

Mode = Literal["save", "publish"]
Severity = Literal["error", "warning"]
CodeChecker = Callable[[CustomCodeConfig], str | None]
"""Returns a ``deno check`` diagnostic for the code, or ``None`` when it passes."""

MAX_NODES: Final = 500
MAX_EDGES: Final = 2_000
MAX_GRAPH_BYTES: Final = 5_000_000
VAR_NAME_PATTERN: Final = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")

START_NODE: Final = "PATHWAY_START_NODE"
DUPLICATE_NODE_ID: Final = "PATHWAY_DUPLICATE_NODE_ID"
DUPLICATE_NODE_NAME: Final = "PATHWAY_DUPLICATE_NODE_NAME"
EDGE_DANGLING: Final = "PATHWAY_EDGE_DANGLING"
EDGE_LABEL: Final = "PATHWAY_EDGE_LABEL"
EDGE_CONFLICT: Final = "PATHWAY_EDGE_CONFLICT"
UNREACHABLE: Final = "PATHWAY_UNREACHABLE"
GLOBAL_LABEL: Final = "PATHWAY_GLOBAL_LABEL"
NODE_EMPTY: Final = "PATHWAY_NODE_EMPTY"
WEBHOOK_URL: Final = "PATHWAY_WEBHOOK_URL"
TRANSFER_TARGET: Final = "PATHWAY_TRANSFER_TARGET"
KB_MISSING: Final = "PATHWAY_KB_MISSING"
TOOL_MISSING: Final = "PATHWAY_TOOL_MISSING"
TARGET_MISSING: Final = "PATHWAY_TARGET_MISSING"
CUSTOM_CODE_ERROR: Final = "CUSTOM_CODE_ERROR"
TEMPLATE_SYNTAX: Final = "PATHWAY_TEMPLATE_SYNTAX"
VAR_NAME: Final = "PATHWAY_VAR_NAME"
TOO_LARGE: Final = "PATHWAY_TOO_LARGE"

RULES: Final[Mapping[str, tuple[Severity, Severity]]] = {
    START_NODE: ("warning", "error"),
    DUPLICATE_NODE_ID: ("error", "error"),
    DUPLICATE_NODE_NAME: ("warning", "error"),
    EDGE_DANGLING: ("error", "error"),
    EDGE_LABEL: ("warning", "error"),
    EDGE_CONFLICT: ("warning", "error"),
    UNREACHABLE: ("warning", "warning"),
    GLOBAL_LABEL: ("warning", "error"),
    NODE_EMPTY: ("warning", "error"),
    WEBHOOK_URL: ("warning", "error"),
    TRANSFER_TARGET: ("warning", "error"),
    KB_MISSING: ("warning", "error"),
    TOOL_MISSING: ("warning", "error"),
    TARGET_MISSING: ("warning", "error"),
    CUSTOM_CODE_ERROR: ("warning", "error"),
    TEMPLATE_SYNTAX: ("error", "error"),
    VAR_NAME: ("error", "error"),
    TOO_LARGE: ("error", "error"),
}
"""Code → (save severity, publish severity), in §11 table row order."""

MESSAGES: Final[Mapping[str, str]] = {
    START_NODE: "Pathway must have exactly one start node (found {n}).",
    DUPLICATE_NODE_ID: "Duplicate node id '{id}'.",
    DUPLICATE_NODE_NAME: "Duplicate node name '{name}'.",
    EDGE_DANGLING: "Edge '{id}' references missing node '{node}'.",
    EDGE_LABEL: "Edge '{id}' needs a condition label.",
    EDGE_CONFLICT: "Node '{name}' has conflicting fallback/always edges.",
    UNREACHABLE: "Node '{name}' is unreachable.",
    GLOBAL_LABEL: "Global node '{name}' needs a global label.",
    NODE_EMPTY: "Node '{name}' needs a prompt or static text.",
    WEBHOOK_URL: "Webhook node '{name}' has an invalid URL.",
    TRANSFER_TARGET: "Transfer node '{name}' needs a destination.",
    KB_MISSING: "Knowledge base '{id}' not found.",
    TOOL_MISSING: "Tool '{id}' not found.",
    TARGET_MISSING: "Target pathway '{id}' not found.",
    CUSTOM_CODE_ERROR: "Custom code in '{name}': {diagnostic}",
    TEMPLATE_SYNTAX: "Invalid template in '{name}': {detail}",
    VAR_NAME: "Invalid variable name '{var}' in '{name}'.",
    TOO_LARGE: "Pathway exceeds size limits.",
}
"""The §11 messages, formatted with :meth:`str.format`."""

EMPTY_CODE_DIAGNOSTIC: Final = "code is empty"
GLOBAL_CONFIG_NAME: Final = "global_config"
_NEEDS_CONTENT: Final = frozenset({"Default", "Knowledge Base", "Wait for Response"})
_TEMPLATED_NODE_FIELDS: Final = ("prompt", "text", "condition", "global_label")
_TEMPLATED_CONFIGS: Final = (
    "webhook",
    "transfer",
    "end_call",
    "sms",
    "press_button",
    "scheduling",
    "wait_for_response",
    "custom_code",
)
_UNTEMPLATED_KEYS: Final = frozenset({"customCode.code"})


class IssueItem(TypedDict, total=False):
    """One entry of the validation result's ``errors``/``warnings``."""

    code: str
    message: str
    node_id: str
    edge_id: str


class ValidationReport(TypedDict):
    """``{"valid", "errors", "warnings"}`` (§11 result shape)."""

    valid: bool
    errors: list[IssueItem]
    warnings: list[IssueItem]


@dataclass(frozen=True, slots=True)
class GraphIssue:
    """One validation finding."""

    code: str
    message: str
    severity: Severity
    node_id: str | None = None
    edge_id: str | None = None

    def as_item(self) -> IssueItem:
        """The result item: ``code``, ``message`` and the ids that apply."""
        item: IssueItem = {"code": self.code, "message": self.message}
        if self.node_id is not None:
            item["node_id"] = self.node_id
        if self.edge_id is not None:
            item["edge_id"] = self.edge_id
        return item


@dataclass(frozen=True, slots=True)
class GraphRefs:
    """Ids that exist for the org; ``None`` skips that existence check."""

    pathway_id: str | None = None
    kb_ids: frozenset[str] | None = None
    tool_ids: frozenset[str] | None = None
    pathway_ids: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class _Found:
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None


def _found(code: str, *, node_id: str | None = None, edge_id: str | None = None, **values: object) -> _Found:
    return _Found(code, MESSAGES[code].format(**values), node_id, edge_id)


def validate_graph(
    graph: PathwayGraph,
    *,
    mode: Mode,
    refs: GraphRefs | None = None,
    allow_http: bool = False,
    code_checker: CodeChecker | None = None,
) -> list[GraphIssue]:
    """Every §11 issue of ``graph`` with its ``mode`` severity, in table row order then array order.

    An oversized graph yields only ``PATHWAY_TOO_LARGE``.
    """
    if _too_large(graph):
        found = [_found(TOO_LARGE)]
    else:
        checks = _Checks(graph, refs or GraphRefs(), allow_http=allow_http, code_checker=code_checker)
        found = checks.run()
    order = {code: index for index, code in enumerate(RULES)}
    found.sort(key=lambda issue: order[issue.code])
    severity = 0 if mode == "save" else 1
    return [
        GraphIssue(issue.code, issue.message, RULES[issue.code][severity], issue.node_id, issue.edge_id)
        for issue in found
    ]


def validation_report(issues: Sequence[GraphIssue]) -> ValidationReport:
    """The §11 result shape; ``valid`` is false only when an issue is an error."""
    errors = [issue.as_item() for issue in issues if issue.severity == "error"]
    warnings = [issue.as_item() for issue in issues if issue.severity == "warning"]
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _too_large(graph: PathwayGraph) -> bool:
    if len(graph.nodes) > MAX_NODES or len(graph.edges) > MAX_EDGES:
        return True
    return len(canonical_json(dump_graph(graph))) > MAX_GRAPH_BYTES


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


class _Checks:
    """One validation pass; each method appends its rule's findings in array order."""

    def __init__(
        self,
        graph: PathwayGraph,
        refs: GraphRefs,
        *,
        allow_http: bool,
        code_checker: CodeChecker | None,
    ) -> None:
        self.graph = graph
        self.refs = refs
        self.allow_http = allow_http
        self.code_checker = code_checker
        self.by_id: dict[str, PathwayNode] = {}
        for node in graph.nodes:
            self.by_id.setdefault(node.id, node)
        self.found: list[_Found] = []

    def run(self) -> list[_Found]:
        self._start_node()
        self._duplicates()
        self._edges()
        self._unreachable()
        for node in self.graph.nodes:
            self._node_content(node)
            self._references(node)
            self._custom_code(node)
            self._templates(node)
            self._var_names(node)
        self._global_config()
        return self.found

    def _add(self, issue: _Found) -> None:
        self.found.append(issue)

    def _start_node(self) -> None:
        count = len(self.graph.start_nodes())
        if count != 1:
            self._add(_found(START_NODE, n=count))

    def _duplicates(self) -> None:
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for node in self.graph.nodes:
            if node.id in seen_ids:
                self._add(_found(DUPLICATE_NODE_ID, node_id=node.id, id=node.id))
            seen_ids.add(node.id)
            key = node.data.name.casefold()
            if key in seen_names:
                self._add(_found(DUPLICATE_NODE_NAME, node_id=node.id, name=node.data.name))
            seen_names.add(key)

    def _edges(self) -> None:
        for edge in self.graph.edges:
            for endpoint in (edge.source, edge.target):
                if endpoint not in self.by_id:
                    self._add(_found(EDGE_DANGLING, edge_id=edge.id, id=edge.id, node=endpoint))
            if not edge.data.is_fallback and _blank(edge.data.label):
                self._add(_found(EDGE_LABEL, edge_id=edge.id, id=edge.id))
        for node in self.graph.nodes:
            if self.by_id[node.id] is not node:
                continue
            out = self.graph.out_edges(node.id)
            fallbacks = sum(edge.data.is_fallback for edge in out)
            always = sum(edge.data.always_pick for edge in out)
            if fallbacks > 1 or always > 1 or (always and len(out) > always):
                self._add(_found(EDGE_CONFLICT, node_id=node.id, name=node.data.name))

    def _unreachable(self) -> None:
        starts = self.graph.start_nodes()
        if len(starts) != 1:
            return
        seeds = [starts[0].id, *(node.id for node in self.graph.global_nodes())]
        reached: set[str] = set(seeds)
        queue = deque(seeds)
        successors: dict[str, list[str]] = {}
        for edge in self.graph.edges:
            successors.setdefault(edge.source, []).append(edge.target)
        while queue:
            current = queue.popleft()
            node = self.by_id.get(current)
            jumps = _node_jumps(node) if node is not None else []
            for target in [*successors.get(current, []), *jumps]:
                if target in self.by_id and target not in reached:
                    reached.add(target)
                    queue.append(target)
        for node in self.graph.nodes:
            if node.id not in reached and not node.data.is_global:
                self._add(_found(UNREACHABLE, node_id=node.id, name=node.data.name))

    def _node_content(self, node: PathwayNode) -> None:
        data = node.data
        name = data.name
        if data.is_global and _blank(data.global_label):
            self._add(_found(GLOBAL_LABEL, node_id=node.id, name=name))
        if node.type in _NEEDS_CONTENT and _blank(data.prompt) and _blank(data.text):
            self._add(_found(NODE_EMPTY, node_id=node.id, name=name))
        if node.type == "Webhook" and not self._webhook_url_ok(data.webhook.url if data.webhook else None):
            self._add(_found(WEBHOOK_URL, node_id=node.id, name=name))
        if node.type == "Transfer Call" and effective_transfer_number(data) is None:
            self._add(_found(TRANSFER_TARGET, node_id=node.id, name=name))

    def _webhook_url_ok(self, url: str | None) -> bool:
        if url is None or not url.strip():
            return False
        try:
            parts = urlsplit(url.strip())
            hostname = parts.hostname
        except ValueError:
            return False
        allowed = {"https", "http"} if self.allow_http else {"https"}
        if parts.scheme.lower() not in allowed or not hostname:
            return False
        return "{{" not in parts.netloc and "}}" not in parts.netloc

    def _references(self, node: PathwayNode) -> None:
        data = node.data
        self._kbs(data.kb_ids(), node.id)
        self._tools(data.tool_ids(), node.id)
        target = data.transfer_pathway
        if node.type == "Transfer Pathway":
            pathway_id = target.pathway_id if target else ""
            self._target(pathway_id, target.node_id if target else None, node.id)
        if isinstance(data.forwarding_node, ForwardingTarget):
            self._target(data.forwarding_node.pathway_id, data.forwarding_node.node_id, node.id)

    def _kbs(self, ids: list[str], node_id: str | None) -> None:
        known = self.refs.kb_ids
        if known is None:
            return
        for kb_id in ids:
            if kb_id not in known:
                self._add(_found(KB_MISSING, node_id=node_id, id=kb_id))

    def _tools(self, ids: list[str], node_id: str | None) -> None:
        known = self.refs.tool_ids
        if known is None:
            return
        for tool_id in ids:
            if tool_id not in known:
                self._add(_found(TOOL_MISSING, node_id=node_id, id=tool_id))

    def _target(self, pathway_id: str, target_node: str | None, node_id: str) -> None:
        blank = _blank(pathway_id)
        unknown = self.refs.pathway_ids is not None and pathway_id not in self.refs.pathway_ids
        recursive = (
            self.refs.pathway_id is not None and pathway_id == self.refs.pathway_id and target_node is None
        )
        if blank or unknown or recursive:
            self._add(_found(TARGET_MISSING, node_id=node_id, id=pathway_id))

    def _custom_code(self, node: PathwayNode) -> None:
        if node.type != "Custom Code":
            return
        config = node.data.custom_code
        if config is None or _blank(config.code):
            diagnostic: str | None = EMPTY_CODE_DIAGNOSTIC
        elif self.code_checker is None:
            return
        else:
            diagnostic = self.code_checker(config)
        if diagnostic is not None:
            self._add(_found(CUSTOM_CODE_ERROR, node_id=node.id, name=node.data.name, diagnostic=diagnostic))

    def _templates(self, node: PathwayNode) -> None:
        data = node.data
        fields: list[tuple[str, object]] = [
            (_alias(data, field), getattr(data, field)) for field in _TEMPLATED_NODE_FIELDS
        ]
        fields.extend((_alias(data, field), getattr(data, field)) for field in _TEMPLATED_CONFIGS)
        for path, value in fields:
            for leaf_path, text in _string_leaves(path, value):
                self._template(leaf_path, text, data.name, node.id)

    def _template(self, path: str, text: str, name: str, node_id: str | None) -> None:
        if path in _UNTEMPLATED_KEYS:
            return
        try:
            parse_template(text)
        except TemplateSyntaxError as error:
            self._add(_found(TEMPLATE_SYNTAX, node_id=node_id, name=name, detail=f"{path}: {error}"))

    def _var_names(self, node: PathwayNode) -> None:
        data = node.data
        groups = [[var.name for var in data.extract_vars]]
        if data.webhook is not None:
            groups.append([item.name for item in data.webhook.response_data])
        for names in groups:
            counts: Counter[str] = Counter()
            for var in names:
                counts[var] += 1
                if not VAR_NAME_PATTERN.fullmatch(var) or counts[var] > 1:
                    self._add(_found(VAR_NAME, node_id=node.id, var=var, name=data.name))

    def _global_config(self) -> None:
        config = self.graph.global_config
        self._kbs(config.kb_ids(), None)
        self._tools(config.tool_ids(), None)
        for field in ("global_prompt", "error_phrase"):
            value = getattr(config, field)
            if isinstance(value, str):
                self._template(_alias(config, field), value, GLOBAL_CONFIG_NAME, None)


def _alias(model: BaseModel, field: str) -> str:
    return type(model).model_fields[field].alias or field


def _string_leaves(path: str, value: object) -> Iterator[tuple[str, str]]:
    """Every string in a declared config field, with its JSON path (``webhook.headers.Authorization``)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, BaseModel):
        for field in type(value).model_fields:
            yield from _string_leaves(f"{path}.{_alias(value, field)}", getattr(value, field))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _string_leaves(f"{path}.{key}", item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _string_leaves(f"{path}[{index}]", item)


def _node_jumps(node: PathwayNode) -> list[str]:
    """Node ids a node can move to without an edge (forwarding, webhook routes, timeouts, failures)."""
    data = node.data
    jumps: list[str] = []
    if isinstance(data.forwarding_node, str):
        jumps.append(data.forwarding_node)
    if data.webhook is not None:
        if data.webhook.route_on_status is not None:
            jumps.extend(data.webhook.route_on_status.targets())
        jumps.extend(route.target_node_id for route in data.webhook.response_pathways)
    if data.wait_for_response is not None and data.wait_for_response.timeout_node_id:
        jumps.append(data.wait_for_response.timeout_node_id)
    if data.scheduling is not None and data.scheduling.on_failure_node_id:
        jumps.append(data.scheduling.on_failure_node_id)
    warm = data.transfer.warm if data.transfer else None
    if warm is not None and warm.on_target_no_answer.node_id:
        jumps.append(warm.on_target_no_answer.node_id)
    return jumps
