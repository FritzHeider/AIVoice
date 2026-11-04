"""The pathway unit-test case runner (specs/12 §8; Decision P07-T11a there).

A test runs its caller message (case 0) and its simulated variations (cases 1…n) one after another. Each case
starts an engine at the test's node with the test's history (:func:`case_state`), runs ONE
``handle_user_turn`` through a :class:`~va_pathways.conversation.TextConversation` (side effects simulated as
in the test chat, webhooks real unless ``expected.mock_tools`` answers their source), and checks the route,
the variables and, with a grading goal, the analysis model's verdict. Nothing raises: an engine failure, the
turn cap, an unavailable grader, a bad history role or a node missing from the version make the case fail
with an ``error`` reason, and the test's ``status`` tells assertion failures (``failed``) from runs that
could not be judged (``error``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_common.ids import new_id
from va_common.logging import get_logger
from va_domain.org import OrgSettings
from va_engine.models import EngineState, TurnRecord
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_llm.tokens import TokenCounter
from va_pathways.conversation import TextConversation, TextDeps, TextSetup
from va_pathways.grading import GradeVerdict, TranscriptLine, grade
from va_pathways.recording import Item, MockResponse
from va_pathways.variations import Variations, paraphrases
from va_voice.pipeline.models import UserTurn

__all__ = [
    "CASE_TIMEOUT_S",
    "MAX_VARIATIONS",
    "CaseError",
    "CaseResult",
    "Expected",
    "HistoryMessage",
    "InvalidHistoryError",
    "TestStatus",
    "UnitTestResult",
    "UnitTestSpec",
    "case_state",
    "route_matches",
    "run_unit_test",
    "values_match",
    "variables_match",
]

CASE_TIMEOUT_S: Final = 60.0
MAX_VARIATIONS: Final = 20
HISTORY_ROLES: Final = frozenset({"user", "assistant"})

type TestStatus = Literal["passed", "failed", "error"]
type CaseError = Literal[
    "invalid_history", "unknown_node", "engine_error", "turn_timeout", "grading_unavailable"
]

_log = get_logger(__name__)


class HistoryMessage(BaseModel):
    """One message before the tested turn (``user`` or ``assistant``; other roles fail the case)."""

    role: str
    content: str


class Expected(BaseModel):
    """What the test asserts, the request data the run sees, and canned answers by request source."""

    route_to_node_id: str | None = None
    variables: dict[str, JsonValue] | None = None
    grading_goal: str | None = None
    request_data: dict[str, JsonValue] = Field(default_factory=dict)
    mock_tools: dict[str, MockResponse] = Field(default_factory=dict)


class UnitTestSpec(BaseModel):
    """A stored unit test (the create body of specs/12 §8)."""

    model_config = ConfigDict(frozen=True)

    name: str
    node_id: str
    history: list[HistoryMessage] = Field(default_factory=list)
    user_message: str = Field(min_length=1)
    expected: Expected = Field(default_factory=Expected)
    simulated_variations: int = Field(default=0, ge=0, le=MAX_VARIATIONS)


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseResult:
    """One input's run. ``assertion_failed`` is true when an assertion that could be judged did not hold."""

    input: str
    passed: bool
    route: str | None
    variables: dict[str, JsonValue]
    reply: str
    grading: GradeVerdict | None = None
    actions: list[Item] = field(default_factory=list)
    error: CaseError | None = None
    assertion_failed: bool = False

    def item(self) -> Item:
        """The result's ``cases`` item (specs/12 §8, plus ``actions`` and ``error`` when set)."""
        item: Item = {
            "input": self.input,
            "passed": self.passed,
            "route": self.route,
            "variables": dict(self.variables),
            "reply": self.reply,
            "grading": None if self.grading is None else dict(self.grading.item()),
            "actions": list(self.actions),
        }
        if self.error is not None:
            item["error"] = self.error
        return item


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitTestResult:
    """One test's outcome."""

    test_id: str
    name: str
    status: TestStatus
    cases: list[CaseResult]
    duration_ms: int
    error: str | None = None
    variations_short: int = 0

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def item(self) -> Item:
        """The result item of specs/12 §8 plus ``status`` (and ``error`` / ``variations_short`` when set)."""
        item: Item = {
            "test_id": self.test_id,
            "name": self.name,
            "passed": self.passed,
            "status": self.status,
            "cases": [case.item() for case in self.cases],
            "duration_ms": self.duration_ms,
        }
        if self.error is not None:
            item["error"] = self.error
        if self.variations_short:
            item["variations_short"] = self.variations_short
        return item


class InvalidHistoryError(ValueError):
    """A history message whose role is not ``user`` or ``assistant``."""

    def __init__(self, role: str) -> None:
        super().__init__(f"history role {role!r} is not user or assistant")
        self.role = role


def case_state(spec: UnitTestSpec, active: ActiveGraph) -> EngineState:
    """The engine state at the test's node: its first visit, the history on that node (``at_ms`` 0, 1, …),
    and ``turn_index`` = the number of caller messages so far."""
    for message in spec.history:
        if message.role not in HISTORY_ROLES:
            raise InvalidHistoryError(message.role)
    history = [
        TurnRecord(
            role="user" if message.role == "user" else "assistant",
            content=message.content,
            node_id=spec.node_id,
            at_ms=index,
        )
        for index, message in enumerate(spec.history)
    ]
    return EngineState(
        runner="pathway",
        pathway_id=active.pathway_id,
        pathway_version=active.version,
        current_node_id=spec.node_id,
        visit_counts={spec.node_id: 1},
        history=history,
        turn_index=sum(1 for message in spec.history if message.role == "user"),
    )


def route_matches(expected: str | None, current_node_id: str) -> bool:
    """No expectation, or the node the turn left the conversation on."""
    return expected is None or expected == current_node_id


def values_match(expected: JsonValue, actual: JsonValue) -> bool:
    """Strings equal after strip + casefold, objects by :func:`variables_match` (subset), lists item by item,
    everything else by equality (a boolean never equals a number)."""
    if isinstance(expected, str) or isinstance(actual, str):
        return (
            isinstance(expected, str)
            and isinstance(actual, str)
            and expected.strip().casefold() == actual.strip().casefold()
        )
    if isinstance(expected, dict) or isinstance(actual, dict):
        return isinstance(expected, dict) and isinstance(actual, dict) and variables_match(expected, actual)
    if isinstance(expected, list) or isinstance(actual, list):
        return (
            isinstance(expected, list)
            and isinstance(actual, list)
            and len(expected) == len(actual)
            and all(values_match(want, got) for want, got in zip(expected, actual, strict=True))
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual


def variables_match(expected: Mapping[str, JsonValue] | None, actual: Mapping[str, JsonValue]) -> bool:
    """Every expected key exists in ``actual`` with a matching value (:func:`values_match`)."""
    if expected is None:
        return True
    return all(name in actual and values_match(value, actual[name]) for name, value in expected.items())


@dataclass(frozen=True, slots=True, kw_only=True)
class _Run:
    deps: TextDeps
    spec: UnitTestSpec
    org_id: uuid.UUID
    active: ActiveGraph
    org: OrgSettings
    loader: PathwayLoader
    counter: TokenCounter


def _failed_case(text: str, error: CaseError, node_id: str | None) -> CaseResult:
    return CaseResult(input=text, passed=False, route=node_id, variables={}, reply="", error=error)


async def _converse(run: _Run, state: EngineState, text: str) -> tuple[TextConversation, CaseError | None]:
    spec = run.spec
    setup = TextSetup(
        conversation_id=new_id(),
        org_id=run.org_id,
        active=run.active,
        org=run.org,
        loader=run.loader,
        request_data=spec.expected.request_data,
        mocks=spec.expected.mock_tools,
    )
    conversation = TextConversation(run.deps, setup, state, counter=run.counter)
    at_ms = len(spec.history)
    turn = UserTurn(index=state.turn_index + 1, text=text, start_ms=at_ms, end_ms=at_ms)
    try:
        finished = await conversation.run(
            conversation.engine.handle_user_turn(turn), timeout_s=CASE_TIMEOUT_S
        )
    except Exception as exc:  # any engine failure fails this case, never the run
        _log.warning("pathway_unit_test.engine_error", node_id=spec.node_id, error=type(exc).__name__)
        return conversation, "engine_error"
    if not finished:
        _log.warning("pathway_unit_test.turn_timeout", node_id=spec.node_id)
        return conversation, "turn_timeout"
    return conversation, None


def _transcript(spec: UnitTestSpec, text: str, reply: str) -> list[TranscriptLine]:
    lines: list[TranscriptLine] = [(message.role, message.content) for message in spec.history]
    lines.append(("user", text))
    lines.append(("assistant", reply))
    return lines


async def _run_case(run: _Run, text: str) -> CaseResult:
    spec = run.spec
    try:
        state = case_state(spec, run.active)
    except InvalidHistoryError:
        return _failed_case(text, "invalid_history", None)
    if run.active.graph.node(spec.node_id) is None:
        return _failed_case(text, "unknown_node", None)
    conversation, error = await _converse(run, state, text)
    final = conversation.state
    reply = conversation.reply()
    variables = dict(final.variables)
    expected = spec.expected
    held = route_matches(expected.route_to_node_id, final.current_node_id) and variables_match(
        expected.variables, variables
    )
    verdict: GradeVerdict | None = None
    if error is None and expected.grading_goal is not None:
        verdict = await grade(
            run.deps.gateway,
            org_id=run.org_id,
            transcript=_transcript(spec, text, reply),
            goal=expected.grading_goal,
        )
        if verdict is None:
            error = "grading_unavailable"
    graded = verdict is None or verdict.pass_
    judged = error is None or error == "grading_unavailable"
    return CaseResult(
        input=text,
        passed=error is None and held and graded,
        route=final.current_node_id,
        variables=variables,
        reply=reply,
        grading=verdict,
        actions=list(conversation.recorder.actions),
        error=error,
        assertion_failed=judged and not (held and graded),
    )


def _status(cases: list[CaseResult], error: str | None) -> TestStatus:
    if any(case.assertion_failed for case in cases):
        return "failed"
    if error is not None or any(case.error is not None for case in cases):
        return "error"
    return "passed"


async def run_unit_test(  # noqa: PLR0913 - the test, its version, the org, and the run's dependencies
    deps: TextDeps,
    spec: UnitTestSpec,
    *,
    test_id: str,
    org_id: uuid.UUID,
    active: ActiveGraph,
    org: OrgSettings,
    loader: PathwayLoader,
    counter: TokenCounter,
) -> UnitTestResult:
    """Run every case of ``spec`` against ``active`` (never raises for a failing pathway or model)."""
    started = deps.clock.monotonic()
    variations = (
        await paraphrases(deps.gateway, org_id=org_id, message=spec.user_message, n=spec.simulated_variations)
        if spec.simulated_variations
        else Variations((), 0)
    )
    run = _Run(deps=deps, spec=spec, org_id=org_id, active=active, org=org, loader=loader, counter=counter)
    cases = [await _run_case(run, text) for text in (spec.user_message, *variations.texts)]
    status = _status(cases, variations.error)
    duration_ms = round((deps.clock.monotonic() - started) * 1000)
    _log.info(
        "pathway_unit_test.finished",
        test_id=test_id,
        status=status,
        cases=len(cases),
        duration_ms=duration_ms,
    )
    return UnitTestResult(
        test_id=test_id,
        name=spec.name,
        status=status,
        cases=cases,
        duration_ms=duration_ms,
        error=variations.error,
        variations_short=0 if variations.error is not None else variations.short,
    )
