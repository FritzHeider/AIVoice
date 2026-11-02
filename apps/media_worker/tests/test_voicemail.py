"""The voicemail monitor (specs/07 §14, specs/10 §9.4; Decision P05-T10c), on a :class:`ManualClock`."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from clock_helpers import ManualClock
from media_helpers import call_context
from pydantic import BaseModel
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError

from va_events import DomainEvent, EventPublishError, EventType, new_event
from va_llm import ChatMessage, LlmUnavailableError
from va_media.voicemail import (
    CLASSIFIER_DESCRIPTION,
    VOICEMAIL_MESSAGE,
    AnswerClass,
    LlmAnswerClassifier,
    VoicemailMonitor,
)
from va_voice.pipeline import AgentTurn, InterruptRecord, Takeover, UserTurn

MESSAGE_TEMPLATE = "Hi {{name}}, please call us back."
PRIVATE_WORDS = "my account number is seven seven"


@dataclass
class Takeovers:
    accepted: bool = True
    seen: list[Takeover] = field(default_factory=list)

    def __call__(self, takeover: Takeover) -> bool:
        self.seen.append(takeover)
        return self.accepted


@dataclass
class ScriptedClassifier:
    """Answers from ``answers`` in order (an exception is raised); ``gate`` holds each request when set."""

    answers: list[AnswerClass | Exception] = field(default_factory=list)
    calls: list[tuple[str, float]] = field(default_factory=list)
    gate: asyncio.Event | None = None

    async def classify(self, transcript: str, seconds: float) -> AnswerClass:
        self.calls.append((transcript, seconds))
        if self.gate is not None:
            await self.gate.wait()
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class RecordingEvents:
    published: list[DomainEvent] = field(default_factory=list)
    fail: Exception | None = None

    async def publish(self, event: DomainEvent) -> DomainEvent:
        if self.fail is not None:
            raise self.fail
        self.published.append(event)
        return event


@dataclass
class RecordingAnsweredBy:
    calls: list[uuid.UUID] = field(default_factory=list)
    fail: Exception | None = None

    async def set_voicemail(self, call_id: uuid.UUID) -> None:
        if self.fail is not None:
            raise self.fail
        self.calls.append(call_id)


@dataclass
class Rig:
    clock: ManualClock
    monitor: VoicemailMonitor
    takeovers: Takeovers
    classifier: ScriptedClassifier
    events: RecordingEvents
    answered_by: RecordingAnsweredBy
    beep: asyncio.Event
    call_id: uuid.UUID


def rig(
    *,
    direction: str = "outbound",
    answered_ago_s: float | None = None,
    sms: Callable[[], Awaitable[None]] | None = None,
    **config: Any,
) -> Rig:
    clock = ManualClock()
    context = call_context(uuid.uuid4(), **config).model_copy(
        update={"direction": direction, "variables": {"name": "Dana"}}
    )
    takeovers, classifier = Takeovers(), ScriptedClassifier()
    events, answered_by, beep = RecordingEvents(), RecordingAnsweredBy(), asyncio.Event()
    answered_at = None if answered_ago_s is None else clock.now() - timedelta(seconds=answered_ago_s)
    monitor = VoicemailMonitor(
        context=context,
        beep=beep,
        clock=clock,
        classifier=classifier,
        events=events,
        answered_by=answered_by,
        answered_at=answered_at,
        sms=sms,
    )
    monitor.bind(takeovers)
    return Rig(clock, monitor, takeovers, classifier, events, answered_by, beep, context.call_id)


def voicemail(
    action: str, *, sensitive: bool = False, message: str | None = MESSAGE_TEMPLATE
) -> dict[str, Any]:
    return {
        "voicemail": {
            "action": action,
            "sensitive": sensitive,
            "message": message,
            "detect_timeout_ms": 10_000,
        }
    }


def turn(index: int, text: str, *, dtmf: str | None = None) -> UserTurn:
    return UserTurn(index, text, 0.0, 100.0, dtmf=dtmf)


@contextlib.asynccontextmanager
async def running(r: Rig) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.ensure_future(r.monitor.run())
    try:
        yield task
    finally:
        await r.monitor.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def event_data(r: Rig) -> list[dict[str, Any]]:
    return [dict(event.data) for event in r.events.published]


async def test_a_machine_verdict_with_hangup_hangs_up() -> None:
    r = rig(**voicemail("hangup"))
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    (event,) = r.events.published
    assert (event.type, event.call_id, event.message) == (
        EventType.CALL_VOICEMAIL,
        r.call_id,
        VOICEMAIL_MESSAGE,
    )
    assert event.data == {"action": "hangup", "trigger": "amd"}
    # The edge stores answered_by for an AMD verdict.
    assert r.answered_by.calls == []
    assert r.monitor.detected


@pytest.mark.parametrize("action", ["leave_message", "leave_message_and_sms"])
async def test_a_machine_verdict_leaves_the_rendered_message_after_the_beep(action: str) -> None:
    stream = json_log_stream()
    r = rig(**voicemail(action))
    r.monitor.start("machine")
    await r.monitor.stop()
    (takeover,) = r.takeovers.seen
    assert takeover.reason == "voicemail_left"
    assert takeover.text == "Hi Dana, please call us back."
    assert takeover.beep is r.beep
    assert event_data(r) == [{"action": action, "trigger": "amd"}]
    assert takeover.after_message is None
    events = [e["event"] for e in log_events(stream)]
    assert ("media.voicemail_sms_unavailable" in events) is (action == "leave_message_and_sms")
    assert "Dana" not in stream.getvalue()


async def test_leave_message_and_sms_hands_the_sms_hook_to_the_takeover() -> None:
    sent: list[str] = []

    async def send_sms() -> None:
        sent.append("sms")

    stream = json_log_stream()
    r = rig(sms=send_sms, **voicemail("leave_message_and_sms"))
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    (takeover,) = r.takeovers.seen
    assert (takeover.reason, takeover.text) == ("voicemail_left", "Hi Dana, please call us back.")
    assert takeover.after_message is send_sms
    assert sent == []
    assert "media.voicemail_sms_unavailable" not in [e["event"] for e in log_events(stream)]


async def test_the_sms_hook_is_not_used_for_leave_message() -> None:
    async def send_sms() -> None:
        raise AssertionError

    r = rig(sms=send_sms, **voicemail("leave_message"))
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    (takeover,) = r.takeovers.seen
    assert takeover.after_message is None


@pytest.mark.parametrize("message", [None, "   "])
async def test_a_blank_message_hangs_up(message: str | None) -> None:
    stream = json_log_stream()
    r = rig(**voicemail("leave_message", message=message))
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    assert event_data(r) == [{"action": "leave_message", "trigger": "amd"}]
    assert "media.voicemail_message_missing" in [e["event"] for e in log_events(stream)]


async def test_ignore_records_the_detection_and_lets_the_conversation_go_on() -> None:
    r = rig(**voicemail("ignore", sensitive=True))
    async with running(r) as task:
        r.monitor.on_verdict("machine")
        r.monitor.on_user_turn(turn(1, "You have reached Dana"))
        await settle()
        assert task.done()
    assert r.takeovers.seen == []
    assert r.classifier.calls == []
    assert event_data(r) == [{"action": "ignore", "trigger": "amd"}]


async def test_a_fax_ends_the_call_silently() -> None:
    r = rig(**voicemail("leave_message", sensitive=True))
    r.monitor.on_verdict("fax")
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    assert r.takeovers.seen == [Takeover("fax_detected")]
    assert r.events.published == []
    assert r.answered_by.calls == []


@pytest.mark.parametrize("verdict", ["human", "unknown"])
async def test_human_and_unknown_verdicts_change_nothing(verdict: str) -> None:
    r = rig(**voicemail("hangup"))
    r.monitor.on_verdict(verdict)  # type: ignore[arg-type]
    await r.monitor.stop()
    assert (r.takeovers.seen, r.events.published, r.monitor.detected) == ([], [], False)


async def test_a_verdict_after_the_window_is_ignored() -> None:
    stream = json_log_stream()
    r = rig(answered_ago_s=10.5, **voicemail("hangup"))
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    assert r.takeovers.seen == []
    assert "media.voicemail_late" in [e["event"] for e in log_events(stream)]


async def test_the_window_counts_from_the_monitor_without_an_answer_time() -> None:
    r = rig(**voicemail("hangup"))
    await r.clock.run_for(9.9)
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]


async def test_ivr_mode_never_acts() -> None:
    r = rig(ivr_mode=True, **voicemail("hangup", sensitive=True))
    r.monitor.start("machine")
    r.monitor.on_verdict("fax")
    await r.monitor.stop()
    assert r.takeovers.seen == []
    assert not r.monitor.classifying


async def test_a_confident_voicemail_classification_acts() -> None:
    r = rig(answered_ago_s=2.0, **voicemail("hangup", sensitive=True))
    r.classifier.answers = [AnswerClass(label="voicemail", confidence=0.93)]
    async with running(r) as task:
        await r.clock.run_for(1.5)
        r.monitor.on_user_turn(turn(1, "You have reached Dana, leave a message"))
        await settle()
        assert task.done()
    assert r.classifier.calls == [("You have reached Dana, leave a message", 3.5)]
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    assert r.answered_by.calls == [r.call_id]
    assert event_data(r) == [{"action": "hangup", "trigger": "classifier", "confidence": 0.93}]


async def test_an_unsure_classification_is_not_acted_on() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.answers = [
        AnswerClass(label="voicemail", confidence=0.79),
        AnswerClass(label="unknown", confidence=0.95),
    ]
    async with running(r):
        r.monitor.on_user_turn(turn(1, "Hi"))
        await settle()
        r.monitor.on_user_turn(turn(2, "there"))
        await settle()
        assert r.monitor.classifying
    assert [call[0] for call in r.classifier.calls] == ["Hi", "Hi there"]
    assert (r.takeovers.seen, r.events.published) == ([], [])


@pytest.mark.parametrize("label", ["human", "ivr"])
async def test_human_or_ivr_stops_the_classification(label: str) -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.answers = [AnswerClass(label=label, confidence=0.9)]  # type: ignore[arg-type]
    async with running(r) as task:
        r.monitor.on_user_turn(turn(1, "Hello?"))
        await settle()
        assert task.done()
        r.monitor.on_user_turn(turn(2, "Leave a message after the tone"))
        await settle()
    assert len(r.classifier.calls) == 1
    assert not r.monitor.classifying
    assert r.takeovers.seen == []


async def test_a_classifier_failure_waits_for_the_next_turn() -> None:
    stream = json_log_stream()
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.answers = [
        LlmUnavailableError("exhausted", provider_id="platform", model="m", last=None),
        AnswerClass(label="voicemail", confidence=0.9),
    ]
    async with running(r):
        r.monitor.on_user_turn(turn(1, "The person you are calling"))
        await settle()
        assert r.takeovers.seen == []
        r.monitor.on_user_turn(turn(2, "is not available"))
        await settle()
    assert [call[0] for call in r.classifier.calls] == [
        "The person you are calling",
        "The person you are calling is not available",
    ]
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    [failed] = [e for e in log_events(stream) if e["event"] == "media.answer_classifier_failed"]
    assert failed["error_type"] == "LlmUnavailableError"


async def test_one_request_in_flight_and_one_rerun_with_the_whole_transcript() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.gate = asyncio.Event()
    r.classifier.answers = [
        AnswerClass(label="unknown", confidence=0.2),
        AnswerClass(label="unknown", confidence=0.2),
    ]
    async with running(r):
        r.monitor.on_user_turn(turn(1, "one"))
        await settle()
        r.monitor.on_user_turn(turn(2, "two"))
        r.monitor.on_user_turn(turn(3, "three"))
        await settle()
        assert len(r.classifier.calls) == 1
        r.classifier.gate.set()
        await settle()
    assert [call[0] for call in r.classifier.calls] == ["one", "one two three"]


async def test_a_result_after_an_amd_detection_is_dropped() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.gate = asyncio.Event()
    r.classifier.answers = [AnswerClass(label="voicemail", confidence=0.99)]
    async with running(r):
        r.monitor.on_user_turn(turn(1, "Hello"))
        await settle()
        r.monitor.on_verdict("machine")
        r.classifier.gate.set()
        await settle()
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    assert event_data(r) == [{"action": "hangup", "trigger": "amd"}]
    assert r.answered_by.calls == []


async def test_a_result_after_the_window_is_late() -> None:
    stream = json_log_stream()
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.gate = asyncio.Event()
    r.classifier.answers = [AnswerClass(label="voicemail", confidence=0.99)]
    async with running(r) as task:
        await r.clock.run_for(9.0)
        r.monitor.on_user_turn(turn(1, "Hello"))
        await settle()
        await r.clock.run_for(2.0)
        r.classifier.gate.set()
        await settle()
        assert task.done()
    assert r.takeovers.seen == []
    assert "media.voicemail_late" in [e["event"] for e in log_events(stream)]


async def test_turns_after_the_window_close_the_classification() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    async with running(r) as task:
        await r.clock.run_for(10.5)
        r.monitor.on_user_turn(turn(1, "Hello"))
        await settle()
        assert task.done()
    assert r.classifier.calls == []
    assert not r.monitor.classifying


async def test_keypad_and_blank_turns_are_not_classified() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    async with running(r):
        r.monitor.on_user_turn(turn(1, "[DTMF: 1]", dtmf="1"))
        r.monitor.on_user_turn(turn(2, "   "))
        await settle()
    assert r.classifier.calls == []


async def test_a_result_after_the_call_ended_is_dropped() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.gate = asyncio.Event()
    r.classifier.answers = [AnswerClass(label="voicemail", confidence=0.99)]
    task = asyncio.ensure_future(r.monitor.run())
    r.monitor.on_user_turn(turn(1, "Hello"))
    await settle()
    await r.monitor.stop()
    r.classifier.gate.set()
    await asyncio.wait_for(task, 5)
    assert r.takeovers.seen == []


@pytest.mark.parametrize(("direction", "sensitive"), [("outbound", False), ("inbound", True)])
async def test_the_classifier_runs_only_for_sensitive_outbound_calls(direction: str, sensitive: bool) -> None:
    r = rig(direction=direction, **voicemail("hangup", sensitive=sensitive))
    assert not r.monitor.classifying
    async with running(r) as task:
        await settle()
        # Nothing to classify: the classifier task returns at once.
        assert task.done()
        r.monitor.on_user_turn(turn(1, "Leave a message"))
        await settle()
    assert r.classifier.calls == []


async def test_recording_failures_are_logged_and_the_takeover_stands() -> None:
    stream = json_log_stream()
    r = rig(**voicemail("hangup", sensitive=True))
    r.classifier.answers = [AnswerClass(label="voicemail", confidence=0.9)]
    r.answered_by.fail = OperationalError("UPDATE calls", {}, ConnectionError("down"))
    r.events.fail = EventPublishError(
        new_event(EventType.CALL_VOICEMAIL, org_id=uuid.uuid4(), clock=r.clock), "stream"
    )
    async with running(r):
        r.monitor.on_user_turn(turn(1, PRIVATE_WORDS))
        await settle()
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    failed = [e for e in log_events(stream) if e["event"] == "media.voicemail_record_failed"]
    assert [(e["target"], e["error_type"]) for e in failed] == [
        ("answered_by", "OperationalError"),
        ("event", "EventPublishError"),
    ]
    assert "seven" not in stream.getvalue()


async def test_event_data_never_carries_the_transcript() -> None:
    r = rig(**voicemail("leave_message", sensitive=True))
    r.classifier.answers = [AnswerClass(label="voicemail", confidence=0.9)]
    async with running(r):
        r.monitor.on_user_turn(turn(1, PRIVATE_WORDS))
        await settle()
    (event,) = r.events.published
    assert "seven" not in event.model_dump_json()
    assert event.message == VOICEMAIL_MESSAGE


async def test_a_refused_takeover_is_still_recorded() -> None:
    r = rig(**voicemail("hangup"))
    r.takeovers.accepted = False
    r.monitor.on_verdict("machine")
    await r.monitor.stop()
    assert r.takeovers.seen == [Takeover("voicemail_hangup")]
    assert len(r.events.published) == 1


async def test_an_unbound_monitor_logs_instead_of_taking_over() -> None:
    stream = json_log_stream()
    clock = ManualClock()
    context = call_context(uuid.uuid4(), **voicemail("hangup"))
    monitor = VoicemailMonitor(context=context, beep=asyncio.Event(), clock=clock)
    monitor.on_verdict("machine")
    await monitor.stop()
    assert monitor.detected
    assert "media.voicemail_unbound" in [e["event"] for e in log_events(stream)]


def test_observer_callbacks_other_than_user_turns_are_ignored() -> None:
    r = rig(**voicemail("hangup", sensitive=True))
    r.monitor.on_agent_turn(AgentTurn(0, "Hi", "Hi", interrupted=False, started_ms=0.0, ended_ms=1.0))
    r.monitor.on_interrupt(InterruptRecord(0, 1.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1, 2, 1, "words"))
    assert r.classifier.calls == []
    assert r.monitor.classifying


@dataclass
class RecordingGateway:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call_structured[M: BaseModel](self, call_type: str, schema: type[M], **kwargs: Any) -> M:
        self.calls.append({"call_type": call_type, "schema": schema, **kwargs})
        return schema.model_validate({"label": "voicemail", "confidence": 0.9})


async def test_the_llm_classifier_sends_the_rendered_prompt() -> None:
    gateway = RecordingGateway()
    org_id, call_id = uuid.uuid4(), uuid.uuid4()
    classifier = LlmAnswerClassifier(gateway, org_id=org_id, call_id=call_id)
    result = await classifier.classify("You've reached Dana", 4.2)
    assert result == AnswerClass(label="voicemail", confidence=0.9)
    (call,) = gateway.calls
    assert call["call_type"] == "voicemail"
    assert call["schema"] is AnswerClass
    assert (call["org_id"], call["call_id"]) == (org_id, call_id)
    assert (call["function_name"], call["description"]) == ("classify_answer", CLASSIFIER_DESCRIPTION)
    (message,) = call["messages"]
    assert isinstance(message, ChatMessage)
    assert message.role == "system"
    assert message.content is not None
    assert 'Transcript so far: "You\'ve reached Dana"' in message.content
    assert "Seconds since answer: 4" in message.content
