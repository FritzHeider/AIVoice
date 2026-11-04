"""Pathway simulations: an LLM plays the caller (specs/12-pathways-api.md §9; Decision P07-T11d there).

One simulation is one conversation of a scenario. The engine starts fresh at the start node with the
scenario's request data and speaks first; then the caller model (analysis call type, temperature 0.7) answers
as the scenario's persona and every caller line runs as one ``handle_user_turn`` through a
:class:`~va_pathways.conversation.TextConversation` (side effects simulated as in the test chat, webhooks
real). The conversation ends when the caller says ``[HANGUP]`` (the words before it are its last line, never
sent to the engine), when the agent ends the conversation (End Call, transfer), or after the agent answered
the scenario's ``max_turns``-th caller line. A conversation that ran without error is graded against the
scenario's goal with its final variables. Nothing raises: engine failures, the turn cap, an unavailable or
blank caller and an unavailable grader end the simulation with ``status: "error"`` and an ``error`` reason.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import JsonValue

from va_common.ids import new_id
from va_common.logging import get_logger
from va_domain.org import OrgSettings
from va_domain.pathway_tests import SimulationJob
from va_engine.engine import PathwayEngine, new_pathway_state
from va_engine.models import EngineState
from va_engine.models.results import Action
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_llm import ChatMessage, LlmUnavailableError
from va_llm.tokens import TokenCounter
from va_pathways.conversation import TextConversation, TextDeps, TextSetup
from va_pathways.grading import GradeVerdict, TranscriptLine, grade_conversation
from va_pathways.recording import Item
from va_pathways.unit_tests import CASE_TIMEOUT_S, TestStatus
from va_voice.pipeline.models import UserTurn

__all__ = [
    "CALLER_MAX_TOKENS",
    "CALLER_SUFFIX",
    "CALLER_TEMPERATURE",
    "HANGUP_MARKER",
    "SILENCE",
    "CallerReply",
    "EndedBy",
    "SimulationError",
    "SimulationResult",
    "caller_messages",
    "error_result",
    "parse_caller_reply",
    "run_simulation",
    "simulation_error_item",
]

CALLER_SUFFIX: Final = (
    'Reply only with what you would say on the phone. Say "[HANGUP]" when you would hang up.'
)
HANGUP_MARKER: Final = "[HANGUP]"
SILENCE: Final = "(silence)"
"""What the caller model sees for an agent turn that said nothing."""
CALLER_TEMPERATURE: Final = 0.7
CALLER_MAX_TOKENS: Final = 256

type EndedBy = Literal["hangup", "max_turns", "agent_ended", "error"]
type SimulationError = Literal[
    "engine_error",
    "turn_timeout",
    "caller_unavailable",
    "caller_invalid",
    "grading_unavailable",
    "version_unavailable",
    "runner_error",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CallerReply:
    """The caller's line (stripped) and whether the caller hung up after it."""

    text: str
    hangup: bool


def parse_caller_reply(content: str | None) -> CallerReply:
    """``[HANGUP]`` anywhere (case-sensitive) hangs up; the text before it is the last line."""
    text = content or ""
    index = text.find(HANGUP_MARKER)
    if index >= 0:
        return CallerReply(text[:index].strip(), hangup=True)
    return CallerReply(text.strip(), hangup=False)


def caller_messages(persona: str, transcript: Sequence[TranscriptLine]) -> list[ChatMessage]:
    """The caller model's view: the persona plus :data:`CALLER_SUFFIX` as the system message, the agent's
    lines as ``user`` messages (:data:`SILENCE` when it said nothing), the caller's own as ``assistant``."""
    messages = [ChatMessage(role="system", content=f"{persona}\n\n{CALLER_SUFFIX}")]
    for role, text in transcript:
        if role == "assistant":
            messages.append(ChatMessage(role="user", content=text or SILENCE))
        else:
            messages.append(ChatMessage(role="assistant", content=text))
    return messages


def _transcript_items(transcript: Sequence[TranscriptLine]) -> list[JsonValue]:
    return [{"role": role, "content": text} for role, text in transcript if text]


def _identity(job: SimulationJob) -> Item:
    return {
        "simulation_id": job.simulation_id,
        "scenario": job.scenario.name,
        "run": job.run_index,
        "goal": job.scenario.goal,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class SimulationResult:
    """One conversation's outcome. ``transcript`` holds ``(role, text)`` with blank agent turns kept as
    ``""`` (they are omitted from the item)."""

    job: SimulationJob
    status: TestStatus
    ended_by: EndedBy
    turns: int
    transcript: list[TranscriptLine]
    variables: dict[str, JsonValue]
    grading: GradeVerdict | None
    actions: list[Item]
    duration_ms: int
    error: SimulationError | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def item(self) -> Item:
        """The result item (Decision P07-T11d item 5)."""
        item: Item = {
            **_identity(self.job),
            "passed": self.passed,
            "status": self.status,
            "ended_by": self.ended_by,
            "turns": self.turns,
            "transcript": _transcript_items(self.transcript),
            "variables": dict(self.variables),
            "grading": None if self.grading is None else dict(self.grading.item()),
            "actions": list(self.actions),
            "duration_ms": self.duration_ms,
        }
        if self.error is not None:
            item["error"] = self.error
        return item


def error_result(job: SimulationJob, error: SimulationError) -> SimulationResult:
    """The outcome of a simulation that could not run (no transcript, nothing graded)."""
    return SimulationResult(
        job=job,
        status="error",
        ended_by="error",
        turns=0,
        transcript=[],
        variables={},
        grading=None,
        actions=[],
        duration_ms=0,
        error=error,
    )


def simulation_error_item(job: SimulationJob, error: SimulationError) -> Item:
    """The result item of a simulation that could not run."""
    return error_result(job, error).item()


type _Step = Callable[[PathwayEngine], AsyncGenerator[Action]]


@dataclass(slots=True, kw_only=True)
class _Simulation:
    """The carried state of one conversation across its engine runs."""

    deps: TextDeps
    job: SimulationJob
    setup: TextSetup
    counter: TokenCounter
    state: EngineState
    started: float
    turns: int = 0
    transcript: list[TranscriptLine] = field(default_factory=list)
    actions: list[Item] = field(default_factory=list)

    def elapsed_ms(self) -> int:
        return round((self.deps.clock.monotonic() - self.started) * 1000)

    async def agent(self, step: _Step) -> SimulationError | None:
        """One engine run over the carried state; the agent's lines join the transcript as one turn."""
        conversation = TextConversation(self.deps, self.setup, self.state, counter=self.counter)
        try:
            finished = await conversation.run(step(conversation.engine), timeout_s=CASE_TIMEOUT_S)
        except Exception as exc:  # any engine failure ends this simulation, never the run
            _log.warning("pathway_simulation.engine_error", error=type(exc).__name__)
            return "engine_error"
        finally:
            self.state = conversation.state
            self.actions.extend(conversation.recorder.actions)
        self.transcript.append(("assistant", conversation.reply()))
        if not finished:
            _log.warning("pathway_simulation.turn_timeout", turns=self.turns)
            return "turn_timeout"
        return None

    async def caller(self) -> CallerReply | SimulationError:
        try:
            completion = await self.deps.gateway.complete(
                "analysis",
                org_id=self.setup.org_id,
                messages=caller_messages(self.job.scenario.persona, self.transcript),
                temperature=CALLER_TEMPERATURE,
                max_tokens=CALLER_MAX_TOKENS,
                call_id=self.setup.conversation_id,
            )
        except LlmUnavailableError as exc:
            _log.warning("pathway_simulation.caller_unavailable", reason=exc.reason)
            return "caller_unavailable"
        return parse_caller_reply(completion.content)

    def user_turn(self, text: str) -> _Step:
        at_ms = self.elapsed_ms()
        turn = UserTurn(index=self.state.turn_index + 1, text=text, start_ms=at_ms, end_ms=at_ms)
        return lambda engine: engine.handle_user_turn(turn)

    async def converse(self) -> tuple[EndedBy, SimulationError | None]:
        """The agent opens, then caller and agent take turns until one of the ends."""
        error = await self.agent(lambda engine: engine.start())
        while error is None:
            if self.state.ended:
                return "agent_ended", None
            if self.turns >= self.job.scenario.max_turns:
                return "max_turns", None
            reply = await self.caller()
            if isinstance(reply, str):
                return "error", reply
            if reply.hangup:
                if reply.text:
                    self.transcript.append(("user", reply.text))
                return "hangup", None
            if not reply.text:
                return "error", "caller_invalid"
            self.turns += 1
            self.transcript.append(("user", reply.text))
            error = await self.agent(self.user_turn(reply.text))
        return "error", error


async def _graded(sim: _Simulation, ended_by: EndedBy, error: SimulationError | None) -> SimulationResult:
    verdict: GradeVerdict | None = None
    variables = dict(sim.state.variables)
    if error is None:
        verdict = await grade_conversation(
            sim.deps.gateway,
            org_id=sim.setup.org_id,
            transcript=[(role, text) for role, text in sim.transcript if text],
            variables=variables,
            goal=sim.job.scenario.goal,
            call_id=sim.setup.conversation_id,
        )
        if verdict is None:
            error = "grading_unavailable"
    status: TestStatus = "error" if verdict is None else "passed" if verdict.pass_ else "failed"
    return SimulationResult(
        job=sim.job,
        status=status,
        ended_by=ended_by,
        turns=sim.turns,
        transcript=list(sim.transcript),
        variables=variables,
        grading=verdict,
        actions=list(sim.actions),
        duration_ms=sim.elapsed_ms(),
        error=error,
    )


async def run_simulation(  # noqa: PLR0913 - the job, its version, the org, and the run's dependencies
    deps: TextDeps,
    job: SimulationJob,
    *,
    org_id: uuid.UUID,
    active: ActiveGraph,
    org: OrgSettings,
    loader: PathwayLoader,
    counter: TokenCounter,
) -> SimulationResult:
    """Run one conversation of ``job`` against ``active`` and grade it (never raises for a failing pathway or
    model); a version without a start node gives ``version_unavailable``."""
    try:
        state = new_pathway_state(active)
    except ValueError:
        _log.info("pathway_simulation.no_start_node", simulation_id=job.simulation_id)
        return error_result(job, "version_unavailable")
    setup = TextSetup(
        conversation_id=new_id(),
        org_id=org_id,
        active=active,
        org=org,
        loader=loader,
        request_data=job.scenario.request_data,
    )
    sim = _Simulation(
        deps=deps, job=job, setup=setup, counter=counter, state=state, started=deps.clock.monotonic()
    )
    ended_by, error = await sim.converse()
    result = await _graded(sim, ended_by, error)
    _log.info(
        "pathway_simulation.finished",
        simulation_id=job.simulation_id,
        status=result.status,
        ended_by=ended_by,
        turns=sim.turns,
        duration_ms=result.duration_ms,
    )
    return result
