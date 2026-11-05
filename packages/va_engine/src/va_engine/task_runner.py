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

Executor tools run as tasks (specs/13 §5, Decision P08-T02b1): while they run the caller hears the rendered
``speech`` of the first executed tool that has one, or, when none has and the tools still run after
:data:`~va_engine.task_tools.FILLER_DELAY_S`, one filler of :data:`~va_voice.phrase_cache.TOOL_FILLERS`
(round-robin per call); ``engine.tool_fillers=false`` silences both. Such lines are transcript records only,
never model messages. The variables a round extracted are merged into the state in call order and reported
to ``on_state``. A barge-in closes the round: unfinished tool tasks are cancelled and answered
``interrupted``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final

import orjson

from va_common.logging import get_logger
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.builtins import call_builtins
from va_engine.dialogue import DialogueSource
from va_engine.models.results import PressButton, TransferRequest
from va_engine.models.state import EngineState, TurnRecord
from va_engine.opener import parse_field
from va_engine.system_tools import SystemToolFacts, system_tools
from va_engine.task_tools import (
    EXECUTOR_CONCURRENCY,
    FILLER_DELAY_S,
    INTERRUPTED,
    MAX_INVALID_ROUNDS,
    MAX_TOOL_CALLS_PER_CALL,
    MAX_TOOL_ROUNDS,
    TOOL_FAILED,
    PlannedCall,
    RoundPlan,
    plan_round,
)
from va_engine.templating import MissingVariables, Template, TemplateScope, TemplateSyntaxError, render
from va_engine.tool_executor import ToolAnswer, ToolExecutor
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
from va_voice.phrase_cache import TOOL_FILLERS
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


def _prerendered(context: CallContext) -> str | None:
    """The opener rendered at prefetch (``None`` when absent or blank)."""
    text = (context.opener_text or "").strip()
    return text or None


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
        on_state: Callable[[EngineState], None] | None = None,
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
        self._on_state = on_state
        self._fillers_on = settings.engine.tool_fillers
        self._fillers = 0
        self._facts = _runnable_facts(facts, executor)
        custom = executor.definitions() if executor is not None else ()
        self._tools: tuple[ToolDef, ...] = (*system_tools(self._facts), *custom)
        speech = executor.tool_speech if executor is not None else None
        self._tool_note = tool_usage_note(speech or DEFAULT_TOOL_SPEECH)
        self._window = ConversationWindow(settings.llm.context_budget_tokens, counter)
        self._task = parse_field(config.task, "task")
        opener = config.first_sentence
        self._opener = parse_field(opener, "first_sentence") if opener and opener.strip() else None
        self._opener_text = _prerendered(context)
        self._missing = MissingVariables(sink=partial(_log_missing_variable, str(context.call_id)))
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

    async def start(self, greeting: UserTurn | None = None) -> AsyncGenerator[BrainOutput]:
        """The opener; a ``greeting`` (``wait_for_greeting``) is appended first as the caller's message, so a
        generated opener answers it (specs/07 §11 step 4). The ``opener_text`` rendered at prefetch is spoken
        verbatim (it matches the phrase cache entry, Decision P05-T10d); without it ``first_sentence`` is
        rendered now."""
        if greeting is not None:
            self._accept(greeting)
        if self._opener is not None:
            text = self._opener_text or self._render(self._opener).strip()
            if text:
                yield self._say(text)
                return
        async with contextlib.aclosing(self._respond()) as outputs:
            async for output in outputs:
                yield output

    async def on_user_turn(self, turn: UserTurn) -> AsyncGenerator[BrainOutput]:
        if turn.supersedes is not None:
            self._drop_turn(turn.supersedes)
        self._accept(turn)
        async with contextlib.aclosing(self._respond()) as outputs:
            async for output in outputs:
                yield output

    def _accept(self, turn: UserTurn) -> None:
        """Append the caller's turn (keypad turns as the DTMF message) and remember where it starts."""
        self._state.turn_index += 1
        self._marks[turn.index] = (len(self._messages), self._records_added)
        message = dtmf_message(turn.dtmf) if turn.dtmf else ChatMessage(role="user", content=turn.text)
        self._last_user = message.content or ""
        self._append(message, at_ms=int(turn.end_ms))

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

    def on_transfer_failed(self, spoken: str) -> None:
        """The transfer the last reply asked for failed and the media worker says ``spoken`` instead: keep it
        as an assistant message (the earlier tool result stays ``transferring``), so the next request knows
        (specs/06 §10.1 Decision P05-T13). A barge-in on it trims it like any spoken line."""
        text = spoken.strip()
        if text:
            self._mark_spoken(self._append(ChatMessage(role="assistant", content=text)))

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
        tasks: dict[str, asyncio.Task[ToolAnswer]] = {}
        try:
            tasks = self._start_tools(plan.executed)
            line = await self._tool_line(plan.executed, tasks)
            if line is not None:
                yield self._note_line(line)
            results = {call_id: await task for call_id, task in tasks.items()}
            self._merge_variables(plan.executed, results)
            for item in plan.calls:
                answer = results.get(item.call.id)
                content = (answer.content if answer is not None else item.answer) or TOOL_FAILED
                self._answer(item.call, content, answered)
                if item.digits is not None:
                    yield PressButton(digits=item.digits)
        finally:
            await _stop_tasks(tasks.values())
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

    def _start_tools(self, planned: tuple[PlannedCall, ...]) -> dict[str, asyncio.Task[ToolAnswer]]:
        """One task per executed call (at most :data:`EXECUTOR_CONCURRENCY` run at once), all rendered in
        the scope of the round's start."""
        executor = self._executor
        if executor is None or not planned:
            return {}
        gate = asyncio.Semaphore(EXECUTOR_CONCURRENCY)
        scope = dataclasses.replace(self._scope(), variables=dict(self._state.variables))
        return {
            item.call.id: asyncio.create_task(
                _execute_gated(executor, item.call, scope, gate), name=f"tool-{item.call.name}"
            )
            for item in planned
        }

    async def _tool_line(
        self, planned: tuple[PlannedCall, ...], tasks: Mapping[str, asyncio.Task[ToolAnswer]]
    ) -> str | None:
        """What the caller hears while the round's tools run: tool speech, else a filler for slow tools."""
        if not self._fillers_on or not tasks:
            return None
        speech = self._speech_line(planned)
        if speech is not None:
            return speech
        if not await self._still_running(tasks.values()):
            return None
        filler = TOOL_FILLERS[self._fillers % len(TOOL_FILLERS)]
        self._fillers += 1
        return filler

    def _speech_line(self, planned: tuple[PlannedCall, ...]) -> str | None:
        """The rendered ``speech`` of the first executed call whose tool has one (arguments: ``input.*``)."""
        executor = self._executor
        if executor is None:
            return None
        for item in planned:
            template = executor.speech(item.call.name)
            if template is None:
                continue
            scope = dataclasses.replace(self._scope(), input={"input": _arguments(item.call)})
            try:
                text = render(template, scope).strip()
            except TemplateSyntaxError:
                _log.warning("task_tool_speech_invalid", tool=item.call.name)
                return None
            return text or None
        return None

    async def _still_running(self, tasks: Iterable[asyncio.Task[ToolAnswer]]) -> bool:
        """Wait up to :data:`FILLER_DELAY_S` (on the injected clock) for ``tasks``; whether any still runs."""
        pending = {task for task in tasks if not task.done()}
        if not pending:
            return False
        delay = asyncio.create_task(self._clock.sleep(FILLER_DELAY_S), name="tool-filler-delay")
        try:
            while pending and not delay.done():
                await asyncio.wait({*pending, delay}, return_when=asyncio.FIRST_COMPLETED)
                pending = {task for task in pending if not task.done()}
        finally:
            await _stop_tasks((delay,))
        return bool(pending)

    def _merge_variables(self, planned: tuple[PlannedCall, ...], results: Mapping[str, ToolAnswer]) -> None:
        changed = False
        for item in planned:
            answer = results.get(item.call.id)
            if answer is not None and answer.variables:
                self._state.variables.update(answer.variables)
                changed = True
        if changed and self._on_state is not None:
            self._on_state(self._state)

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

    def _note_line(self, text: str) -> SpeakStatic:
        """A tool speech or filler line: a transcript record, never a model message (an assistant message
        may not sit between a tool call and its answers), so a barge-in on it trims nothing."""
        self._state.add_turn(
            TurnRecord(role="assistant", content=text, node_id=TASK_NODE_ID, at_ms=self._now_ms())
        )
        self._records_added += 1
        self._spoken = None
        self._spoken_record = None
        return SpeakStatic(text)

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


def _log_missing_variable(call_id: str, path: str) -> None:
    """The template sink (a function, not a bound method: the brain is freed without the cyclic collector)."""
    _log.info("template_variable_missing", path=path, call_id=call_id)


def _arguments(call: ToolCall) -> dict[str, object]:
    """The call's arguments as a mapping (``{}`` when they are not a JSON object)."""
    try:
        value: object = orjson.loads(call.arguments_json or "{}")
    except orjson.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _runnable_facts(facts: SystemToolFacts, executor: ToolExecutor | None) -> SystemToolFacts:
    """``facts`` without the executor-backed system tools the executor does not run (never offered)."""

    def runs(name: str) -> bool:
        return executor is not None and executor.handles(name)

    return dataclasses.replace(
        facts,
        sms_capable=facts.sms_capable and runs("send_sms"),
        allow_schedule_callback=facts.allow_schedule_callback and runs("schedule_callback"),
        has_knowledge_bases=facts.has_knowledge_bases and runs("lookup_knowledge"),
    )


async def _execute_gated(
    executor: ToolExecutor, call: ToolCall, scope: TemplateScope, gate: asyncio.Semaphore
) -> ToolAnswer:
    """Run one call (an exception answers ``tool_failed``)."""
    async with gate:
        try:
            return await executor.execute(call, scope)
        except Exception as exc:
            _log.warning("task_tool_failed", tool=call.name, error_type=type(exc).__name__)
            return ToolAnswer(TOOL_FAILED)


async def _stop_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel the unfinished ``tasks`` and wait until they ended."""
    running = [task for task in tasks if not task.done()]
    for task in running:
        task.cancel()
    if running:
        await asyncio.wait(running)
