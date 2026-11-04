"""Voicemail detection and actions of an outbound call (specs/07-media-pipeline.md §11 step 2, §14; specs/10
§9.4; specs/07 §14 Decision P05-T10c).

:class:`VoicemailMonitor` watches one outbound call for an answering machine:

- the AMD verdict (:class:`~va_media.carrier_watch.CarrierWatch`): ``machine`` within
  ``voicemail.detect_timeout_ms`` of the answer is a detection (trigger ``amd``); ``fax`` ends the call
  silently (``fax_detected``; the edge already stored ``answered_by='fax'`` and emitted ``call.amd``);
  ``human`` and ``unknown`` change nothing;
- with ``voicemail.sensitive`` (:func:`va_domain.voicemail.classifier_enabled`), the callee's delivered turns
  (it is a :class:`~va_voice.pipeline.PipelineObserver`): the words so far go to the ``classify_answer``
  classifier, one request at a time (turns arriving meanwhile re-run it once with the whole transcript);
  ``voicemail`` with confidence ≥ :data:`CONFIDENCE_THRESHOLD` is a detection (trigger ``classifier``),
  ``human``/``ivr`` stop the classification, failures wait for the next turn.

Only the first detection acts, ``ivr_mode`` calls never act, and anything arriving after the window is logged
``media.voicemail_late``. A detection first hands the end of the call to the pipeline by ``voicemail.action``
(``hangup`` → ``voicemail_hangup``; ``leave_message``/``leave_message_and_sms`` → the templated
``voicemail.message`` after the beep or 1.2 s of silence, then ``voicemail_left``, with
``leave_message_and_sms`` sending the SMS (the injected ``sms`` hook, :mod:`va_media.voicemail_sms`)
between the message and the hangup; a blank message hangs up;
``ignore`` → the conversation goes on), then records it in the background: ``call.voicemail``
(``data.action``, ``data.trigger``, ``data.confidence`` for the classifier) and, for the classifier,
``answered_by='voicemail'`` (the edge stores it for AMD). Recording failures are logged and never undo the
action. Transcripts and the message are never logged.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from pydantic import BaseModel, Field, JsonValue, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.call_end import ANSWERED_STATUSES
from va_db.repositories import CallRepo
from va_db.session import Database
from va_domain.call_context import CallContext
from va_domain.voicemail import AmdVerdict, classifier_enabled
from va_engine.opener import render_call_text
from va_events import DomainEvent, EventPublishError, EventType, new_event
from va_llm import ChatMessage, LlmUnavailableError, StructuredOutputError
from va_llm.policy import CallType
from va_llm.prompts import render_answer_classifier
from va_voice.latency import TurnReport
from va_voice.pipeline import AgentTurn, InterruptRecord, Takeover, UserTurn

__all__ = [
    "CLASSIFIER_DESCRIPTION",
    "CONFIDENCE_THRESHOLD",
    "VOICEMAIL_MESSAGE",
    "AnswerClass",
    "AnswerClassifier",
    "AnsweredByWriter",
    "DbAnsweredBy",
    "LlmAnswerClassifier",
    "StructuredSource",
    "VoicemailEvents",
    "VoicemailMonitor",
]

CONFIDENCE_THRESHOLD: Final = 0.8
"""A classification is acted on from this confidence (specs/10 §9.4)."""

VOICEMAIL_MESSAGE: Final = "Voicemail detected"
"""``message`` of the ``call.voicemail`` event."""

CLASSIFIER_DESCRIPTION: Final = "Classify who answered the call."

type Trigger = Literal["amd", "classifier"]

_log = get_logger(__name__)


class AnswerClass(BaseModel):
    label: Literal["human", "voicemail", "ivr", "unknown"]
    confidence: float = Field(ge=0, le=1)


class AnswerClassifier(Protocol):
    """Classifies the callee's first words (:class:`LlmAnswerClassifier`)."""

    async def classify(self, transcript: str, seconds: float) -> AnswerClass: ...


class StructuredSource(Protocol):
    """Where structured outputs come from (:class:`va_llm.LlmGateway` satisfies it)."""

    async def call_structured[M: BaseModel](  # noqa: PLR0913 - mirrors LlmGateway.call_structured
        self,
        call_type: CallType,
        schema: type[M],
        *,
        org_id: uuid.UUID,
        messages: list[ChatMessage],
        function_name: str,
        description: str,
        call_id: uuid.UUID | None = None,
    ) -> M: ...


class LlmAnswerClassifier:
    """``classify_answer`` through the LLM gateway (call type ``voicemail``, specs/10 §9.4)."""

    def __init__(self, gateway: StructuredSource, *, org_id: uuid.UUID, call_id: uuid.UUID) -> None:
        self._gateway = gateway
        self._org_id = org_id
        self._call_id = call_id

    async def classify(self, transcript: str, seconds: float) -> AnswerClass:
        prompt = render_answer_classifier(transcript, seconds)
        return await self._gateway.call_structured(
            "voicemail",
            AnswerClass,
            org_id=self._org_id,
            messages=[ChatMessage(role="system", content=prompt)],
            function_name="classify_answer",
            description=CLASSIFIER_DESCRIPTION,
            call_id=self._call_id,
        )


class VoicemailEvents(Protocol):
    """Where ``call.voicemail`` goes (:class:`va_events.EventPublisher` satisfies it)."""

    def publish(self, event: DomainEvent) -> Awaitable[DomainEvent]: ...


class AnsweredByWriter(Protocol):
    """Stores ``answered_by='voicemail'`` on the call row (:class:`DbAnsweredBy`)."""

    async def set_voicemail(self, call_id: uuid.UUID) -> None: ...


class DbAnsweredBy:
    """``calls.answered_by = 'voicemail'`` while the call is answered, in the org's tenant session."""

    def __init__(self, database: Database, org_id: uuid.UUID) -> None:
        self._database = database
        self._org_id = org_id

    async def set_voicemail(self, call_id: uuid.UUID) -> None:
        async with self._database.tenant_session(self._org_id) as session:
            updated = await CallRepo(session, self._org_id).set_answered_by(
                call_id, "voicemail", statuses=ANSWERED_STATUSES
            )
        if not updated:
            _log.info("media.answered_by_skipped", detail="not_answered")


class VoicemailMonitor:
    """The voicemail detector of one outbound call: :meth:`bind` the pipeline's takeover, call :meth:`start`
    with the attach-time verdict before the pipeline runs, run :meth:`run` as a task (the classifier), and
    await :meth:`stop` when the call ends."""

    def __init__(  # noqa: PLR0913 - the call plus its keyword-only collaborators
        self,
        *,
        context: CallContext,
        beep: asyncio.Event,
        clock: Clock,
        classifier: AnswerClassifier | None = None,
        events: VoicemailEvents | None = None,
        answered_by: AnsweredByWriter | None = None,
        answered_at: datetime | None = None,
        pipeline_takeover: Callable[[Takeover], bool] | None = None,
        sms: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._context = context
        self._sms = sms
        self._config = context.config
        self._pipeline_takeover = pipeline_takeover
        self._beep = beep
        self._clock = clock
        self._classifier = classifier if classifier_enabled(context.config, context.direction) else None
        self._events = events
        self._answered_by = answered_by
        backdate_ms = 0.0
        if answered_at is not None:
            backdate_ms = max((clock.now() - answered_at) / timedelta(milliseconds=1), 0.0)
        self._origin_ms = clock.monotonic() * 1000.0 - backdate_ms
        self._detected = False
        self._classifying = self._classifier is not None
        self._closed = False
        self._transcript: list[str] = []
        self._turn_wake = asyncio.Event()
        self._recordings: set[asyncio.Task[None]] = set()

    @property
    def detected(self) -> bool:
        return self._detected

    @property
    def classifying(self) -> bool:
        return self._classifying

    def bind(self, pipeline_takeover: Callable[[Takeover], bool]) -> None:
        """Set the pipeline's :meth:`~va_voice.pipeline.CallPipeline.take_over` (built after the monitor)."""
        self._pipeline_takeover = pipeline_takeover

    def unbind(self) -> None:
        """Forget the pipeline's takeover (the call is over; it holds the pipeline)."""
        self._pipeline_takeover = None

    def _take_over(self, takeover: Takeover) -> bool:
        if self._pipeline_takeover is None:
            _log.error("media.voicemail_unbound", reason=takeover.reason)
            return False
        return self._pipeline_takeover(takeover)

    def start(self, verdict: AmdVerdict | None) -> None:
        """Act on a verdict the carrier state already held at attach (before the pipeline opens the call)."""
        if verdict is not None:
            self.on_verdict(verdict)

    def on_verdict(self, verdict: AmdVerdict) -> None:
        """An AMD verdict (the carrier watch calls it once)."""
        if self._config.ivr_mode:
            _log.info("media.amd_ignored", verdict=verdict, detail="ivr_mode")
            return
        if verdict == "fax":
            self._fax()
        elif verdict == "machine":
            if self._in_window():
                self._detect("amd", None)
            else:
                _log.info("media.voicemail_late", trigger="amd")

    # PipelineObserver

    def on_user_turn(self, turn: UserTurn) -> None:
        if not self._classifying or self._detected or turn.dtmf is not None or not turn.text.strip():
            return
        if not self._in_window():
            self._classifying = False
            _log.info("media.voicemail_late", trigger="classifier")
            self._turn_wake.set()
            return
        self._transcript.append(turn.text.strip())
        self._turn_wake.set()

    def on_agent_turn(self, turn: AgentTurn) -> None:
        del turn

    def on_latency(self, report: TurnReport) -> None:
        del report

    def on_interrupt(self, record: InterruptRecord) -> None:
        del record

    # classifier

    async def run(self) -> None:
        """Classify the callee's words while the classification is open; returns when it closes."""
        while self._open():
            await self._turn_wake.wait()
            self._turn_wake.clear()
            if self._open():
                await self._classify_once()

    def _open(self) -> bool:
        return self._classifying and not self._detected and not self._closed

    async def _classify_once(self) -> None:
        classifier = self._classifier
        if classifier is None:
            return
        transcript = " ".join(self._transcript)
        try:
            result = await classifier.classify(transcript, self._elapsed_ms() / 1000.0)
        except (LlmUnavailableError, StructuredOutputError, ValidationError, ValueError) as exc:
            _log.warning("media.answer_classifier_failed", error_type=type(exc).__name__)
            return
        if not self._open():
            _log.debug("media.answer_classification_dropped", label=result.label)
            return
        if not self._in_window():
            self._classifying = False
            _log.info("media.voicemail_late", trigger="classifier", label=result.label)
            return
        self._apply(result)

    def _apply(self, result: AnswerClass) -> None:
        if result.label == "unknown" or result.confidence < CONFIDENCE_THRESHOLD:
            _log.debug("media.answer_unsure", label=result.label)
            return
        if result.label == "voicemail":
            self._detect("classifier", result.confidence)
            return
        self._classifying = False
        _log.info("media.answer_classified", label=result.label)

    # actions

    def _fax(self) -> None:
        if self._detected:
            return
        self._detected = True
        self._classifying = False
        accepted = self._take_over(Takeover("fax_detected"))
        _log.info("media.fax_detected", accepted=accepted)

    def _detect(self, trigger: Trigger, confidence: float | None) -> None:
        if self._detected:
            return
        self._detected = True
        self._classifying = False
        self._turn_wake.set()
        action = self._config.voicemail.action
        takeover = self._takeover_for(action)
        accepted = self._take_over(takeover) if takeover is not None else False
        _log.info("media.voicemail_detected", trigger=trigger, action=action, accepted=accepted)
        task = asyncio.get_running_loop().create_task(
            self._record(trigger, action, confidence), name="voicemail-record"
        )
        self._recordings.add(task)
        task.add_done_callback(self._recordings.discard)

    def _takeover_for(self, action: str) -> Takeover | None:
        if action == "ignore":
            return None
        if action == "hangup":
            return Takeover("voicemail_hangup")
        message = render_call_text(
            self._context,
            self._config.voicemail.message,
            now=self._clock.now(),
            field_name="voicemail.message",
        )
        if message is None:
            _log.warning("media.voicemail_message_missing", action=action)
            return Takeover("voicemail_hangup")
        if action != "leave_message_and_sms":
            return Takeover("voicemail_left", text=message, beep=self._beep)
        if self._sms is None:
            _log.warning("media.voicemail_sms_unavailable")
        return Takeover("voicemail_left", text=message, beep=self._beep, after_message=self._sms)

    async def _record(self, trigger: Trigger, action: str, confidence: float | None) -> None:
        if trigger == "classifier" and self._answered_by is not None:
            try:
                await self._answered_by.set_voicemail(self._context.call_id)
            except (SQLAlchemyError, OSError) as exc:
                _log.warning(
                    "media.voicemail_record_failed", target="answered_by", error_type=type(exc).__name__
                )
        if self._events is None:
            return
        data: dict[str, JsonValue] = {"action": action, "trigger": trigger}
        if confidence is not None:
            data["confidence"] = confidence
        event = new_event(
            EventType.CALL_VOICEMAIL,
            org_id=self._context.org_id,
            clock=self._clock,
            call_id=self._context.call_id,
            message=VOICEMAIL_MESSAGE,
            data=data,
        )
        try:
            await self._events.publish(event)
        except (EventPublishError, OSError) as exc:
            _log.warning("media.voicemail_record_failed", target="event", error_type=type(exc).__name__)

    async def stop(self) -> None:
        """The call ended: drop later classifications and wait for the recordings in flight."""
        self._closed = True
        self._turn_wake.set()
        pending = list(self._recordings)
        if pending:
            await asyncio.wait(pending)

    # time

    def _elapsed_ms(self) -> float:
        return self._clock.monotonic() * 1000.0 - self._origin_ms

    def _in_window(self) -> bool:
        return self._elapsed_ms() <= self._config.voicemail.detect_timeout_ms
