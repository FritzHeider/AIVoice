"""Read calls as Call Objects (specs/19-events-and-webhooks.md §4; Decision P05-T16a).

Runs in the caller's ``tenant_session``. Child rows of all requested calls are read with one query per table
(``call_id IN (...)``), never per call, so a page of 1 000 calls costs the same number of statements as one.

- :func:`load_call_objects` → full :class:`~va_domain.call_object.CallObject` values (detail endpoint,
  ``call.completed`` webhook).
- :func:`load_transcripts` → the ``transcripts`` of one call (transcript endpoint).
- :func:`load_call_list_items` → :class:`~va_domain.call_object.CallListItem` values (``GET /v1/calls``); only
  turn latencies, guard rails, warm-transfer legs, and voicemail events are read.

Recording URLs are presigned by the caller (``recording_urls``: call id → :class:`SignedUrl` or ``None``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.storage import SignedUrl
from va_db.models import (
    AgentVersion,
    Call,
    CallAnalysis,
    CallEvent,
    CallLeg,
    CallTurn,
    Citation,
    EvalRun,
    ExperimentArm,
    GuardRailEvent,
)
from va_domain.call_object import (
    CALL_ENDED_BY,
    LOG_EVENT_TYPES,
    CallCitation,
    CallListItem,
    CallObject,
    EvalSummary,
    GuardRailEntry,
    LogEvent,
    LogTurn,
    PathwayLog,
    Transcript,
    VoicemailSummary,
    WarmTransferCall,
    bland_answered_by,
    bland_status,
    build_pathway_logs,
    concatenated_transcript,
    dollars,
    latency_summary,
    minutes,
)

__all__ = ["load_call_list_items", "load_call_objects", "load_transcripts"]

DEFAULT_MAX_DURATION_MIN: Final = 30
"""``SendCallConfig.max_duration_min`` default, for calls whose stored config omits it."""

_VOICEMAIL_END_ACTIONS: Final[Mapping[str, str]] = {
    "voicemail_hangup": "hangup",
    "voicemail_left": "leave_message",
}


@dataclass(slots=True)
class _Children:
    """Child rows of a set of calls, grouped by call id."""

    latencies: dict[uuid.UUID, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    guard_rails: dict[uuid.UUID, list[GuardRailEntry]] = field(default_factory=lambda: defaultdict(list))
    warm_legs: dict[uuid.UUID, list[WarmTransferCall]] = field(default_factory=lambda: defaultdict(list))
    voicemail_actions: dict[uuid.UUID, str | None] = field(default_factory=dict)
    semvers: dict[uuid.UUID, str] = field(default_factory=dict)
    arm_labels: dict[uuid.UUID, str] = field(default_factory=dict)


@dataclass(slots=True)
class _Details:
    """The detail-only child rows."""

    turns: dict[uuid.UUID, list[CallTurn]] = field(default_factory=lambda: defaultdict(list))
    log_events: dict[uuid.UUID, list[LogEvent]] = field(default_factory=lambda: defaultdict(list))
    analyses: dict[uuid.UUID, dict[str, Any] | None] = field(default_factory=dict)
    citations: dict[uuid.UUID, list[CallCitation]] = field(default_factory=lambda: defaultdict(list))
    evals: dict[uuid.UUID, EvalSummary] = field(default_factory=dict)


def _scope(calls: Sequence[Call]) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    return [call.id for call in calls], list({call.org_id for call in calls})


async def _children(session: AsyncSession, calls: Sequence[Call], *, all_turns: bool) -> _Children:
    ids, orgs = _scope(calls)
    found = _Children()
    if not all_turns:
        latency_rows = await session.execute(
            select(CallTurn.call_id, CallTurn.latency).where(
                CallTurn.org_id.in_(orgs), CallTurn.call_id.in_(ids)
            )
        )
        for call_id, latency in latency_rows:
            found.latencies[call_id].append(latency)
    rails = await session.scalars(
        select(GuardRailEvent)
        .where(GuardRailEvent.org_id.in_(orgs), GuardRailEvent.call_id.in_(ids))
        .order_by(GuardRailEvent.created_at, GuardRailEvent.id)
    )
    for rail in rails:
        found.guard_rails[rail.call_id].append(
            GuardRailEntry(
                guard_rail=rail.guard_rail,
                triggered=rail.triggered,
                action=rail.action,
                turn_index=rail.turn_index,
            )
        )
    legs = await session.scalars(
        select(CallLeg)
        .where(CallLeg.org_id.in_(orgs), CallLeg.call_id.in_(ids), CallLeg.role == "warm_proxy")
        .order_by(CallLeg.started_at, CallLeg.id)
    )
    for leg in legs:
        found.warm_legs[leg.call_id].append(
            WarmTransferCall(
                role=leg.role,
                state=leg.state,
                started_at=leg.started_at,
                answered_at=leg.answered_at,
                ended_at=leg.ended_at,
                hangup_cause=leg.hangup_cause,
            )
        )
    voicemail = await session.execute(
        select(CallEvent.call_id, CallEvent.data)
        .where(CallEvent.org_id.in_(orgs), CallEvent.call_id.in_(ids), CallEvent.category == "voicemail")
        .order_by(CallEvent.call_id, CallEvent.seq)
    )
    for call_id, data in voicemail:
        action = data.get("action")
        found.voicemail_actions[call_id] = action if isinstance(action, str) else None
    await _load_versions(session, orgs, calls, found)
    return found


async def _turns(session: AsyncSession, calls: Sequence[Call]) -> Sequence[CallTurn]:
    ids, orgs = _scope(calls)
    rows = await session.scalars(
        select(CallTurn)
        .where(CallTurn.org_id.in_(orgs), CallTurn.call_id.in_(ids))
        .order_by(CallTurn.call_id, CallTurn.turn_index, CallTurn.start_ms, CallTurn.id)
    )
    return rows.all()


async def _details(session: AsyncSession, calls: Sequence[Call]) -> _Details:
    ids, orgs = _scope(calls)
    found = _Details()
    for turn in await _turns(session, calls):
        found.turns[turn.call_id].append(turn)
    log_events = await session.execute(
        select(CallEvent.call_id, CallEvent.event_type, CallEvent.data)
        .where(
            CallEvent.org_id.in_(orgs),
            CallEvent.call_id.in_(ids),
            CallEvent.event_type.in_(LOG_EVENT_TYPES),
        )
        .order_by(CallEvent.call_id, CallEvent.seq)
    )
    for call_id, event_type, data in log_events:
        if event_type is None:
            continue
        found.log_events[call_id].append(LogEvent(event_type=event_type, data=data))
    analyses = await session.scalars(
        select(CallAnalysis)
        .where(CallAnalysis.org_id.in_(orgs), CallAnalysis.call_id.in_(ids))
        .where(CallAnalysis.kind == "analysis_schema")
        .order_by(CallAnalysis.created_at, CallAnalysis.id)
    )
    for analysis in analyses:
        found.analyses[analysis.call_id] = analysis.result if isinstance(analysis.result, dict) else None
    citations = await session.scalars(
        select(Citation)
        .where(Citation.org_id.in_(orgs), Citation.call_id.in_(ids))
        .order_by(Citation.created_at, Citation.id)
    )
    for citation in citations:
        found.citations[citation.call_id].append(
            CallCitation(
                citation_schema_id=citation.citation_schema_id,
                variable_name=citation.variable_name,
                variable_type=citation.variable_type,
                value=citation.value,
                conditions_met=citation.conditions_met,
                cited_utterances=citation.cited_utterances,
            )
        )
    await _load_evals(session, orgs, ids, found)
    return found


async def _load_evals(
    session: AsyncSession, orgs: list[uuid.UUID], ids: list[uuid.UUID], found: _Details
) -> None:
    runs = await session.scalars(
        select(EvalRun)
        .where(EvalRun.org_id.in_(orgs), EvalRun.call_id.in_(ids))
        .order_by(EvalRun.created_at, EvalRun.id)
    )
    for run in runs:
        if run.call_id is None:
            continue
        found.evals[run.call_id] = EvalSummary(
            eval_run_id=run.id,
            overall_score=None if run.overall_score is None else float(run.overall_score),
            passed=run.passed,
            results=run.results,
        )


async def _load_versions(
    session: AsyncSession, orgs: list[uuid.UUID], calls: Sequence[Call], found: _Children
) -> None:
    version_ids = {call.agent_version_id for call in calls if call.agent_version_id is not None}
    if version_ids:
        versions = await session.execute(
            select(AgentVersion.id, AgentVersion.semver).where(
                AgentVersion.org_id.in_(orgs), AgentVersion.id.in_(version_ids)
            )
        )
        found.semvers = dict(versions.all())
    arm_ids = {call.experiment_arm_id for call in calls if call.experiment_arm_id is not None}
    if arm_ids:
        arms = await session.execute(
            select(ExperimentArm.id, ExperimentArm.label).where(
                ExperimentArm.org_id.in_(orgs), ExperimentArm.id.in_(arm_ids)
            )
        )
        found.arm_labels = dict(arms.all())


def _voicemail(call: Call, event_action: str | None, has_event: bool) -> VoicemailSummary | None:
    end_reason = call.end_reason or ""
    if not (has_event or call.answered_by == "voicemail" or end_reason in _VOICEMAIL_END_ACTIONS):
        return None
    return VoicemailSummary(
        detected=True,
        action_taken=event_action or _VOICEMAIL_END_ACTIONS.get(end_reason),
        message_left=end_reason == "voicemail_left",
    )


def _list_fields(
    call: Call,
    children: _Children,
    latencies: Iterable[Mapping[str, Any]],
    recording: SignedUrl | None,
) -> dict[str, Any]:
    config = call.config
    strategy = config.get("dialing_strategy")
    return {
        "call_id": call.id,
        "c_id": call.id,
        "org_id": call.org_id,
        "external_id": call.external_id,
        "batch_id": call.batch_id,
        "to": call.to_number or "",
        "from": call.from_number or "",
        "inbound": call.direction == "inbound",
        "direction": call.direction,
        "test_call": call.test_call,
        "created_at": call.created_at,
        "started_at": call.answered_at or call.started_at,
        "end_at": call.ended_at,
        "call_length": minutes(call.connected_seconds),
        "corrected_duration": str(round(call.connected_seconds)),
        "completed": call.queue_status == "complete",
        "status": bland_status(call.status),
        "queue_status": call.queue_status or "new",
        "answered_by": bland_answered_by(call.answered_by),
        "call_ended_by": None if call.ended_by is None else CALL_ENDED_BY[call.ended_by],
        "end_reason": call.end_reason,
        "max_duration": config.get("max_duration_min", DEFAULT_MAX_DURATION_MIN),
        "record": bool(config.get("record", False)),
        "recording_url": None if recording is None else recording.url,
        "recording_expiration": None if recording is None else recording.expires_at,
        "request_data": call.request_data,
        "metadata": call.metadata_,
        "pathway_id": call.pathway_id,
        "pathway_version": call.pathway_version,
        "persona_id": call.persona_id,
        "persona_version": None if call.persona_version is None else str(call.persona_version),
        "agent_id": call.agent_id,
        "agent_version": None
        if call.agent_version_id is None
        else children.semvers.get(call.agent_version_id),
        "experiment_id": call.experiment_id,
        "experiment_arm": None
        if call.experiment_arm_id is None
        else children.arm_labels.get(call.experiment_arm_id),
        "summary": call.summary,
        "disposition_tag": call.disposition_tag,
        "analysis_schema": config.get("analysis_schema"),
        "price": dollars(call.price_micros),
        "transferred_to": call.transferred_to,
        "transfer_duration": minutes(call.transfer_seconds),
        "local_dialing": isinstance(strategy, dict) and strategy.get("type") == "local",
        "error_message": call.error_message,
        "latency": latency_summary(latencies),
        "voicemail": _voicemail(
            call, children.voicemail_actions.get(call.id), call.id in children.voicemail_actions
        ),
        "warm_transfer_calls": children.warm_legs.get(call.id, []),
        "guard_rails": children.guard_rails.get(call.id, []),
    }


def _transcripts(turns: Sequence[CallTurn]) -> list[Transcript]:
    spoken = [turn for turn in turns if turn.role != "system"]
    return [
        Transcript.model_validate(
            {
                "id": position,
                "user": turn.role,
                "text": turn.text_,
                "created_at": turn.created_at,
                "start_ms": turn.start_ms,
                "end_ms": turn.end_ms,
                "interrupted": turn.interrupted,
            }
        )
        for position, turn in enumerate(spoken, start=1)
    ]


def _pathway_logs(call: Call, turns: Sequence[CallTurn], details: _Details) -> list[PathwayLog]:
    """specs/11 §12 entries from the turns and the route/condition/extraction/tool events (P07-T08c); a
    pathway call's rows are grouped by their engine turn (``pathway_turn``, Decision P07-T12b)."""
    return build_pathway_logs(
        [
            LogTurn(
                turn_index=turn.turn_index if turn.pathway_turn is None else turn.pathway_turn,
                role=turn.role,
                text=turn.text_,
                node_id=turn.node_id,
                node_name=turn.node_name,
                created_at=turn.created_at,
            )
            for turn in turns
        ],
        details.log_events.get(call.id, []),
        pathway_id=call.pathway_id,
        pathway_version=call.pathway_version,
    )


async def load_transcripts(session: AsyncSession, call: Call) -> list[Transcript]:
    """The ``transcripts`` of ``call``'s Call Object (``GET /v1/calls/{call_id}/transcript``)."""
    return _transcripts(await _turns(session, [call]))


async def load_call_list_items(
    session: AsyncSession, calls: Sequence[Call], *, recording_urls: Mapping[uuid.UUID, SignedUrl | None]
) -> list[CallListItem]:
    """``calls`` as list items, in the given order."""
    if not calls:
        return []
    children = await _children(session, calls, all_turns=False)
    return [
        CallListItem.model_validate(
            _list_fields(call, children, children.latencies.get(call.id, []), recording_urls.get(call.id))
        )
        for call in calls
    ]


async def load_call_objects(
    session: AsyncSession, calls: Sequence[Call], *, recording_urls: Mapping[uuid.UUID, SignedUrl | None]
) -> list[CallObject]:
    """``calls`` as full Call Objects, in the given order."""
    if not calls:
        return []
    children = await _children(session, calls, all_turns=True)
    details = await _details(session, calls)
    objects: list[CallObject] = []
    for call in calls:
        turns = details.turns.get(call.id, [])
        transcripts = _transcripts(turns)
        fields = _list_fields(call, children, [turn.latency for turn in turns], recording_urls.get(call.id))
        objects.append(
            CallObject.model_validate(
                {
                    **fields,
                    "variables": call.variables,
                    "pathway_logs": _pathway_logs(call, turns, details),
                    "transcripts": transcripts,
                    "concatenated_transcript": concatenated_transcript(transcripts),
                    "analysis": details.analyses.get(call.id),
                    "citations": details.citations.get(call.id, []),
                    "evals": details.evals.get(call.id),
                }
            )
        )
    return objects
