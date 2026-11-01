"""Telnyx Call Control request and response models (specs/06-telephony-telnyx.md §2.1, §6, §10).

Request models forbid unknown fields and serialize with :meth:`RequestModel.body`: aliases (``from``),
``None`` fields omitted, defaults sent, so every body carries exactly the fields of the specs/06 §2.1 row.
Streaming parameters are a nested :class:`StreamParams` flattened into the body. Response models ignore fields
we do not read, so Telnyx can add fields without breaking us.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

__all__ = [
    "AmdConfig",
    "AmdMode",
    "AnswerRequest",
    "CommandResult",
    "Conference",
    "CustomHeader",
    "DialRequest",
    "DialResult",
    "RequestModel",
    "ResponseModel",
    "StreamParams",
    "TransferRequest",
]

type AmdMode = Literal["premium", "detect", "detect_beep", "detect_words", "greeting_end", "disabled"]
type NonEmpty = Annotated[str, StringConstraints(min_length=1)]
type CommandId = Annotated[str, StringConstraints(min_length=1, max_length=255)]
type TimeoutSecs = Annotated[int, Field(ge=5, le=600)]
type TimeLimitSecs = Annotated[int, Field(ge=30, le=14_400)]
type RecordFromAnswer = Literal["record-from-answer"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def body(self) -> dict[str, Any]:
        """The JSON body: aliases applied, ``None`` omitted, nested :class:`StreamParams` flattened."""
        stream = getattr(self, "stream", None)
        body = self.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"stream"})
        if isinstance(stream, StreamParams):
            body.update(stream.body())
            body["stream_bidirectional_target_legs"] = "self"
        return body


class StreamParams(RequestModel):
    """Media streaming parameters (specs/06 §6 step 5). A listen-only stream (specs/06 §10.1 step 4) passes
    ``None`` for the three bidirectional fields."""

    stream_url: Annotated[str, StringConstraints(pattern=r"^wss?://\S+$")]
    stream_track: Literal["inbound_track", "outbound_track", "both_tracks"] = "inbound_track"
    stream_codec: Literal["PCMU", "L16"] = "PCMU"
    stream_bidirectional_mode: Literal["rtp"] | None = "rtp"
    stream_bidirectional_codec: Literal["PCMU", "L16"] | None = "PCMU"
    stream_bidirectional_sampling_rate: Literal[8000, 16000] | None = 8000
    stream_auth_token: str | None = None


class CustomHeader(RequestModel):
    name: NonEmpty
    value: str


# jscpd:ignore-start  (the Telnyx field list; FakeTelnyx declares it on its own, they must not share code)
class AmdConfig(RequestModel):
    """``answering_machine_detection_config`` (specs/06 §6 step 5 sends the premium tuning)."""

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


# jscpd:ignore-end  (field list above)


class DialRequest(RequestModel):
    """``POST /calls``. ``webhook_url`` ``None`` = the Call Control App's URL; ``stream`` ``None`` = no
    media."""

    connection_id: NonEmpty
    to: NonEmpty
    from_: NonEmpty = Field(serialization_alias="from")
    from_display_name: Annotated[str, StringConstraints(max_length=128)] | None = None
    timeout_secs: TimeoutSecs = 45
    time_limit_secs: TimeLimitSecs = 14_400
    answering_machine_detection: AmdMode = "disabled"
    answering_machine_detection_config: AmdConfig | None = None
    client_state: str
    command_id: CommandId
    webhook_url: str | None = None
    webhook_url_method: Literal["POST"] = "POST"
    stream: StreamParams | None = None
    record: RecordFromAnswer | None = None
    record_channels: Literal["single", "dual"] | None = None
    record_format: Literal["wav", "mp3"] | None = None
    record_track: Literal["both", "inbound", "outbound"] | None = None
    record_max_length: Annotated[int, Field(ge=0, le=14_400)] | None = None
    custom_headers: list[CustomHeader] | None = None
    link_to: str | None = None
    bridge_intent: bool | None = None
    bridge_on_answer: bool | None = None
    send_silence_when_idle: Literal[True] = True


class AnswerRequest(RequestModel):
    """``POST /calls/{ccid}/actions/answer``."""

    client_state: str
    command_id: CommandId
    webhook_url: str | None = None
    stream: StreamParams | None = None
    record: RecordFromAnswer | None = None
    record_channels: Literal["single", "dual"] | None = None
    record_format: Literal["wav", "mp3"] | None = None
    send_silence_when_idle: Literal[True] = True


class TransferRequest(RequestModel):
    """``POST /calls/{ccid}/actions/transfer`` (specs/06 §10.1 step 3)."""

    to: NonEmpty
    from_: NonEmpty = Field(serialization_alias="from")
    from_display_name: Annotated[str, StringConstraints(max_length=128)] | None = None
    timeout_secs: TimeoutSecs = 30
    time_limit_secs: TimeLimitSecs
    early_media: Literal[True] = True
    target_leg_client_state: str
    client_state: str
    command_id: CommandId
    webhook_url: str | None = None
    answering_machine_detection: Literal["disabled"] = "disabled"
    custom_headers: list[CustomHeader] | None = None
    record: RecordFromAnswer | None = None


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, coerce_numbers_to_str=True)


class DialResult(ResponseModel):
    """``data`` of a dial response."""

    call_control_id: NonEmpty
    call_leg_id: str
    call_session_id: str
    is_alive: bool = False
    record_type: str = "call"


class CommandResult(ResponseModel):
    """``data`` of a call or conference command response (``{"result": "ok"}``)."""

    result: str


class Conference(ResponseModel):
    """``data`` of ``POST /conferences``."""

    id: NonEmpty
    name: str
    status: str | None = None
    record_type: str = "conference"
