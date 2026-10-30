"""FakeTelnyx call control request models (specs/06-telephony-telnyx.md §2.1; specs/30-testing-and-evals.md
§3.2).

Like every FakeTelnyx body model these forbid unknown fields, so a misspelt field in a client fails a
contract test with ``10015``. Field sets and defaults follow the Telnyx Call Control v2 API for the fields
specs/06 §2.1 sends; ``from`` is exposed as ``from_``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveInt, StringConstraints

from va_sim.telnyx.models import E164, Name, WebhookUrl

__all__ = [
    "AmdConfig",
    "AmdMode",
    "AnswerRequest",
    "BridgeRequest",
    "CallTarget",
    "CallerDtmf",
    "CallerSay",
    "CommandFields",
    "ConferenceCreate",
    "ConferenceJoin",
    "ConferenceLeave",
    "CustomHeader",
    "DialRequest",
    "HangupCause",
    "HangupRequest",
    "InboundCall",
    "InjectFrame",
    "PlaybackStartRequest",
    "PlaybackStopRequest",
    "RecordFields",
    "RecordStartRequest",
    "RecordStopRequest",
    "RejectRequest",
    "RemoteHangup",
    "SendDtmfRequest",
    "StreamFields",
    "StreamingStartRequest",
    "StreamingStopRequest",
    "TransferRequest",
]

type CallTarget = E164 | Annotated[str, StringConstraints(pattern=r"^sip:\S+$")]
type AmdMode = Literal["premium", "detect", "detect_beep", "detect_words", "greeting_end", "disabled"]
type HangupCause = Literal[
    "normal_clearing",
    "originator_cancel",
    "timeout",
    "time_limit",
    "user_busy",
    "not_found",
    "no_answer",
    "call_rejected",
    "unspecified",
]
type ClientState = Annotated[str, StringConstraints(max_length=4096)]
type Channels = Literal["single", "dual"]
type RecordFormat = Literal["wav", "mp3"]
type Seconds = Annotated[int, Field(ge=0, le=14_400)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CommandFields(_Body):
    """``command_id`` (dedup key) and ``client_state`` (replaces the leg's state for later events)."""

    command_id: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    client_state: ClientState | None = None


class StreamFields(_Body):
    """Media streaming parameters of dial, answer, and ``streaming_start``."""

    stream_url: Annotated[str, StringConstraints(pattern=r"^wss?://\S+$")] | None = None
    stream_track: Literal["inbound_track", "outbound_track", "both_tracks"] = "inbound_track"
    stream_codec: Literal["PCMU", "PCMA", "G722", "OPUS", "AMR-WB", "L16", "default"] = "default"
    stream_bidirectional_mode: Literal["mp3", "rtp"] | None = None
    stream_bidirectional_codec: Literal["PCMU", "PCMA", "G722", "OPUS", "AMR-WB", "L16"] | None = None
    stream_bidirectional_sampling_rate: Literal[8000, 16000, 22050, 24000, 48000] | None = None
    stream_bidirectional_target_legs: Literal["both", "self", "opposite"] = "self"
    stream_auth_token: str | None = None
    send_silence_when_idle: bool = False

    def stream_params(self) -> dict[str, object]:
        """The streaming fields as sent (kept on the call for the media WS client)."""
        return self.model_dump(include=set(StreamFields.model_fields))


class RecordFields(_Body):
    """``record: "record-from-answer"`` and its options on dial, answer, and transfer."""

    record: Literal["record-from-answer"] | None = None
    record_channels: Channels = "dual"
    record_format: RecordFormat = "wav"
    record_track: Literal["both", "inbound", "outbound"] = "both"
    record_max_length: Seconds = 0


class CustomHeader(_Body):
    name: Annotated[str, StringConstraints(min_length=1)]
    value: str


class AmdConfig(_Body):
    """``answering_machine_detection_config`` (accepted and stored; detection is scenario driven)."""

    total_analysis_time_millis: PositiveInt | None = None
    after_greeting_silence_millis: PositiveInt | None = None
    between_words_silence_millis: PositiveInt | None = None
    greeting_duration_millis: PositiveInt | None = None
    initial_silence_millis: PositiveInt | None = None
    maximum_number_of_words: PositiveInt | None = None
    maximum_word_length_millis: PositiveInt | None = None
    silence_threshold: PositiveInt | None = None
    greeting_total_analysis_time_millis: PositiveInt | None = None
    greeting_silence_duration_millis: PositiveInt | None = None


class DialRequest(StreamFields, RecordFields, CommandFields):
    """``POST /v2/calls``."""

    connection_id: Annotated[str, StringConstraints(min_length=1)]
    to: CallTarget
    from_: E164 = Field(alias="from")
    from_display_name: Annotated[str, StringConstraints(max_length=128)] | None = None
    timeout_secs: Annotated[int, Field(ge=5, le=600)] = 30
    time_limit_secs: Annotated[int, Field(ge=30, le=14_400)] = 14_400
    answering_machine_detection: AmdMode = "disabled"
    answering_machine_detection_config: AmdConfig | None = None
    webhook_url: WebhookUrl | None = None
    webhook_url_method: Literal["POST", "GET"] = "POST"
    custom_headers: list[CustomHeader] = Field(default_factory=list)
    link_to: str | None = None
    bridge_intent: bool = False
    bridge_on_answer: bool = False


class AnswerRequest(StreamFields, RecordFields, CommandFields):
    webhook_url: WebhookUrl | None = None
    webhook_url_method: Literal["POST", "GET"] = "POST"


class RejectRequest(CommandFields):
    cause: Literal["CALL_REJECTED", "USER_BUSY"]


class HangupRequest(CommandFields):
    """``hangup``: only the command fields."""


class TransferRequest(CommandFields):
    to: CallTarget
    from_: E164 | None = Field(default=None, alias="from")
    from_display_name: Annotated[str, StringConstraints(max_length=128)] | None = None
    timeout_secs: Annotated[int, Field(ge=5, le=600)] = 30
    time_limit_secs: Annotated[int, Field(ge=30, le=14_400)] = 14_400
    early_media: bool = True
    target_leg_client_state: ClientState | None = None
    webhook_url: WebhookUrl | None = None
    webhook_url_method: Literal["POST", "GET"] = "POST"
    answering_machine_detection: AmdMode = "disabled"
    custom_headers: list[CustomHeader] = Field(default_factory=list)
    record: Literal["record-from-answer"] | None = None


class BridgeRequest(CommandFields):
    call_control_id: Annotated[str, StringConstraints(min_length=1)]
    park_after_unbridge: Literal["self"] | None = None


class SendDtmfRequest(CommandFields):
    digits: Annotated[str, StringConstraints(pattern=r"^[0-9A-Da-d*#wW]+$")]
    duration_millis: Annotated[int, Field(ge=100, le=500)] = 250


class RecordStartRequest(CommandFields):
    format: RecordFormat = "wav"
    channels: Channels = "dual"
    play_beep: bool = False
    max_length: Seconds = 0
    timeout_secs: Seconds = 0
    trim: Literal["trim-silence"] | None = None


class RecordStopRequest(CommandFields):
    """``record_stop``: only the command fields."""


class StreamingStartRequest(StreamFields, CommandFields):
    """``streaming_start``: the streaming parameters and command fields."""


class StreamingStopRequest(CommandFields):
    stream_id: str | None = None


class PlaybackStartRequest(CommandFields):
    audio_url: WebhookUrl
    loop: PositiveInt | Literal["infinity"] = "infinity"
    overlay: bool = False
    target_legs: Literal["self", "opposite", "both"] = "self"


class PlaybackStopRequest(CommandFields):
    stop: Literal["all", "current"] = "all"
    overlay: bool = False


class ConferenceCreate(CommandFields):
    """``POST /v2/conferences``."""

    call_control_id: Annotated[str, StringConstraints(min_length=1)]
    name: Name
    beep_enabled: Literal["always", "never", "on_enter", "on_exit"] = "never"
    start_conference_on_create: bool = True
    comfort_noise: bool | None = None


class ConferenceJoin(CommandFields):
    call_control_id: Annotated[str, StringConstraints(min_length=1)]
    start_conference_on_enter: bool = True
    end_conference_on_exit: bool = False
    hold: bool = False
    mute: bool = False


class ConferenceLeave(_Body):
    call_control_id: Annotated[str, StringConstraints(min_length=1)]
    command_id: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None


class InboundCall(_Body):
    """``POST /sim/telnyx/inbound``: a call from ``from`` to an owned number."""

    to: E164
    from_: E164 = Field(alias="from")
    scenario: str | None = None


class RemoteHangup(_Body):
    """``POST /sim/telnyx/calls/{ccid}/hangup``: the remote party hangs up."""

    cause: HangupCause = "normal_clearing"


class CallerSay(_Body):
    """``POST /sim/telnyx/calls/{ccid}/say``: the caller speaks ``text`` (synthetic marker audio)."""

    text: Annotated[str, StringConstraints(pattern=r"\S", max_length=2000)]
    language: Annotated[str, StringConstraints(min_length=2, max_length=16)] = "en"


class CallerDtmf(_Body):
    """``POST /sim/telnyx/calls/{ccid}/dtmf``: the caller presses ``digits`` (media ``dtmf`` events)."""

    digits: Annotated[str, StringConstraints(pattern=r"^[0-9A-D*#]+$", max_length=64)]


class InjectFrame(_Body):
    """``POST /sim/telnyx/calls/{ccid}/media/inject``: a frame sent verbatim on the media WebSocket."""

    frame: dict[str, JsonValue]
