"""The pathway engine as the call pipeline's Brain (specs/11 §10; Decision P07-T12a under specs/11 §10).

:class:`PathwayBrain` adapts :class:`~va_engine.engine.PathwayEngine` (a generator of the specs/11 §7
actions) to the narrower :class:`va_voice.pipeline.Brain` contract (specs/07 §16):

- ``Say`` → :class:`~va_voice.pipeline.SpeakStatic`, ``SayStream`` → :class:`~va_voice.pipeline.SpeakText`
  (``interruptible`` ``None`` = the call level, i.e. interruptible);
- ``EndCallAction`` → ``EndCall(final_text=None, reason="agent_hangup")``: the final line was already yielded
  and played (the pipeline resumes a reply only after its speech finished), the engine's reason is logged;
- ``TransferAction`` → :class:`~va_engine.models.results.TransferRequest` (a warm transfer runs cold until
  warm transfers exist, logged ``pathway_brain.warm_transfer_as_cold``); ``SendDtmfAction`` →
  :class:`~va_engine.models.results.PressButton`;
- ``Wait`` arms the node's wait: :meth:`timing` reports ``no_input_timeout_ms`` and the pipeline hands that
  expiry to :meth:`on_no_input` (→ ``PathwayEngine.on_wait_timeout``) without its own reprompt policy;
- ``SwitchGraph`` (applied inside the engine) and ``HumanTakeover`` (text channels) never reach a voice
  consumer: logged ``pathway_brain.action_ignored``, nothing yielded.

Closing an output stream (barge-in) closes the engine generator; the pipeline then calls
:meth:`on_interrupted`. ``on_state`` runs after every finished or closed engine generator and after a
truncation or a transfer-failure note, so the runtime can snapshot the state.

:meth:`turn_tags` tells the media worker's turn recorder which engine turn and node a pipeline turn belongs to
(``call_turns.pathway_turn``/``node_id``/``node_name``; Decision P07-T12b under specs/11 §10): the pipeline's
observer sees a routed caller turn before the Brain accepts it (every routed turn is accepted next, so it is
the engine's next turn index at the current node); an agent turn belongs to the engine turn and node that were
current when its line was handed to the pipeline.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_domain.call_object import PathwayLog
from va_engine.engine import PathwayEngine
from va_engine.models import (
    Action,
    EndCallAction,
    EngineState,
    Say,
    SayStream,
    SendDtmfAction,
    SwitchGraph,
    TransferAction,
    Wait,
)
from va_engine.models.results import PressButton, TransferRequest
from va_voice.pipeline.models import BrainOutput, BrainTiming, EndCall, SpeakStatic, SpeakText, UserTurn

__all__ = ["PATHWAY_END_REASON", "SETTLE_TIMEOUT_S", "PathwayBrain", "TurnTags"]

PATHWAY_END_REASON: Final = "agent_hangup"
"""The pipeline end reason of an End Call node (the engine's own reason goes to the log)."""

SETTLE_TIMEOUT_S: Final = 2.0
"""How long :meth:`PathwayBrain.aclose` waits for background extractions before closing the engine."""

_MS_PER_SECOND: Final = 1000

_log = get_logger(__name__)


_NOTHING: Final[tuple[BrainOutput, ...]] = ()


@dataclass(frozen=True, slots=True)
class TurnTags:
    """The engine turn index and node a pipeline turn belongs to."""

    pathway_turn: int
    node_id: str | None
    node_name: str | None


async def _nothing() -> AsyncGenerator[BrainOutput]:
    """An empty output stream."""
    for output in _NOTHING:
        yield output


class PathwayBrain:
    """One pathway conversation behind the pipeline's Brain protocol."""

    def __init__(
        self,
        engine: PathwayEngine,
        *,
        clock: Clock,
        on_state: Callable[[EngineState], None] | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._on_state = on_state
        self._wait_ms: int | None = None
        self._spoken: tuple[int, str] | None = None

    @property
    def engine(self) -> PathwayEngine:
        return self._engine

    @property
    def state(self) -> EngineState:
        return self._engine.state

    @property
    def variables(self) -> dict[str, JsonValue]:
        """A copy of the conversation's variables."""
        return dict(self._engine.state.variables)

    def pathway_logs(self) -> list[PathwayLog]:
        return self._engine.pathway_logs()

    def turn_tags(self, role: Literal["user", "assistant"]) -> TurnTags | None:
        """The engine turn and node of the pipeline turn being recorded (see the module docstring); ``None``
        for a caller turn once the conversation ended (the engine ignores it)."""
        state = self._engine.state
        if role == "user":
            if state.ended:
                return None
            return self._tags(state.turn_index + 1, state.current_node_id)
        turn_index, node_id = self._spoken or (state.turn_index, state.current_node_id)
        return self._tags(turn_index, node_id)

    # Brain protocol.

    def start(self, greeting: UserTurn | None = None) -> AsyncGenerator[BrainOutput]:
        return self._relay(self._engine.start(greeting))

    def on_user_turn(self, turn: UserTurn) -> AsyncGenerator[BrainOutput]:
        """Any caller turn (speech or keypad) ends the node's wait (the engine re-arms it with a ``Wait``)."""
        self._wait_ms = None
        return self._relay(self._engine.handle_user_turn(turn))

    def on_no_input(self) -> AsyncGenerator[BrainOutput]:
        """The node's wait expired: the engine reprompts, ends, repeats, or moves on. Without a wait: nothing
        (the pipeline speaks its own reprompt)."""
        if self._wait_ms is None:
            return _nothing()
        self._wait_ms = None
        return self._relay(self._engine.on_wait_timeout())

    async def on_interrupted(self, spoken_text: str) -> None:
        await self._engine.on_interrupted(spoken_text)
        self._publish()

    def on_transfer_failed(self, spoken: str) -> None:
        """The transfer failed and the media worker says ``spoken`` instead: the history keeps it so later
        decisions know (specs/06 §10.1 Decision P05-T13)."""
        self._engine.note_assistant(spoken)
        self._mark_spoken()
        self._publish()

    def timing(self) -> BrainTiming:
        """The current node's interruption and endpointing settings, plus its armed wait."""
        try:
            node = self._engine.navigator.node
        except LookupError:
            return BrainTiming(no_input_timeout_ms=self._wait_ms)
        options = node.data.model_options
        return BrainTiming(
            interruptibility=0 if options.block_interruptions else options.interruptibility,
            resumption_speed=options.resumption_speed,
            no_input_timeout_ms=self._wait_ms,
        )

    async def aclose(self) -> None:
        """Let background extractions merge (at most :data:`SETTLE_TIMEOUT_S`), then close the engine."""
        try:
            settled = await within(self._engine.settle_background(), self._clock, SETTLE_TIMEOUT_S)
            if not settled:
                _log.warning("pathway_brain.settle_timeout", timeout_s=SETTLE_TIMEOUT_S)
        finally:
            await self._engine.aclose()
        self._publish()

    # Relay.

    async def _relay(self, actions: AsyncGenerator[Action]) -> AsyncGenerator[BrainOutput]:
        try:
            async with contextlib.aclosing(actions) as running:
                async for action in running:
                    output = self._output(action)
                    if output is not None:
                        yield output
        finally:
            self._publish()

    def _output(self, action: Action) -> BrainOutput | None:  # noqa: PLR0911 - one return per action type
        if isinstance(action, Say):
            self._mark_spoken()
            return SpeakStatic(action.text, interruptible=action.interruptible is not False)
        if isinstance(action, SayStream):
            self._mark_spoken()
            return SpeakText(action.tokens, interruptible=action.interruptible is not False)
        if isinstance(action, Wait):
            self._wait_ms = max(1, round(action.seconds * _MS_PER_SECOND))
            return None
        if isinstance(action, EndCallAction):
            _log.info("pathway_brain.end_call", reason=action.reason, ended_by=action.ended_by)
            return EndCall(final_text=None, reason=PATHWAY_END_REASON)
        if isinstance(action, TransferAction):
            if action.mode == "warm":
                _log.info("pathway_brain.warm_transfer_as_cold")
            return TransferRequest(target=action.target)
        if isinstance(action, SendDtmfAction):
            return PressButton(digits=action.digits)
        kind = "switch_graph" if isinstance(action, SwitchGraph) else "human_takeover"
        _log.warning("pathway_brain.action_ignored", action=kind)
        return None

    def _mark_spoken(self) -> None:
        state = self._engine.state
        self._spoken = (state.turn_index, state.current_node_id)

    def _tags(self, turn_index: int, node_id: str) -> TurnTags:
        node = self._engine.navigator.graph.node(node_id)
        return TurnTags(turn_index, node_id, None if node is None else node.data.name)

    def _publish(self) -> None:
        if self._on_state is not None:
            self._on_state(self._engine.state)
