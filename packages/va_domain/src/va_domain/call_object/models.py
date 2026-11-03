"""Call Object contract models (specs/19-events-and-webhooks.md §4; Decision P05-T16a).

Field names are Bland's (``from``, ``c_id``, ``call_length``, ...). :class:`CallListItem` is the
``GET /v1/calls`` item (specs/05 §6): the Call Object minus :data:`LIST_EXCLUDED_FIELDS`. :class:`CallObject`
extends it with those fields, so both share one field definition. Serialize with
``model_dump(mode="json", by_alias=True)``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PlainSerializer, WithJsonSchema

__all__ = [
    "LIST_EXCLUDED_FIELDS",
    "BlandAnsweredBy",
    "BlandCallEndedBy",
    "BlandCallStatus",
    "CallCitation",
    "CallLatency",
    "CallListItem",
    "CallObject",
    "EvalSummary",
    "GuardRailEntry",
    "PathwayLog",
    "PathwayTag",
    "Transcript",
    "TranscriptRole",
    "VoicemailSummary",
    "WarmTransferCall",
]

type BlandCallStatus = Literal[
    "completed", "failed", "no-answer", "busy", "canceled", "in-progress", "queued", "scheduled"
]
type BlandAnsweredBy = Literal["human", "voicemail", "ivr", "unknown", "no-answer", "fax"]
type BlandCallEndedBy = Literal["ASSISTANT", "USER", "SYSTEM", "API", "GUARD_RAIL", "TIMEOUT", "ERROR"]
type TranscriptRole = Literal["user", "assistant", "agent-action", "human_agent"]

LIST_EXCLUDED_FIELDS: Final = frozenset(
    {"transcripts", "concatenated_transcript", "pathway_logs", "variables", "citations", "evals", "analysis"}
)
"""Call Object fields that only the detail endpoint returns (specs/05 §6)."""

_EXAMPLE_CALL_ID: Final = "0193c5d2-7b1e-7a10-9a52-1f3b0d4e6a77"
_EXAMPLE_ORG_ID: Final = "0193c5d0-11aa-7b20-8c31-5e2f7a9b0c11"
_EXAMPLE_TIME: Final = "2026-10-24T18:22:31.412000Z"

_LATENCY_EXAMPLE: Final[dict[str, JsonValue]] = {
    "p50_e2e_ms": 742,
    "p95_e2e_ms": 1180,
    "avg_llm_ttft_ms": 310,
    "avg_tts_ttfa_ms": 160,
    "turns": 6,
}
_LIST_ITEM_EXAMPLE: Final[dict[str, JsonValue]] = {
    "call_id": _EXAMPLE_CALL_ID,
    "c_id": _EXAMPLE_CALL_ID,
    "org_id": _EXAMPLE_ORG_ID,
    "external_id": None,
    "batch_id": None,
    "to": "+14155550123",
    "from": "+14155550999",
    "inbound": False,
    "direction": "outbound",
    "test_call": False,
    "created_at": "2026-10-24T18:20:01.000000Z",
    "started_at": "2026-10-24T18:20:09.000000Z",
    "end_at": "2026-10-24T18:22:30.000000Z",
    "call_length": 2.35,
    "corrected_duration": "141",
    "completed": True,
    "status": "completed",
    "queue_status": "complete",
    "answered_by": "human",
    "call_ended_by": "ASSISTANT",
    "end_reason": "agent_end_call",
    "max_duration": 30,
    "record": True,
    "recording_url": "https://va-recordings.s3.amazonaws.com/0193c5d0/2026/10/0193c5d2.wav?X-Amz-Signature=…",
    "recording_expiration": "2026-10-31T18:25:00.000000Z",
    "request_data": {"first_name": "Mike"},
    "metadata": {"crm_id": "42"},
    "pathway_id": None,
    "pathway_version": None,
    "persona_id": None,
    "persona_version": None,
    "agent_id": None,
    "agent_version": None,
    "experiment_id": None,
    "experiment_arm": None,
    "summary": "The caller booked a demo for Tuesday.",
    "disposition_tag": "INTERESTED",
    "analysis_schema": {"demo_booked": "boolean"},
    "price": 0.282,
    "transferred_to": None,
    "transfer_duration": 0,
    "local_dialing": False,
    "error_message": None,
    "latency": _LATENCY_EXAMPLE,
    "voicemail": None,
    "warm_transfer_calls": [],
    "guard_rails": [],
}
_CALL_OBJECT_EXAMPLE: Final[dict[str, JsonValue]] = {
    **_LIST_ITEM_EXAMPLE,
    "variables": {"now_utc": "2026-10-24T18:20:09Z", "call_id": _EXAMPLE_CALL_ID, "first_name": "Mike"},
    "pathway_logs": [],
    "transcripts": [
        {
            "id": 1,
            "user": "assistant",
            "text": "Hi Mike, this is Ava from Acme.",
            "created_at": _EXAMPLE_TIME,
            "start_ms": 420,
            "end_ms": 2310,
            "interrupted": False,
        },
        {
            "id": 2,
            "user": "user",
            "text": "Hi, yes.",
            "created_at": _EXAMPLE_TIME,
            "start_ms": 2900,
            "end_ms": 3550,
            "interrupted": False,
        },
    ],
    "concatenated_transcript": "assistant: Hi Mike, this is Ava from Acme.\n user: Hi, yes.\n",
    "analysis": {"demo_booked": True},
    "citations": [],
    "evals": None,
}


def _model_config(example: dict[str, JsonValue]) -> ConfigDict:
    return ConfigDict(populate_by_name=True, json_schema_extra={"examples": [example]})


def _log_time(value: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC (specs/11 §12); naive values are read as UTC."""
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


class PathwayTag(BaseModel):
    name: str | None = Field(description="Name of the node that produced the turn.")
    color: str | None = Field(description="Tag colour of the route taken into the node (`#RRGGBB`).")


class PathwayLog(BaseModel):
    """One assistant turn of a pathway call (specs/11 §12); every key present, `null` when not applicable."""

    tag: PathwayTag = Field(description="Node name and route tag colour.")
    node_id: str = Field(description="Node that spoke the turn.")
    pathway_id: uuid.UUID | None = Field(description="Pathway of the call.")
    pathway_version: int | None = Field(description="Pathway version of the call.")
    role: Literal["assistant"] = Field(description="Always `assistant`.")
    text: str = Field(description="What the agent said.")
    user_text: str | None = Field(description="The caller turn the agent answered.")
    decision: dict[str, Any] | None = Field(
        description=(
            "The turn's decisions: `route` (`from`, `to`, `label`, `alternatives`), `global`,"
            " `loop_condition` (`score`, `met`), `extracted` variables, and `tools` (`name`, `status`, `ms`)."
        )
    )
    created_at: Annotated[
        datetime,
        PlainSerializer(_log_time, return_type=str, when_used="json"),
        WithJsonSchema({"type": "string", "format": "date-time"}),
    ] = Field(description="When the turn was recorded (UTC, milliseconds).")


class Transcript(BaseModel):
    id: int = Field(description="1-based position of the turn in the call.")
    user: TranscriptRole = Field(description="Speaker of the turn.")
    text: str = Field(description="Transcribed or spoken text.")
    created_at: datetime = Field(description="When the turn was recorded (UTC).")
    start_ms: int = Field(description="Start, in milliseconds after the call was answered.")
    end_ms: int = Field(description="End, in milliseconds after the call was answered.")
    interrupted: bool = Field(description="Whether the caller interrupted the agent during this turn.")


class CallCitation(BaseModel):
    citation_schema_id: uuid.UUID = Field(description="Citation schema that requested the variable.")
    variable_name: str = Field(description="Extracted variable.")
    variable_type: str = Field(description="Declared variable type.")
    value: Any = Field(description="Extracted value (`null` when not found).")
    conditions_met: bool | None = Field(description="Whether the schema's conditions held.")
    cited_utterances: list[Any] = Field(description="Utterances that support the value.")


class EvalSummary(BaseModel):
    eval_run_id: uuid.UUID = Field(description="Latest eval run of the call.")
    overall_score: float | None = Field(description="Overall score (0-100) once graded.")
    passed: bool | None = Field(description="Whether the call passed the eval setup.")
    results: list[Any] = Field(description="Per-criterion results.")


class CallLatency(BaseModel):
    """Turn latency summary of the call (ours)."""

    p50_e2e_ms: int | None = Field(description="Median voice-to-voice latency of the agent's turns.")
    p95_e2e_ms: int | None = Field(description="95th percentile voice-to-voice latency.")
    avg_llm_ttft_ms: int | None = Field(description="Mean time to the first LLM token.")
    avg_tts_ttfa_ms: int | None = Field(description="Mean time to the first TTS audio.")
    turns: int = Field(description="Agent turns with a measured voice-to-voice latency.")


class VoicemailSummary(BaseModel):
    detected: bool = Field(description="A voicemail greeting was detected.")
    action_taken: str | None = Field(
        description="Voicemail action applied: `hangup`, `leave_message`, `leave_message_and_sms`, `ignore`."
    )
    message_left: bool = Field(description="Whether the voicemail message was played.")


class WarmTransferCall(BaseModel):
    role: str = Field(description="Leg role (`warm_proxy`).")
    state: str = Field(description="Last carrier state of the leg.")
    started_at: datetime = Field(description="When the leg was created.")
    answered_at: datetime | None = Field(description="When the leg was answered.")
    ended_at: datetime | None = Field(description="When the leg hung up.")
    hangup_cause: str | None = Field(description="Carrier hangup cause.")


class GuardRailEntry(BaseModel):
    guard_rail: str = Field(description="Guard rail name, e.g. `tcpa:opt_out` or `custom:<name>`.")
    triggered: bool = Field(description="Whether the guard rail fired.")
    action: str | None = Field(description="Action taken when it fired.")
    turn_index: int | None = Field(description="Turn that was evaluated.")


class CallListItem(BaseModel):
    """A call in `GET /v1/calls`: the Call Object without its heavy fields (use `GET /v1/calls/{call_id}`)."""

    model_config = _model_config(_LIST_ITEM_EXAMPLE)

    call_id: uuid.UUID = Field(description="Call id.")
    c_id: uuid.UUID = Field(description="Alias of `call_id` (Bland legacy).")
    org_id: uuid.UUID = Field(description="Owning organization (ours).")
    external_id: str | None = Field(description="Caller-supplied external id.")
    batch_id: uuid.UUID | None = Field(description="Batch the call belongs to.")
    to: str = Field(description="Callee number (E.164; empty for web calls).")
    from_: str = Field(
        validation_alias="from",
        serialization_alias="from",
        description="Caller id (E.164; empty for web calls).",
    )
    inbound: bool = Field(description="Whether the call was inbound.")
    direction: Literal["outbound", "inbound", "web"] = Field(description="Call direction (ours).")
    test_call: bool = Field(description="Dashboard test call (ours).")
    created_at: datetime = Field(description="When the call was created (UTC).")
    started_at: datetime | None = Field(description="When the call was answered.")
    end_at: datetime | None = Field(description="When the call ended.")
    call_length: float = Field(description="Connected minutes (6 decimals).")
    corrected_duration: str = Field(description="Connected seconds as an integer string (Bland compat).")
    completed: bool = Field(description="Whether post-call processing finished.")
    status: BlandCallStatus = Field(description="Call status (Bland hyphenated form).")
    queue_status: str = Field(
        description="Queue state: `complete`, `started`, `allocated`, `queued`, `call_error`, `new`."
    )
    answered_by: BlandAnsweredBy | None = Field(description="Who answered (AMD / classifier result).")
    call_ended_by: BlandCallEndedBy | None = Field(description="Who ended the call; `null` while active.")
    end_reason: str | None = Field(description="Why the call ended (specs/05 §9); `null` while active.")
    max_duration: float = Field(description="Maximum call length in minutes.")
    record: bool = Field(description="Whether the call is recorded.")
    recording_url: str | None = Field(
        description="Presigned recording URL, valid 7 days, fresh on each read."
    )
    recording_expiration: datetime | None = Field(description="When `recording_url` stops working.")
    request_data: dict[str, Any] = Field(description="Request data given to the agent.")
    metadata: dict[str, Any] = Field(description="Caller metadata.")
    pathway_id: uuid.UUID | None = Field(description="Pathway run by the call.")
    pathway_version: int | None = Field(description="Pathway version run by the call.")
    persona_id: uuid.UUID | None = Field(description="Persona run by the call.")
    persona_version: str | None = Field(description="Persona version run by the call.")
    agent_id: uuid.UUID | None = Field(description="Agent run by the call.")
    agent_version: str | None = Field(description="Agent version (semver) run by the call.")
    experiment_id: uuid.UUID | None = Field(description="Experiment that picked the agent version (ours).")
    experiment_arm: str | None = Field(description="Label of the experiment arm (ours).")
    summary: str | None = Field(description="Post-call summary.")
    disposition_tag: str | None = Field(description="Disposition chosen post-call.")
    analysis_schema: dict[str, Any] | None = Field(description="Requested analysis schema.")
    price: float = Field(description="Price in dollars (6 decimals).")
    transferred_to: str | None = Field(description="Transfer destination.")
    transfer_duration: float = Field(description="Minutes spent transferred.")
    local_dialing: bool = Field(description="Whether local dialing picked the caller id.")
    error_message: str | None = Field(description="Error description of a failed call.")
    latency: CallLatency = Field(description="Turn latency summary (ours).")
    voicemail: VoicemailSummary | None = Field(description="Voicemail handling; `null` when none detected.")
    warm_transfer_calls: list[WarmTransferCall] = Field(description="Warm transfer legs.")
    guard_rails: list[GuardRailEntry] = Field(description="Guard rail evaluations.")


class CallObject(CallListItem):
    """The full Call Object (specs/19 §4): the list item plus transcripts, logs, variables, and results."""

    model_config = _model_config(_CALL_OBJECT_EXAMPLE)

    variables: dict[str, Any] = Field(description="Final variable bag including built-ins.")
    pathway_logs: list[PathwayLog] = Field(description="One entry per assistant turn of a pathway call.")
    transcripts: list[Transcript] = Field(description="Transcript turns in order.")
    concatenated_transcript: str = Field(description='Whole transcript as `"user: …\\n assistant: …\\n"`.')
    analysis: dict[str, Any] | None = Field(
        description="Results of `analysis_schema` / the analyze endpoint."
    )
    citations: list[CallCitation] = Field(description="Extracted citation variables.")
    evals: EvalSummary | None = Field(description="Latest eval run when computed.")
