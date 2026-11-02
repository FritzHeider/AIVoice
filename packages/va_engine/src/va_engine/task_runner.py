"""The task-mode runner: a single-prompt Brain for the call pipeline (specs/11 §6; Decision P05-T09c).

:class:`TaskBrain` implements :class:`va_voice.pipeline.Brain`. Every model reply streams to the caller as one
:class:`~va_voice.pipeline.SpeakText`; the pipeline resumes the generator only after the caller heard it, so
tool calls are handled after the spoken part of their reply. A user turn runs at most
:data:`~va_engine.task_tools.MAX_TOOL_ROUNDS` tool rounds; then (or after two rounds with an invalid call, or
once the call made :data:`~va_engine.task_tools.MAX_TOOL_CALLS_PER_CALL` tool calls) the model is asked
without tools. ``end_call`` ends the call (``agent_end_call``), ``transfer_call`` speaks its message and hands
a :class:`~va_engine.models.results.TransferRequest` to the action handler, ``press_button`` hands a
:class:`~va_engine.models.results.PressButton` to it. Every assistant message with tool calls is followed by
exactly one ``tool`` message per call before the next request.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Final, Protocol

import orjson

from va_common.logging import get_logger
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.builtins import call_builtins
from va_engine.models.results import PressButton, TransferRequest
from va_engine.models.state import EngineState, TurnRecord
from va_engine.system_tools import SystemToolFacts, system_tools
from va_engine.task_tools import (
    EXECUTOR_CONCURRENCY,
    INTERRUPTED,
    MAX_INVALID_ROUNDS,
    MAX_TOOL_CALLS_PER_CALL,
    MAX_TOOL_ROUNDS,
    TOOL_FAILED,
    PlannedCall,
    RoundPlan,
    plan_round,
)
from va_engine.templating import (
    MissingVariables,
    Template,
    TemplateScope,
    TemplateSyntaxError,
    parse_template,
    render,
)
from va_engine.tool_executor import ToolExecutor
from va_engine.voice_inputs import voice_prompt_inputs
from va_llm.dialogue import DialogueRequest, DialogueStream
from va_llm.errors import LlmUnavailableError
from va_llm.models import ChatMessage, ToolCall, ToolDef
from va_llm.prompts import (
    DEFAULT_TOOL_SPEECH,
    INTERRUPTION_MARK,
    assemble_messages,
    dtmf_message,
    fit_voice_system,
    render_voice_system,
    tool_usage_note,
)
from va_llm.tokens import TokenCounter
from va_llm.window import ConversationWindow
from va_voice.pipeline.models import (
    BrainOutput,
    BrainTiming,
    EndCall,
    SpeakStatic,
    SpeakText,
    UserTurn,
)

__all__ = ["AGENT_END_CALL", "DEFAULT_GOODBYE", "TASK_NODE_ID", "DialogueSource", "TaskBrain"]

TASK_NODE_ID: Final = "task_root"
AGENT_END_CALL: Final = "agent_end_call"
DEFAULT_GOODBYE: Final = "Goodbye."
_NOT_TASK: Final = "TaskBrain needs a task-mode call config with a task"

_log = get_logger(__name__)


class DialogueSource(Protocol):
    """Where the runner streams replies from (:class:`va_llm.LlmGateway` satisfies it)."""

    async def stream_dialogue(self, request: DialogueRequest) -> DialogueStream: ...


class _Recorder:
    """The token stream the speaker reads: keeps every delta and swallows a pre-token
    :class:`LlmUnavailableError` (the runner speaks the error phrase instead)."""

    def __init__(self, stream: DialogueStream) -> None:
        self._stream = stream
        self._parts: list[str] = []
        self.unavailable: LlmUnavailableError | None = None

    @property
    def text(self) -> str:
        return "".join(self._parts)

    async def tokens(self) -> AsyncIterator[str]:
        try:
            async for delta in self._stream.text():
                self._parts.append(delta)
                yield delta
        except LlmUnavailableError as exc:
            self.unavailable = exc


@dataclass(slots=True)
class _RoundResult:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    unavailable: LlmUnavailableError | None = None


@dataclass(slots=True)
class _TurnBudget:
    tool_rounds: int = 0
    invalid_rounds: int = 0
    spoke: bool = False


_NOTHING: Final[tuple[BrainOutput, ...]] = ()


async def _no_outputs() -> AsyncIterator[BrainOutput]:
    for output in _NOTHING:
        yield output


def _template(text: str, field_name: str) -> Template | str:
    try:
        return parse_template(text)
    except TemplateSyntaxError as exc:
        _log.warning("template_invalid", field=field_name, position=exc.position)
        return text


class TaskBrain:
    """The Brain of a task-mode call; ``facts`` come from :func:`va_engine.system_tools.facts_for_call`."""

    def __init__(  # noqa: PLR0913 - the runner's collaborators, all keyword-only
        self,
        *,
        context: CallContext,
        settings: OrgSettings,
        gateway: DialogueSource,
        counter: TokenCounter,
        facts: SystemToolFacts,
        clock: Clock,
        executor: ToolExecutor | None = None,
        memory_context: str | None = None,
    ) -> None:
        config = context.config
        if config.runner_type != "task" or config.task is None:
            raise ValueError(_NOT_TASK)
        self._context = context
        self._config = config
        self._gateway = gateway
        self._counter = counter
        self._clock = clock
        self._executor = executor
        self._memory_context = memory_context
        if executor is None:
            facts = dataclasses.replace(
                facts, sms_capable=False, allow_schedule_callback=False, has_knowledge_bases=False
            )
        self._facts = facts
        custom = executor.definitions() if executor is not None else ()
        self._tools: tuple[ToolDef, ...] = (*system_tools(facts), *custom)
        speech = executor.tool_speech if executor is not None else None
        self._tool_note = tool_usage_note(speech or DEFAULT_TOOL_SPEECH)
        self._window = ConversationWindow(settings.llm.context_budget_tokens, counter)
        self._task = _template(config.task, "task")
        opener = config.first_sentence
        self._opener = _template(opener, "first_sentence") if opener and opener.strip() else None
        self._missing = MissingVariables(sink=self._missing_variable)
        self._started = clock.monotonic()
        self._messages: list[ChatMessage] = []
        self._state = EngineState(
            runner="task",
            pathway_id=None,
            pathway_version=None,
            current_node_id=TASK_NODE_ID,
            variables=dict(context.variables),
        )
        self._records_added = 0
        self._marks: dict[int, tuple[int, int]] = {}
        self._spoken: int | None = None
        self._spoken_record: TurnRecord | None = None
        self._last_user = ""
        self._calls_used = 0

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        """The conversation as the model sees it (before window trimming)."""
        return tuple(self._messages)

    # The Brain protocol.

    async def start(self) -> AsyncGenerator[BrainOutput]:
        if self._opener is not None:
            text = self._render(self._opener).strip()
            if text:
                yield self._say(text)
                return
        async with contextlib.aclosing(self._respond()) as outputs:
            async for output in outputs:
                yield output

    async def on_user_turn(self, turn: UserTurn) -> AsyncGenerator[BrainOutput]:
        self._state.turn_index += 1
        if turn.supersedes is not None:
            self._drop_turn(turn.supersedes)
        self._marks[turn.index] = (len(self._messages), self._records_added)
        message = dtmf_message(turn.dtmf) if turn.dtmf else ChatMessage(role="user", content=turn.text)
        self._last_user = message.content or ""
        self._append(message, at_ms=int(turn.end_ms))
        async with contextlib.aclosing(self._respond()) as outputs:
            async for output in outputs:
                yield output

    def on_no_input(self) -> AsyncIterator[BrainOutput]:
        """Nothing: the pipeline speaks its reprompt."""
        return _no_outputs()

    async def on_interrupted(self, spoken_text: str) -> None:
        """Keep only what the caller heard of the interrupted message (it already ends with ``—``)."""
        index = self._spoken
        if index is None:
            return
        text = spoken_text if spoken_text.strip() else INTERRUPTION_MARK
        self._messages[index] = self._messages[index].model_copy(update={"content": text})
        if self._spoken_record is not None:
            self._spoken_record.content = text
            self._spoken_record.interrupted = True
        self._spoken = None
        self._spoken_record = None

    def timing(self) -> BrainTiming:
        return BrainTiming()

    # One user turn.

    async def _respond(self) -> AsyncGenerator[BrainOutput]:
        budget = _TurnBudget()
        while True:
            tools = self._offer(budget)
            result = _RoundResult()
            async with contextlib.aclosing(self._stream_round(tools, result)) as speech:
                async for output in speech:
                    yield output
            if result.unavailable is not None:
                _log.warning("task_llm_unavailable", reason=result.unavailable.reason)
                yield self._say(self._config.error_phrase)
                return
            budget.spoke = budget.spoke or bool(result.text.strip())
            if not result.calls:
                return
            plan = plan_round(
                result.calls,
                self._facts,
                handles=self._handles,
                budget=MAX_TOOL_CALLS_PER_CALL - self._calls_used,
            )
            self._calls_used += plan.used
            budget.tool_rounds += 1
            budget.invalid_rounds += int(plan.invalid)
            async with contextlib.aclosing(self._run_tools(plan, budget)) as actions:
                async for output in actions:
                    yield output
            if plan.terminal is not None:
                return

    def _offer(self, budget: _TurnBudget) -> tuple[ToolDef, ...]:
        exhausted = (
            budget.tool_rounds >= MAX_TOOL_ROUNDS
            or budget.invalid_rounds >= MAX_INVALID_ROUNDS
            or self._calls_used >= MAX_TOOL_CALLS_PER_CALL
        )
        return () if exhausted else self._tools

    async def _stream_round(
        self, tools: tuple[ToolDef, ...], result: _RoundResult
    ) -> AsyncGenerator[BrainOutput]:
        async with await self._gateway.stream_dialogue(self._request(tools)) as stream:
            recorder = _Recorder(stream)
            self._spoken = None
            heard = False
            try:
                yield SpeakText(recorder.tokens())
                heard = True
            finally:
                if not heard:
                    self._keep_partial(recorder.text)
            result.unavailable = recorder.unavailable
            if recorder.unavailable is not None:
                return
            result.text = recorder.text
            result.calls = await self._round_calls(stream, tools)
        self._record_reply(result.text, result.calls)

    async def _round_calls(self, stream: DialogueStream, tools: tuple[ToolDef, ...]) -> list[ToolCall]:
        if stream.error is not None:
            _log.warning("task_llm_stream_failed", error_type=type(stream.error).__name__)
            return []
        calls = await stream.tool_calls()
        if calls and not tools:
            _log.info("task_tool_calls_ignored", count=len(calls))
            return []
        return calls

    async def _run_tools(self, plan: RoundPlan, budget: _TurnBudget) -> AsyncGenerator[BrainOutput]:
        answered: set[str] = set()
        try:
            results = await self._execute(plan.executed)
            for item in plan.calls:
                content = results.get(item.call.id, item.answer) or TOOL_FAILED
                self._answer(item.call, content, answered)
                if item.digits is not None:
                    yield PressButton(digits=item.digits)
        finally:
            for item in plan.calls:
                if item.call.id not in answered:
                    self._answer(item.call, INTERRUPTED, answered)
        terminal = plan.terminal
        if terminal is not None:
            async with contextlib.aclosing(self._finish(terminal, budget)) as outputs:
                async for output in outputs:
                    yield output

    async def _finish(self, item: PlannedCall, budget: _TurnBudget) -> AsyncGenerator[BrainOutput]:
        if item.end is not None:
            final = item.end.final_message or (None if budget.spoke else DEFAULT_GOODBYE)
            self._state.ended = True
            self._state.end_reason = AGENT_END_CALL
            if final:
                self._append(ChatMessage(role="assistant", content=final))
            yield EndCall(final_text=final, reason=AGENT_END_CALL)
            return
        if item.transfer is None or item.target is None:
            return
        message = (item.transfer.message or "").strip()
        if message:
            yield self._say(message, interruptible=False)
        yield TransferRequest(target=item.target, label=item.transfer.label, message=message or None)

    async def _execute(self, planned: tuple[PlannedCall, ...]) -> dict[str, str]:
        executor = self._executor
        if executor is None or not planned:
            return {}
        gate = asyncio.Semaphore(EXECUTOR_CONCURRENCY)
        results: dict[str, str] = {}

        async def run(call: ToolCall) -> None:
            async with gate:
                results[call.id] = await self._execute_one(executor, call)

        async with asyncio.TaskGroup() as group:
            for item in planned:
                group.create_task(run(item.call), name=f"tool-{item.call.name}")
        return results

    @staticmethod
    async def _execute_one(executor: ToolExecutor, call: ToolCall) -> str:
        try:
            return await executor.execute(call)
        except Exception as exc:
            _log.warning("task_tool_failed", tool=call.name, error_type=type(exc).__name__)
            return TOOL_FAILED

    def _handles(self, name: str) -> bool:
        return self._executor is not None and self._executor.handles(name)

    # Requests.

    def _scope(self) -> TemplateScope:
        return TemplateScope(
            variables=self._state.variables,
            request_data=self._config.request_data,
            builtins=call_builtins(self._context, now=self._clock.now(), last_user_message=self._last_user),
            timezone=self._config.timezone,
            missing=self._missing,
        )

    def _render(self, template: Template | str) -> str:
        return template if isinstance(template, str) else render(template, self._scope())

    def _request(self, tools: tuple[ToolDef, ...]) -> DialogueRequest:
        note = self._tool_note if tools else None
        extra = self._counter.text(note or "")
        if tools:
            extra += self._counter.text(orjson.dumps([tool.wire() for tool in tools]).decode())
        inputs = voice_prompt_inputs(
            self._context,
            now=self._clock.now(),
            instructions=self._render(self._task),
            variables={**self._config.request_data, **self._state.variables},
            memory_context=self._memory_context,
        )
        system = render_voice_system(fit_voice_system(inputs, counter=self._counter, extra_tokens=extra))
        window = self._window.fit(self._messages).messages
        return DialogueRequest(
            org_id=self._context.org_id,
            messages=assemble_messages(system, tool_note=note, window=window),
            tools=tools,
            model=self._config.dialogue_model,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            call_id=self._context.call_id,
            turn_index=self._state.turn_index,
        )

    # History.

    def _now_ms(self) -> int:
        return int((self._clock.monotonic() - self._started) * 1000)

    def _append(
        self, message: ChatMessage, *, at_ms: int | None = None, tool_name: str | None = None
    ) -> TurnRecord | None:
        self._messages.append(message)
        content = message.content or ""
        if message.role == "assistant" and not content:
            return None
        record = TurnRecord(
            role=message.role,
            content=content,
            node_id=TASK_NODE_ID,
            tool_call_id=message.tool_call_id,
            tool_name=tool_name,
            at_ms=self._now_ms() if at_ms is None else at_ms,
        )
        self._state.add_turn(record)
        self._records_added += 1
        return record

    def _mark_spoken(self, record: TurnRecord | None) -> None:
        self._spoken = len(self._messages) - 1
        self._spoken_record = record

    def _say(self, text: str, *, interruptible: bool = True) -> SpeakStatic:
        self._mark_spoken(self._append(ChatMessage(role="assistant", content=text)))
        return SpeakStatic(text, interruptible=interruptible)

    def _keep_partial(self, text: str) -> None:
        """The reply was cut while streaming: keep what was generated so far (barge-in trims it further)."""
        if text:
            self._mark_spoken(self._append(ChatMessage(role="assistant", content=text)))

    def _record_reply(self, text: str, calls: list[ToolCall]) -> None:
        if not text and not calls:
            return
        record = self._append(ChatMessage(role="assistant", content=text or None, tool_calls=tuple(calls)))
        if text:
            self._mark_spoken(record)

    def _answer(self, call: ToolCall, content: str, answered: set[str]) -> None:
        self._append(ChatMessage(role="tool", content=content, tool_call_id=call.id), tool_name=call.name)
        answered.add(call.id)

    def _drop_turn(self, index: int) -> None:
        """A superseding turn replaces turn ``index``: drop its user message and everything after it."""
        mark = self._marks.pop(index, None)
        if mark is None:
            return
        position, added = mark
        del self._messages[position:]
        dropped = self._records_added - added
        if 0 < dropped <= len(self._state.history):
            del self._state.history[-dropped:]
        self._records_added = added
        if self._spoken is not None and self._spoken >= position:
            self._spoken = None
            self._spoken_record = None

    def _missing_variable(self, path: str) -> None:
        _log.info("template_variable_missing", path=path, call_id=str(self._context.call_id))
