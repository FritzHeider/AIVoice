"""Request and response bodies of the pathway unit tests and runs API (specs/12-pathways-api.md §8; legacy).

Rules: specs/12 §8 Decision P07-T11b items 4, 7, and 8.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_api.routers.pathways_schemas import Name
from va_db.models import PathwayTestRun, PathwayTestRunKind, PathwayTestRunStatus, PathwayUnitTest
from va_pathways.unit_tests import MAX_VARIATIONS, Expected

__all__ = [
    "MAX_CONTENT_LENGTH",
    "MAX_GOAL_LENGTH",
    "MAX_HISTORY",
    "ExpectedBody",
    "HistoryItem",
    "RunDetail",
    "RunList",
    "RunListItem",
    "RunOne",
    "RunSummary",
    "UnitTestCreate",
    "UnitTestDeleted",
    "UnitTestFromCall",
    "UnitTestList",
    "UnitTestOne",
    "UnitTestUpdate",
    "UnitTestView",
    "run_detail",
    "run_item",
    "run_summary",
    "unit_test_view",
]

MAX_HISTORY: Final = 200
MAX_CONTENT_LENGTH: Final = 4000
MAX_GOAL_LENGTH: Final = 2000
_MAX_NODE_ID_LENGTH: Final = 200

_EXPECTED: Final[dict[str, JsonValue]] = {
    "route_to_node_id": "node_order",
    "variables": {"pizza_size": "large"},
    "grading_goal": "The agent should acknowledge the order and ask about toppings or delivery.",
}
_CREATE: Final[dict[str, JsonValue]] = {
    "name": "Routes to order",
    "node_id": "start",
    "history": [{"role": "assistant", "content": "Hi, Bella Pasta."}],
    "user_message": "Can I get a large pepperoni?",
    "expected": _EXPECTED,
    "simulated_variations": 5,
}
_TEST: Final[dict[str, JsonValue]] = {
    "test_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d80",
    "pathway_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70",
    **_CREATE,
    "source_call_id": None,
    "created_at": "2026-11-04T09:15:00Z",
    "updated_at": "2026-11-04T09:15:00Z",
}
_SUMMARY: Final[dict[str, JsonValue]] = {"total": 2, "passed": 1, "failed": 1, "error": 0}
_RUN: Final[dict[str, JsonValue]] = {
    "run_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d90",
    "pathway_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d70",
    "version_number": 3,
    "kind": "unit",
    "status": "failed",
    "summary": _SUMMARY,
    "started_at": "2026-11-04T09:20:00Z",
    "finished_at": "2026-11-04T09:20:09Z",
    "created_at": "2026-11-04T09:20:00Z",
}
_RESULT: Final[dict[str, JsonValue]] = {
    "test_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d80",
    "name": "Routes to order",
    "status": "passed",
    "passed": True,
    "cases": [],
    "duration_ms": 4120,
}

Content = Annotated[str, StringConstraints(max_length=MAX_CONTENT_LENGTH)]
UserMessage = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CONTENT_LENGTH)
]
NodeId = Annotated[str, StringConstraints(min_length=1, max_length=_MAX_NODE_ID_LENGTH)]
Variations = Annotated[int, Field(ge=0, le=MAX_VARIATIONS)]


class HistoryItem(BaseModel):
    """One message before the tested caller message."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: Content


class ExpectedBody(Expected):
    """What the test asserts (``route_to_node_id``, ``variables``, ``grading_goal``), the run's
    ``request_data``, and ``mock_tools`` answers by request source; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_EXPECTED]})

    route_to_node_id: NodeId | None = None
    grading_goal: Annotated[str, StringConstraints(max_length=MAX_GOAL_LENGTH)] | None = None

    def stored(self) -> dict[str, Any]:
        """The ``expected`` column value: exactly the keys that were sent."""
        return self.model_dump(mode="json", exclude_unset=True)


class UnitTestCreate(BaseModel):
    """``POST /v1/pathway/{pathway_id}/tests``."""

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [_CREATE]})

    name: Name = Field(description="Test name (1-200 characters).")
    node_id: NodeId = Field(description="The node the conversation is on when the caller speaks.")
    history: list[HistoryItem] = Field(
        default_factory=list, max_length=MAX_HISTORY, description="Messages before the tested one."
    )
    user_message: UserMessage = Field(description="The caller message the test sends.")
    expected: ExpectedBody = Field(default_factory=ExpectedBody, description="The assertions.")
    simulated_variations: Variations = Field(default=5, description="Paraphrases also run (0-20).")


class UnitTestUpdate(BaseModel):
    """``PATCH /v1/pathway/{pathway_id}/tests/{test_id}``: sent non-null fields replace the stored ones."""

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{"name": "Routes to order"}]})

    name: Name | None = None
    node_id: NodeId | None = None
    history: Annotated[list[HistoryItem], Field(max_length=MAX_HISTORY)] | None = None
    user_message: UserMessage | None = None
    expected: ExpectedBody | None = None
    simulated_variations: Variations | None = None

    def values(self) -> dict[str, Any]:
        """The column values to set (``null`` fields are left unchanged)."""
        values: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, ExpectedBody):
                values[name] = value.stored()
            elif name == "history":
                values[name] = [item.model_dump() for item in value]
            else:
                values[name] = value
        return values


class UnitTestFromCall(BaseModel):
    """``POST /v1/pathway/{pathway_id}/tests/from_call``."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "call_id": "0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4da0",
                    "turn_index": 3,
                    "name": "Large pepperoni",
                }
            ]
        },
    )

    call_id: uuid.UUID = Field(description="A call that ran this pathway.")
    turn_index: int = Field(ge=0, description="The caller turn to test (its `turn_index` in the transcript).")
    name: Name = Field(description="Test name (1-200 characters).")
    expected: ExpectedBody | None = Field(
        default=None, description="Assertions; default = the route the call actually took."
    )
    simulated_variations: Variations = Field(default=5, description="Paraphrases also run (0-20).")


class UnitTestView(BaseModel):
    """A pathway unit test."""

    model_config = ConfigDict(json_schema_extra={"examples": [_TEST]})

    test_id: uuid.UUID
    pathway_id: uuid.UUID
    name: str
    node_id: str
    history: list[dict[str, JsonValue]]
    user_message: str
    expected: dict[str, JsonValue]
    simulated_variations: int
    source_call_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


def unit_test_view(row: PathwayUnitTest) -> UnitTestView:
    """The response view of a unit test row."""
    return UnitTestView(
        test_id=row.id,
        pathway_id=row.pathway_id,
        name=row.name,
        node_id=row.node_id,
        history=list(row.history),
        user_message=row.user_message,
        expected=dict(row.expected),
        simulated_variations=row.simulated_variations,
        source_call_id=row.source_call_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class UnitTestOne(BaseModel):
    """One unit test (get, create, update, from_call)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "test": _TEST}]})

    status: Literal["success"] = "success"
    test: UnitTestView


class UnitTestList(BaseModel):
    """``GET /v1/pathway/{pathway_id}/tests``: newest first."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "tests": [_TEST], "next_cursor": None}]}
    )

    status: Literal["success"] = "success"
    tests: list[UnitTestView]
    next_cursor: str | None = Field(description="Cursor of the next page; null on the last page.")


class UnitTestDeleted(BaseModel):
    """``DELETE /v1/pathway/{pathway_id}/tests/{test_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "message": "Test deleted."}]}
    )

    status: Literal["success"] = "success"
    message: str


class RunSummary(BaseModel):
    """Result counts by status (``total`` also counts results without a known status)."""

    model_config = ConfigDict(json_schema_extra={"examples": [_SUMMARY]})

    total: int
    passed: int
    failed: int
    error: int


def run_summary(results: list[Any]) -> RunSummary:
    """Count ``results[*].status``."""
    counts = Counter(item.get("status") for item in results if isinstance(item, dict))
    return RunSummary(
        total=len(results), passed=counts["passed"], failed=counts["failed"], error=counts["error"]
    )


class RunListItem(BaseModel):
    """A test run without its results."""

    model_config = ConfigDict(json_schema_extra={"examples": [_RUN]})

    run_id: uuid.UUID
    pathway_id: uuid.UUID
    version_number: int
    kind: PathwayTestRunKind
    status: PathwayTestRunStatus
    summary: RunSummary
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunDetail(RunListItem):
    """A test run with its per-test (or per-scenario) results."""

    model_config = ConfigDict(json_schema_extra={"examples": [{**_RUN, "results": [_RESULT]}]})

    results: list[JsonValue]


def _run_fields(row: PathwayTestRun) -> dict[str, Any]:
    return {
        "run_id": row.id,
        "pathway_id": row.pathway_id,
        "version_number": row.version_number,
        "kind": row.kind,
        "status": row.status,
        "summary": run_summary(list(row.results)),
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }


def run_item(row: PathwayTestRun) -> RunListItem:
    """The list view of a run row."""
    return RunListItem(**_run_fields(row))


def run_detail(row: PathwayTestRun) -> RunDetail:
    """The detail view of a run row."""
    return RunDetail(**_run_fields(row), results=list(row.results))


class RunList(BaseModel):
    """``GET /v1/pathway/{pathway_id}/runs``: newest first."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "runs": [_RUN], "next_cursor": None}]}
    )

    status: Literal["success"] = "success"
    runs: list[RunListItem]
    next_cursor: str | None = Field(description="Cursor of the next page; null on the last page.")


class RunOne(BaseModel):
    """``GET /v1/pathway/{pathway_id}/runs/{run_id}``."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "run": {**_RUN, "results": [_RESULT]}}]}
    )

    status: Literal["success"] = "success"
    run: RunDetail
