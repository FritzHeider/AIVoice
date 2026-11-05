"""In-call persistence of one call: turn rows and live call events (specs/07-media-pipeline.md §8 steps 2, 6,
7; specs/19 §2 ``call.turn``, ``call.interrupt``, ``call.latency``; specs/07 §8 Decision P05-T11a).

:class:`TurnRecorder` is the call's :class:`~va_voice.pipeline.PipelineObserver`. Its callbacks are sync: turn
rows go to the write-behind :class:`TurnSink` at once, events are published in callback order by one chain of
background tasks (:meth:`TurnRecorder.drain` waits for it). The assistant ``call.turn`` event is held back
until the next callback: when that is the reply's latency report (``on_latency``, which the pipeline sends
after the reply's last agent turn) the event carries ``data.latency`` (the measured ``call_turns.latency``
values) and is published before the ``call.latency`` events; any other callback or :meth:`TurnRecorder.drain`
publishes it as is (specs/19 §1 Decision P06-T05). :meth:`TurnRecorder.started` publishes ``call.started``
once, before the pipeline runs (specs/07 §3 step 7; specs/19 §1 Decision P06-T07). A publish failure is logged
``media.event_publish_failed`` (event type and error type, never turn text) and never reaches the pipeline.

Pathway calls (specs/11 §10 Decision P07-T12b): the recorder's :attr:`TurnRecorder.tagger` (the call's
:class:`~va_engine.pathway_brain.PathwayBrain`) tags each row with its engine turn (``pathway_turn``) and node
(``node_id``/``node_name``, also ``call.turn`` ``data.node_id``); :meth:`TurnRecorder.engine_event`
publishes the engine's ``call.route``/``call.extraction``/``call.loop_condition``/``call.webhook_node``/
``call.sms_sent`` events in the same ordered chain (messages from :mod:`va_media.pathway_events`); any other
type is logged ``media.engine_event_unknown`` and dropped.

Task-mode tools (specs/13 §5 Decision P08-T02b1): the recorder is the call's
:class:`~va_tools.executor.ToolEvents`: :meth:`TurnRecorder.tool` publishes ``call.tool`` (message
:func:`tool_message`, tool name and outcome only) and :meth:`TurnRecorder.tool_failed` a ``warning``
``call.error`` with ``data`` ``{"code": "TOOL_FAILED", "detail": ...}`` (message :data:`TOOL_FAILED_MESSAGE`).

Times: ``start_ms``/``end_ms`` are relative to the call's answer time: ``pipeline_ms - origin_ms +
answered_offset_ms`` (rounded, at least 0), where ``origin_ms`` is the pipeline clock when the recorder was
built and ``answered_offset_ms`` how long before that the carrier reported the answer (0 when unknown).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.call_turns import CallTurnRow, LatencyMerge, TurnDelete, TurnOp
from va_db.models import CallEventLevel, CallTurnRole
from va_domain.call_context import CallContext
from va_engine.pathway_brain import TurnTags
from va_events import DomainEvent, EventType, new_event
from va_media.pathway_events import PATHWAY_EVENT_TYPES, event_message
from va_voice.latency import TurnReport
from va_voice.pipeline import AgentTurn, InterruptRecord, UserTurn

__all__ = [
    "EVENT_MESSAGE_CHARS",
    "STARTED_MESSAGE",
    "TOOL_FAILED_MESSAGE",
    "CallFlusher",
    "CallPersistence",
    "EventSink",
    "TurnRecorder",
    "TurnSink",
    "TurnStore",
    "TurnTagger",
    "tool_message",
]

STARTED_MESSAGE: Final = "Call started"
"""The ``call.started`` message."""

EVENT_MESSAGE_CHARS: Final = 200
"""``call.turn`` messages carry the turn text cut to this many characters (the full text is in ``data``)."""

TOOL_FAILED_MESSAGE: Final = "Tool call failed"
"""The message of the ``call.error`` ``TOOL_FAILED`` event."""

_log = get_logger(__name__)


def tool_message(data: dict[str, JsonValue]) -> str:
    """The ``call.tool`` message: the tool's name and the run's outcome."""
    return f"Tool {data.get('tool_name')}: {data.get('outcome')}"


class TurnSink(Protocol):
    """Where turn ops go (:class:`~va_media.turn_writer.TurnWriter`)."""

    def add(self, op: TurnOp) -> None: ...


class TurnTagger(Protocol):
    """Names the engine turn and node of the turn being recorded
    (:class:`va_engine.pathway_brain.PathwayBrain`)."""

    def turn_tags(self, role: Literal["user", "assistant"]) -> TurnTags | None: ...


class EventSink(Protocol):
    """Where events go (:class:`va_events.EventPublisher` satisfies it)."""

    def publish(self, event: DomainEvent) -> Awaitable[DomainEvent]: ...


class TurnRecorder:
    """Maps one call's pipeline callbacks onto ``call_turns`` ops and call events."""

    def __init__(  # noqa: PLR0913 - the call plus its keyword-only collaborators and time origin
        self,
        context: CallContext,
        *,
        turns: TurnSink,
        events: EventSink,
        clock: Clock,
        origin_ms: float,
        answered_offset_ms: float = 0.0,
        tagger: TurnTagger | None = None,
    ) -> None:
        self.tagger = tagger
        self._context = context
        self._turns = turns
        self._events = events
        self._clock = clock
        self._offset_ms = max(answered_offset_ms, 0.0) - origin_ms
        self._tail: asyncio.Task[None] | None = None
        self._held: DomainEvent | None = None
        self._started = False

    def started(self, media_format: str) -> None:
        """Publish ``call.started`` (``{direction, media_format}``, e.g. ``"PCMU/8000"``); later calls do
        nothing."""
        if self._started:
            return
        self._started = True
        data: dict[str, JsonValue] = {"direction": self._context.direction, "media_format": media_format}
        self._publish(EventType.CALL_STARTED, STARTED_MESSAGE, data)

    def on_user_turn(self, turn: UserTurn) -> None:
        if turn.supersedes is not None:
            self._turns.add(self._delete(turn.supersedes))
        tags = self._tags("user")
        self._turns.add(
            self._row(
                turn.index,
                "user",
                turn.text,
                turn.start_ms,
                turn.end_ms,
                language_code=turn.language_code,
                tags=tags,
            )
        )
        self._enqueue(self._turn_event(turn.index, "user", turn.text, interrupted=False, tags=tags))

    def on_agent_turn(self, turn: AgentTurn) -> None:
        tags = self._tags("assistant")
        self._turns.add(
            self._row(
                turn.index,
                "assistant",
                turn.text,
                turn.started_ms,
                turn.ended_ms,
                text_spoken=turn.text_spoken,
                interrupted=turn.interrupted,
                tags=tags,
            )
        )
        self._release_held()
        self._held = self._turn_event(
            turn.index, "assistant", turn.text, interrupted=turn.interrupted, tags=tags
        )

    def on_latency(self, report: TurnReport) -> None:
        values = report.latency.as_dict()
        if values:
            context = self._context
            self._turns.add(
                LatencyMerge(context.org_id, context.call_id, report.turn_index, "user", dict(values))
            )
        held, self._held = self._held, None
        if held is not None:
            if values:
                latency: dict[str, JsonValue] = dict(values)
                held = held.model_copy(update={"data": {**held.data, "latency": latency}})
            self._enqueue(held)
        for item in report.events:
            data: dict[str, JsonValue] = dict(item.data)
            self._publish(EventType.CALL_LATENCY, item.message, data)

    def on_interrupt(self, record: InterruptRecord) -> None:
        data: dict[str, JsonValue] = {
            "turn_index": record.turn_index,
            "played_chars": record.played_chars,
            "total_chars": record.total_chars,
            "played_ms": round(record.played_ms),
            "reaction_ms": round(record.reaction_ms),
            "speech_ms": round(record.at_ms - record.speech_start_ms),
            "clear_delay_ms": round(record.clear_delay_ms),
            "level": record.level,
        }
        message = f"Interrupted after {record.played_chars} of {record.total_chars} characters"
        self._publish(EventType.CALL_INTERRUPT, message, data)

    def engine_event(self, event_type: str, data: dict[str, JsonValue]) -> None:
        """Publish a pathway engine event (see the module docstring)."""
        known = next((member for member in PATHWAY_EVENT_TYPES if member.value == event_type), None)
        if known is None:
            _log.warning("media.engine_event_unknown", event_type=event_type)
            return
        self._publish(known, event_message(known, data), data)

    def tool(self, data: dict[str, JsonValue]) -> None:
        """Publish ``call.tool`` for one tool run."""
        self._publish(EventType.CALL_TOOL, tool_message(data), data)

    def tool_failed(self, detail: str) -> None:
        """Publish the ``call.error`` ``TOOL_FAILED`` of a failed tool run."""
        data: dict[str, JsonValue] = {"code": "TOOL_FAILED", "detail": detail}
        self._publish(EventType.CALL_ERROR, TOOL_FAILED_MESSAGE, data, level="warning")

    async def drain(self) -> None:
        """Release a held assistant turn, then wait until every queued event was published (or failed)."""
        self._release_held()
        tail = self._tail
        if tail is not None:
            await asyncio.wait({tail})

    def _relative(self, pipeline_ms: float) -> int:
        return max(round(pipeline_ms + self._offset_ms), 0)

    def _row(  # noqa: PLR0913 - one call_turns row
        self,
        index: int,
        role: CallTurnRole,
        text: str,
        start_ms: float,
        end_ms: float,
        *,
        text_spoken: str | None = None,
        interrupted: bool = False,
        language_code: str | None = None,
        tags: TurnTags | None = None,
    ) -> CallTurnRow:
        context = self._context
        return CallTurnRow(
            org_id=context.org_id,
            call_id=context.call_id,
            turn_index=index,
            role=role,
            text=text,
            start_ms=self._relative(start_ms),
            end_ms=self._relative(end_ms),
            text_spoken=text_spoken,
            interrupted=interrupted,
            language_code=language_code,
            node_id=None if tags is None else tags.node_id,
            node_name=None if tags is None else tags.node_name,
            pathway_turn=None if tags is None else tags.pathway_turn,
        )

    def _tags(self, role: Literal["user", "assistant"]) -> TurnTags | None:
        return None if self.tagger is None else self.tagger.turn_tags(role)

    def _delete(self, index: int) -> TurnDelete:
        return TurnDelete(self._context.org_id, self._context.call_id, index, "user")

    def _turn_event(
        self, index: int, role: CallTurnRole, text: str, *, interrupted: bool, tags: TurnTags | None = None
    ) -> DomainEvent:
        data: dict[str, JsonValue] = {
            "turn_index": index,
            "role": role,
            "text": text,
            "node_id": None if tags is None else tags.node_id,
            "interrupted": interrupted,
        }
        return self._event(EventType.CALL_TURN, text[:EVENT_MESSAGE_CHARS], data)

    def _event(
        self,
        event_type: EventType,
        message: str,
        data: dict[str, JsonValue],
        *,
        level: CallEventLevel = "info",
    ) -> DomainEvent:
        return new_event(
            event_type,
            org_id=self._context.org_id,
            clock=self._clock,
            call_id=self._context.call_id,
            level=level,
            message=message,
            data=data,
        )

    def _publish(
        self,
        event_type: EventType,
        message: str,
        data: dict[str, JsonValue],
        *,
        level: CallEventLevel = "info",
    ) -> None:
        self._enqueue(self._event(event_type, message, data, level=level))

    def _release_held(self) -> None:
        held, self._held = self._held, None
        if held is not None:
            self._enqueue(held)

    def _enqueue(self, event: DomainEvent) -> None:
        """Queue ``event`` behind every earlier one (a held assistant turn is released first)."""
        if self._held is not None and event is not self._held:
            self._release_held()
        previous = self._tail
        self._tail = asyncio.get_running_loop().create_task(
            self._publish_after(previous, event), name="call-event-publish"
        )

    async def _publish_after(self, previous: asyncio.Task[None] | None, event: DomainEvent) -> None:
        if previous is not None:
            await asyncio.wait({previous})
        try:
            await self._events.publish(event)
        except Exception as exc:  # events are best effort: a Redis failure must never end the call
            _log.warning("media.event_publish_failed", event_type=event.type.value, error=type(exc).__name__)


class TurnStore(TurnSink, Protocol):
    """A turn sink that can write one call's pending ops now (:class:`~va_media.turn_writer.TurnWriter`)."""

    async def flush_call(self, call_id: uuid.UUID) -> int: ...


class CallFlusher(Protocol):
    """Writes one call's pending rows now (:class:`va_events.CallEventBuffer` satisfies it)."""

    async def flush_call(self, call_id: uuid.UUID) -> int: ...


class CallPersistence:
    """The per-process persistence a :class:`~va_media.runtime.CallRuntime` hands to each call: the turn
    writer, the event publisher, and the ``call_events`` buffer behind it."""

    def __init__(self, *, turns: TurnStore, events: EventSink, event_rows: CallFlusher | None = None) -> None:
        self.turns = turns
        self.events = events
        self.event_rows = event_rows

    def recorder(
        self, context: CallContext, *, clock: Clock, answered_at: datetime | None = None
    ) -> TurnRecorder:
        """A recorder whose time origin is now (``answered_at``: the carrier's answer time, if known)."""
        offset_ms = 0.0
        if answered_at is not None:
            offset_ms = (clock.now() - answered_at) / timedelta(milliseconds=1)
        return TurnRecorder(
            context,
            turns=self.turns,
            events=self.events,
            clock=clock,
            origin_ms=clock.monotonic() * 1000.0,
            answered_offset_ms=offset_ms,
        )

    async def finish_call(self, recorder: TurnRecorder, context: CallContext) -> None:
        """Call end: wait for the recorder's events, then write the call's turns and event rows now. Failures
        are logged (the rows stay queued for the periodic flush)."""
        await recorder.drain()
        flushers: list[tuple[str, CallFlusher]] = [("turns", self.turns)]
        if self.event_rows is not None:
            flushers.append(("call_events", self.event_rows))
        for name, flusher in flushers:
            try:
                await flusher.flush_call(context.call_id)
            except Exception as exc:  # the periodic flush retries; the call end must not fail on it
                _log.warning("media.call_flush_failed", target=name, error=type(exc).__name__)
