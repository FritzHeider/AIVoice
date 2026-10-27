"""Calls: contacts, batches, calls, legs, turns, events, analyses, evals, DNC (specs/03-data-model.md §6).

Follows the conventions of :mod:`va_db.models.tenancy`: explicit SQL types, ``Literal`` aliases enforced by
:func:`check_in`, and spec-named indexes. Per the §1 decision, every tenant table has an ``org_id`` index.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from sqlalchemy import (
    REAL,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from va_common.ids import new_id
from va_db.base import Base, IdMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    bool_column,
    fk_column,
    int_column,
    jsonb_column,
    jsonb_default,
    micros_column,
    org_id_column,
    platform_scope,
    text_column,
    tz,
)

__all__ = [
    "AnsweredBy",
    "Batch",
    "BatchItem",
    "BatchItemStatus",
    "BatchStatus",
    "Call",
    "CallAnalysis",
    "CallAnalysisKind",
    "CallDirection",
    "CallEvent",
    "CallEventCategory",
    "CallEventLevel",
    "CallLeg",
    "CallLegRole",
    "CallLegState",
    "CallStatus",
    "CallTurn",
    "CallTurnRole",
    "Citation",
    "Contact",
    "DncEntry",
    "DncSource",
    "EndedBy",
    "EvalRun",
    "EvalRunStatus",
    "GuardRailEvent",
    "RunnerType",
]

BatchStatus = Literal[
    "created", "scheduled", "validating", "dispatching", "in_progress", "completed", "stopped", "failed"
]
BatchItemStatus = Literal["pending", "skipped", "dispatched", "completed", "failed", "canceled"]
CallDirection = Literal["outbound", "inbound", "web"]
CallStatus = Literal[
    "scheduled",
    "queued",
    "dialing",
    "ringing",
    "in_progress",
    "transferring",
    "transferred",
    "completed",
    "failed",
    "canceled",
    "no_answer",
    "busy",
]
AnsweredBy = Literal["human", "voicemail", "ivr", "unknown", "no_answer", "fax"]
RunnerType = Literal["task", "pathway", "persona", "agent"]
EndedBy = Literal["agent", "user", "system", "api", "guard_rail", "timeout", "error"]
CallLegRole = Literal["primary", "transfer_target", "warm_proxy", "monitor"]
CallLegState = Literal["initiated", "ringing", "answered", "bridged", "hangup"]
CallTurnRole = Literal["user", "assistant", "agent-action", "system", "human_agent"]
CallEventCategory = Literal[
    "queue",
    "call",
    "latency",
    "webhook",
    "tool",
    "dynamic_data",
    "route",
    "extraction",
    "guard_rail",
    "transfer",
    "voicemail",
    "error",
    "citations",
    "evals",
    "post_transfer_transcript",
    "sms",
    "memory",
    "interrupt",
]
CallEventLevel = Literal["debug", "info", "warning", "error"]
CallAnalysisKind = Literal["summary", "disposition", "analysis_schema", "sentiment", "qa"]
EvalRunStatus = Literal["queued", "running", "completed", "failed"]
DncSource = Literal["manual", "api", "opt_out_voice", "opt_out_sms", "import", "platform"]


def _call_id_column() -> MappedColumn[Any]:
    """``call_id uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE`` of the per-call child tables."""
    return fk_column("calls.id", "CASCADE", nullable=False)


def _seconds_column() -> MappedColumn[Any]:
    """``numeric(10,3) NOT NULL DEFAULT 0``: a duration in seconds with millisecond precision."""
    return mapped_column(Numeric(10, 3), nullable=False, server_default=text("0"))


class Contact(IdMixin, TimestampMixin, Base):
    """``contacts``: a callee; phone, email (case-insensitive), and external id are each unique per org."""

    __tablename__ = "contacts"
    __table_args__ = (
        Index(
            "contacts_phone_uq",
            "org_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL"),
        ),
        Index(
            "contacts_email_uq", "org_id", "email", unique=True, postgresql_where=text("email IS NOT NULL")
        ),
        Index(
            "contacts_external_uq",
            "org_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("contacts_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    phone_e164: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(CITEXT)
    external_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = jsonb_column("{}")


class Batch(IdMixin, TimestampMixin, Base):
    """``batches``: a bulk call campaign; ``schedule`` is a ``BatchSchedule`` (specs/17-batches §3)."""

    __tablename__ = "batches"
    __table_args__ = (check_in("status", get_args(BatchStatus)),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    label: Mapped[str | None] = mapped_column(Text)
    status: Mapped[BatchStatus] = text_column("created")
    global_props: Mapped[dict[str, Any]] = jsonb_column("{}")
    status_webhook: Mapped[str | None] = mapped_column(Text)
    schedule: Mapped[dict[str, Any]] = jsonb_column("{}")
    calls_total: Mapped[int] = int_column(0)
    calls_dispatched: Mapped[int] = int_column(0)
    calls_completed: Mapped[int] = int_column(0)
    calls_successful: Mapped[int] = int_column(0)
    calls_failed: Mapped[int] = int_column(0)
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(tz())
    completed_at: Mapped[datetime | None] = mapped_column(tz())
    created_by_key: Mapped[uuid.UUID | None] = fk_column("api_keys.id", "SET NULL")


Index("batches_org_created_idx", Batch.org_id, Batch.created_at.desc())


class BatchItem(IdMixin, TimestampMixin, Base):
    """``batch_items``: one row of a batch; ``payload`` is the per-call Send Call body merged over
    ``global_props``.

    ``call_id`` deliberately has no foreign key: ``calls.batch_item_id`` already references this table, and a
    reference back would make the two tables a creation/drop cycle.
    """

    __tablename__ = "batch_items"
    __table_args__ = (
        check_in("status", get_args(BatchItemStatus)),
        UniqueConstraint("batch_id", "ordinal"),
        Index("batch_items_dispatch_idx", "batch_id", "status", "next_attempt_at"),
        Index("batch_items_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    batch_id: Mapped[uuid.UUID] = fk_column("batches.id", "CASCADE", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb_column()
    status: Mapped[BatchItemStatus] = text_column("pending")
    skip_reason: Mapped[str | None] = mapped_column(Text)
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(tz())


class Call(IdMixin, TimestampMixin, Base):
    """``calls``: one phone or web call.

    ``config`` is the fully resolved ``SendCallConfig`` after precedence (specs/05-calls-api §4),
    ``variables`` the final variable bag, ``queue_status`` the Bland-compatible queue state, and
    ``end_reason`` one of the specs/05-calls-api §9 enumeration. ``credit_hold_id`` and ``memory_id`` carry
    no foreign key, as in the spec.
    """

    __tablename__ = "calls"
    __table_args__ = (
        CheckConstraint("char_length(external_id) <= 256", name="external_id_check"),
        check_in("direction", get_args(CallDirection)),
        check_in("status", get_args(CallStatus)),
        check_in("answered_by", get_args(AnsweredBy)),
        check_in("runner_type", get_args(RunnerType)),
        check_in("ended_by", get_args(EndedBy)),
        Index("calls_org_status_idx", "org_id", "status"),
        Index(
            "calls_org_external_idx",
            "org_id",
            "external_id",
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("calls_batch_idx", "batch_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    external_id: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[CallDirection] = mapped_column(Text, nullable=False)
    to_number: Mapped[str | None] = mapped_column(Text)
    from_number: Mapped[str | None] = mapped_column(Text)
    contact_id: Mapped[uuid.UUID | None] = fk_column("contacts.id", "SET NULL")
    status: Mapped[CallStatus] = mapped_column(Text, nullable=False)
    queue_status: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[AnsweredBy | None] = mapped_column(Text)
    runner_type: Mapped[RunnerType] = mapped_column(Text, nullable=False)
    pathway_id: Mapped[uuid.UUID | None] = fk_column("pathways.id", "SET NULL")
    pathway_version: Mapped[int | None] = mapped_column(Integer)
    persona_id: Mapped[uuid.UUID | None] = fk_column("personas.id", "SET NULL")
    persona_version: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[uuid.UUID | None] = fk_column("agents.id", "SET NULL")
    agent_version_id: Mapped[uuid.UUID | None] = fk_column("agent_versions.id", "SET NULL")
    experiment_id: Mapped[uuid.UUID | None] = fk_column("experiments.id", "SET NULL")
    experiment_arm_id: Mapped[uuid.UUID | None] = fk_column("experiment_arms.id", "SET NULL")
    # Dashboard test calls; excluded from analytics and experiments.
    test_call: Mapped[bool] = bool_column(default=False)
    batch_id: Mapped[uuid.UUID | None] = fk_column("batches.id", "SET NULL")
    batch_item_id: Mapped[uuid.UUID | None] = fk_column("batch_items.id", "SET NULL")
    # Retry-of or warm-transfer parent.
    parent_call_id: Mapped[uuid.UUID | None] = fk_column("calls.id", "SET NULL")
    phone_number_id: Mapped[uuid.UUID | None] = fk_column("phone_numbers.id", "SET NULL")
    voice_id: Mapped[uuid.UUID | None] = fk_column("voices.id", "SET NULL")
    config: Mapped[dict[str, Any]] = jsonb_column()
    request_data: Mapped[dict[str, Any]] = jsonb_column("{}")
    # ``metadata`` is reserved on Declarative classes (it is the MetaData), so the attribute takes a trailing
    # underscore while the column keeps the spec name.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=jsonb_default("{}")
    )
    variables: Mapped[dict[str, Any]] = jsonb_column("{}")
    start_time: Mapped[datetime | None] = mapped_column(tz())
    dial_started_at: Mapped[datetime | None] = mapped_column(tz())
    answered_at: Mapped[datetime | None] = mapped_column(tz())
    started_at: Mapped[datetime | None] = mapped_column(tz())
    ended_at: Mapped[datetime | None] = mapped_column(tz())
    connected_seconds: Mapped[Decimal] = _seconds_column()
    transfer_seconds: Mapped[Decimal] = _seconds_column()
    end_reason: Mapped[str | None] = mapped_column(Text)
    ended_by: Mapped[EndedBy | None] = mapped_column(Text)
    transferred_to: Mapped[str | None] = mapped_column(Text)
    recording_s3_key: Mapped[str | None] = mapped_column(Text)
    recording_duration_ms: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    disposition_tag: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    price_micros: Mapped[int] = micros_column(0)
    credit_hold_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    workflow_id: Mapped[str | None] = mapped_column(Text)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


Index("calls_org_created_idx", Call.org_id, Call.created_at.desc())
Index("calls_org_to_idx", Call.org_id, Call.to_number, Call.created_at.desc())
Index("calls_pathway_idx", Call.org_id, Call.pathway_id, Call.created_at.desc())


class CallLeg(IdMixin, TimestampMixin, Base):
    """``call_legs``: one Telnyx call leg (primary, transfer target, warm-transfer proxy, monitor)."""

    __tablename__ = "call_legs"
    __table_args__ = (
        check_in("role", get_args(CallLegRole)),
        check_in("state", get_args(CallLegState)),
        Index("call_legs_ccid_uq", "call_control_id", unique=True),
        Index("call_legs_call_idx", "call_id"),
        Index("call_legs_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    call_id: Mapped[uuid.UUID] = _call_id_column()
    role: Mapped[CallLegRole] = mapped_column(Text, nullable=False)
    telnyx_account_id: Mapped[uuid.UUID | None] = fk_column("telnyx_accounts.id", "SET NULL")
    call_control_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_leg_id: Mapped[str | None] = mapped_column(Text)
    call_session_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[CallLegState] = mapped_column(Text, nullable=False)
    hangup_cause: Mapped[str | None] = mapped_column(Text)
    hangup_source: Mapped[str | None] = mapped_column(Text)
    sip_hangup_cause: Mapped[str | None] = mapped_column(Text)
    amd_result: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(tz(), nullable=False, server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(tz())
    ended_at: Mapped[datetime | None] = mapped_column(tz())


class CallTurn(IdMixin, TimestampMixin, Base):
    """``call_turns``: one transcript turn; ``start_ms``/``end_ms`` are relative to the call's answer time.

    ``latency`` is ``{eot_ms, stt_final_ms, llm_ttft_ms, tts_ttfa_ms, e2e_ms, route_ms, tool_ms}``. For
    assistant turns ``text_spoken`` is what was played before an interruption; for user turns
    ``text_realtime`` keeps the realtime STT text once batch re-transcription replaces ``text``.
    """

    __tablename__ = "call_turns"
    __table_args__ = (
        check_in("role", get_args(CallTurnRole)),
        UniqueConstraint("call_id", "turn_index", "role"),
        Index("call_turns_call_idx", "call_id", "turn_index"),
        Index("call_turns_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    call_id: Mapped[uuid.UUID] = _call_id_column()
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[CallTurnRole] = mapped_column(Text, nullable=False)
    # The attribute takes a trailing underscore so it does not shadow ``sqlalchemy.text`` in the class body.
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    text_spoken: Mapped[str | None] = mapped_column(Text)
    interrupted: Mapped[bool] = bool_column(default=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text)
    node_name: Mapped[str | None] = mapped_column(Text)
    language_code: Mapped[str | None] = mapped_column(Text)
    stt_confidence: Mapped[float | None] = mapped_column(REAL)
    latency: Mapped[dict[str, Any]] = jsonb_column("{}")
    text_realtime: Mapped[str | None] = mapped_column(Text)


class CallEvent(Base):
    """``call_events``: the per-call event log, range-partitioned by month on ``created_at``.

    The model maps the partitioned parent. Partitions are created monthly by RetentionWorkflow (three months
    ahead) and dropped after the orgs' maximum retention. A partitioned table's primary key must contain the
    partition key, hence ``(created_at, id)``; ``created_at`` comes back from the server default via
    RETURNING. ``org_id`` and ``call_id`` carry no foreign keys, as in the spec, so old partitions can be
    dropped freely.
    """

    __tablename__ = "call_events"
    __table_args__ = (
        check_in("category", get_args(CallEventCategory)),
        check_in("level", get_args(CallEventLevel)),
        PrimaryKeyConstraint("created_at", "id"),
        Index("call_events_call_idx", "call_id", "seq"),
        Index("call_events_org_idx", "org_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=new_id)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    call_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[CallEventCategory] = mapped_column(Text, nullable=False)
    level: Mapped[CallEventLevel] = text_column("info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = jsonb_column("{}")
    created_at: Mapped[datetime] = mapped_column(tz(), nullable=False, server_default=func.now())


class CallAnalysis(IdMixin, TimestampMixin, Base):
    """``call_analyses``: a post-call analysis; ``questions`` is ``[[question, type], ...]`` (Bland analyze
    format) and ``schema`` the ``analysis_schema`` used.
    """

    __tablename__ = "call_analyses"
    __table_args__ = (
        check_in("kind", get_args(CallAnalysisKind)),
        Index("call_analyses_call_idx", "call_id"),
        Index("call_analyses_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    call_id: Mapped[uuid.UUID] = _call_id_column()
    kind: Mapped[CallAnalysisKind] = mapped_column(Text, nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    questions: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[Any] = jsonb_column()
    model: Mapped[str] = mapped_column(Text, nullable=False)


class Citation(IdMixin, TimestampMixin, Base):
    """``citations``: one extracted variable with the utterances that support it.

    ``cited_utterances`` is ``[{turn_index, start_time, end_time, channel, speaker_id, text, topics,
    utterance_type}]``.
    """

    __tablename__ = "citations"
    __table_args__ = (
        Index("citations_call_idx", "call_id"),
        Index("citations_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    call_id: Mapped[uuid.UUID] = _call_id_column()
    citation_schema_id: Mapped[uuid.UUID] = fk_column("citation_schemas.id", "CASCADE", nullable=False)
    variable_name: Mapped[str] = mapped_column(Text, nullable=False)
    variable_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    conditions_met: Mapped[bool | None] = mapped_column(Boolean)
    cited_utterances: Mapped[list[Any]] = jsonb_column("[]")


class EvalRun(IdMixin, TimestampMixin, Base):
    """``eval_runs``: one grading run of an eval setup version; ``call_id`` is nullable, as in the spec.

    ``results`` is ``[{criterion_id, score, passed, rationale, cited_turns}]``.
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        check_in("status", get_args(EvalRunStatus)),
        Index("eval_runs_call_idx", "call_id"),
        Index("eval_runs_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    call_id: Mapped[uuid.UUID | None] = fk_column("calls.id", "CASCADE")
    eval_setup_version_id: Mapped[uuid.UUID] = fk_column("eval_setup_versions.id", "CASCADE", nullable=False)
    status: Mapped[EvalRunStatus] = mapped_column(Text, nullable=False)
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    results: Mapped[list[Any]] = jsonb_column("[]")
    error: Mapped[str | None] = mapped_column(Text)


class GuardRailEvent(IdMixin, TimestampMixin, Base):
    """``guard_rail_events``: a guard rail evaluation, e.g. ``tcpa:opt_out`` or ``custom:<name>``."""

    __tablename__ = "guard_rail_events"
    __table_args__ = (
        Index("guard_rail_events_call_idx", "call_id"),
        Index("guard_rail_events_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    call_id: Mapped[uuid.UUID] = _call_id_column()
    guard_rail: Mapped[str] = mapped_column(Text, nullable=False)
    triggered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    action: Mapped[str | None] = mapped_column(Text)
    turn_index: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = jsonb_column("{}")


class DncEntry(IdMixin, TimestampMixin, Base):
    """``dnc_entries``: a do-not-call number; ``org_id`` NULL is the platform-wide blacklist.

    ``call_id`` has no foreign key, as in the spec, so an entry outlives the call that created it.
    """

    __tablename__ = "dnc_entries"
    __table_args__ = (
        check_in("source", get_args(DncSource)),
        Index("dnc_entries_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID | None] = org_id_column(nullable=True)
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[DncSource] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(tz())


Index("dnc_scope_phone_uq", platform_scope(DncEntry.org_id), DncEntry.phone_e164, unique=True)
