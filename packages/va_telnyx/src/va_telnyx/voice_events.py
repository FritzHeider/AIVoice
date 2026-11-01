"""Telnyx voice webhook payload models (specs/06-telephony-telnyx.md §5; specs/06 §4.1 Decision P03-T09 rule
3).

One model per ``event_type`` of the specs/06 §5 table. Models ignore fields we do not read and default every
field Telnyx may omit, so a new or missing optional field never breaks webhook processing. ``direction`` and
the result/cause fields stay plain strings: Telnyx adds values over time and the edge maps unknown ones itself
(specs/06 §5.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from pydantic import ConfigDict, Field

from va_telnyx.models import ResponseModel

__all__ = [
    "VOICE_PAYLOAD_MODELS",
    "CallAnswered",
    "CallBridged",
    "CallHangup",
    "CallHold",
    "CallInitiated",
    "CallPayload",
    "CallScreeningDetected",
    "CallUnhold",
    "ConferencePayload",
    "ConversationCreated",
    "ConversationStartFailed",
    "CustomHeaderValue",
    "DeepfakeError",
    "DeepfakeResult",
    "DtmfReceived",
    "MachineDetectionEnded",
    "MachineGreetingEnded",
    "PlaybackEnded",
    "PlaybackStarted",
    "PremiumDetectionEnded",
    "PremiumGreetingEnded",
    "RecordingError",
    "RecordingSaved",
    "RecordingUrls",
    "StreamingFailed",
    "StreamingStarted",
    "StreamingStopped",
    "VoicePayload",
]


class _Payload(ResponseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, coerce_numbers_to_str=True, populate_by_name=True)


class CustomHeaderValue(_Payload):
    name: str
    value: str | None = None


class CallPayload(_Payload):
    """The fields every call-leg event carries (``from_`` is the ``from`` number)."""

    call_control_id: str
    call_leg_id: str | None = None
    call_session_id: str | None = None
    connection_id: str | None = None
    client_state: str | None = None
    from_: str | None = Field(None, validation_alias="from")
    to: str | None = None
    direction: str | None = None
    state: str | None = None
    start_time: datetime | None = None
    custom_headers: tuple[CustomHeaderValue, ...] = ()


class CallInitiated(CallPayload):
    """``call.initiated``: ``direction`` is ``incoming`` (inbound router) or ``outgoing``."""

    caller_id_name: str | None = None


class CallAnswered(CallPayload):
    """``call.answered``."""


class CallBridged(CallPayload):
    """``call.bridged``."""


class CallHangup(CallPayload):
    """``call.hangup`` (causes are stored verbatim, specs/06 §5.1)."""

    hangup_cause: str | None = None
    hangup_source: str | None = None
    sip_hangup_cause: str | None = None
    end_time: datetime | None = None


class _AmdResult(CallPayload):
    result: str | None = None


class MachineDetectionEnded(_AmdResult):
    """``call.machine.detection.ended``: ``human`` | ``machine`` | ``not_sure``."""


class MachineGreetingEnded(_AmdResult):
    """``call.machine.greeting.ended``."""


class PremiumDetectionEnded(_AmdResult):
    """``call.machine.premium.detection.ended``: ``human_residence`` | ``human_business`` | ``machine`` |
    ``silence`` | ``fax_detected``."""


class PremiumGreetingEnded(_AmdResult):
    """``call.machine.premium.greeting.ended`` (``beep_detected`` when the beep was heard)."""


class CallScreeningDetected(_AmdResult):
    """``call.machine.premium.call_screening.detected``."""


class DtmfReceived(CallPayload):
    """``call.dtmf.received``."""

    digit: str


class RecordingUrls(_Payload):
    wav: str | None = None
    mp3: str | None = None


class RecordingSaved(CallPayload):
    """``call.recording.saved``: the copy flow (specs/06 §11) downloads ``recording_urls.wav``."""

    recording_id: str | None = None
    channels: str | None = None
    format: str | None = None
    recording_started_at: datetime | None = None
    recording_ended_at: datetime | None = None
    recording_urls: RecordingUrls = RecordingUrls()
    public_recording_urls: RecordingUrls = RecordingUrls()


class RecordingError(CallPayload):
    """``call.recording.error``."""

    reason: str | None = None


class _Playback(CallPayload):
    media_url: str | None = None
    media_name: str | None = None
    overlay: bool | None = None


class PlaybackStarted(_Playback):
    """``call.playback.started``."""


class PlaybackEnded(_Playback):
    """``call.playback.ended`` (``status`` ``completed`` | ``cancelled`` | ``call_hangup`` | ...)."""

    status: str | None = None
    status_detail: str | None = None


class _Streaming(CallPayload):
    stream_url: str | None = None


class StreamingStarted(_Streaming):
    """``streaming.started``."""


class StreamingStopped(_Streaming):
    """``streaming.stopped``."""


class StreamingFailed(_Streaming):
    """``streaming.failed``."""

    stream_id: str | None = None
    failure_reason: str | None = None


class CallHold(CallPayload):
    """``call.hold``."""


class CallUnhold(CallPayload):
    """``call.unhold``."""


class DeepfakeResult(CallPayload):
    """``call.deepfake_detection.result`` (stored only)."""

    result: str | None = None


class DeepfakeError(CallPayload):
    """``call.deepfake_detection.error`` (stored only)."""

    error: str | None = None


class ConversationCreated(CallPayload):
    """``call.conversation.created`` (stored only)."""

    conversation_id: str | None = None


class ConversationStartFailed(CallPayload):
    """``call.conversation.start_failed`` (stored only)."""

    conversation_id: str | None = None


class ConferencePayload(_Payload):
    """``conference.*`` events; the call fields name the participant leg when there is one."""

    conference_id: str
    call_control_id: str | None = None
    call_leg_id: str | None = None
    call_session_id: str | None = None
    connection_id: str | None = None
    client_state: str | None = None
    reason: str | None = None


type VoicePayload = CallPayload | ConferencePayload

VOICE_PAYLOAD_MODELS: Final[Mapping[str, type[CallPayload] | type[ConferencePayload]]] = {
    "call.initiated": CallInitiated,
    "call.answered": CallAnswered,
    "call.bridged": CallBridged,
    "call.hangup": CallHangup,
    "call.machine.detection.ended": MachineDetectionEnded,
    "call.machine.greeting.ended": MachineGreetingEnded,
    "call.machine.premium.detection.ended": PremiumDetectionEnded,
    "call.machine.premium.greeting.ended": PremiumGreetingEnded,
    "call.machine.premium.call_screening.detected": CallScreeningDetected,
    "call.dtmf.received": DtmfReceived,
    "call.recording.saved": RecordingSaved,
    "call.recording.error": RecordingError,
    "call.playback.started": PlaybackStarted,
    "call.playback.ended": PlaybackEnded,
    "streaming.started": StreamingStarted,
    "streaming.stopped": StreamingStopped,
    "streaming.failed": StreamingFailed,
    "conference.created": ConferencePayload,
    "conference.participant.joined": ConferencePayload,
    "conference.participant.left": ConferencePayload,
    "conference.ended": ConferencePayload,
    "call.hold": CallHold,
    "call.unhold": CallUnhold,
    "call.deepfake_detection.result": DeepfakeResult,
    "call.deepfake_detection.error": DeepfakeError,
    "call.conversation.created": ConversationCreated,
    "call.conversation.start_failed": ConversationStartFailed,
}
