"""Send Call reference checks (specs/05-calls-api.md §4.1 steps 5-7; Decision P05-T03c): voice, tools and
knowledge bases, citation schemas, eval setups, memory, number pool, plan features, and the BYO Telnyx
account behind `encrypted_key`.

All queries run in the caller's tenant session and filter by ``org_id``; platform voices (``org_id`` NULL)
are visible through the `platform_read` policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from va_common.errors import AppError
from va_db.models import (
    CitationSchema,
    EvalSetup,
    EvalSetupVersion,
    KnowledgeBase,
    Memory,
    NumberPool,
    Plan,
    TelnyxAccount,
    Tool,
    Voice,
)
from va_domain.call_resolution import MergedParameters, ResolvedVoice
from va_domain.calls import GuardRail, InlineTool, PostCallEvals
from va_domain.org import OrgSettings
from va_domain.voices import VoiceSettings

__all__ = [
    "MAX_CUSTOM_GUARD_RAILS",
    "check_plan_features",
    "check_references",
    "check_tools",
    "load_byo_account",
    "load_voice",
]

MAX_CUSTOM_GUARD_RAILS: Final = 5
_UNUSABLE_CLONES: Final = ("processing", "failed")
_UNAVAILABLE_ACCOUNTS: Final = ("pending", "error")
# Tool reference prefix -> (table, error code, label).
_REFERENCE_KINDS: Final[Mapping[str, tuple[Any, str, str]]] = {
    "TL": (Tool, "TOOL_NOT_FOUND", "Tool"),
    "KB": (KnowledgeBase, "NOT_FOUND", "Knowledge base"),
}
# Dialing strategy -> plan feature it needs.
_STRATEGY_FEATURES: Final[Mapping[str, str]] = {"local": "local_presence", "custom_pooling": "number_pools"}
_INVALID_KEY_MESSAGE: Final = "Invalid `encrypted_key`."
_CARRIER_MESSAGE: Final = "Error in creating Telnyx connection."


def _requested_voice(merged: MergedParameters, org: OrgSettings) -> str:
    if merged.voice is not None:
        return merged.voice
    if org.default_voice_id is not None:
        return str(org.default_voice_id)
    return org.default_voice


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def load_voice(
    session: AsyncSession, org_id: uuid.UUID, merged: MergedParameters, org: OrgSettings
) -> ResolvedVoice:
    """The call's voice: the merged `voice`, else the org `default_voice_id`, else `default_voice`.

    A UUID matches `voices.id`; anything else matches the name case-insensitively, the org's own voice before
    a platform voice. Deleted voices and clones still processing or failed do not match.
    """
    requested = _requested_voice(merged, org)
    voice_id = _as_uuid(requested)
    match = Voice.id == voice_id if voice_id is not None else func.lower(Voice.name) == requested.lower()
    voice = await session.scalar(
        select(Voice)
        .where(
            match,
            or_(Voice.org_id == org_id, Voice.org_id.is_(None)),
            Voice.deleted_at.is_(None),
            or_(Voice.clone_status.is_(None), Voice.clone_status.not_in(_UNUSABLE_CLONES)),
        )
        .order_by(case((Voice.org_id.is_(None), 1), else_=0))
        .limit(1)
    )
    if voice is None:
        raise AppError("VOICE_NOT_FOUND", f"Voice {requested} not found.", field="voice")
    settings = VoiceSettings.model_validate(voice.settings)
    return ResolvedVoice(voice_id=voice.id, provider_voice_id=voice.provider_voice_id, settings=settings)


async def _existing(
    session: AsyncSession,
    org_id: uuid.UUID,
    model: Any,
    ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """The subset of `ids` that are rows of `model` in the org (not soft-deleted where the table can be)."""
    if not ids:
        return set()
    id_column: InstrumentedAttribute[uuid.UUID] = model.id
    statement = select(id_column).where(id_column.in_(ids), model.org_id == org_id)
    deleted = getattr(model, "deleted_at", None)
    if deleted is not None:
        statement = statement.where(deleted.is_(None))
    return set((await session.scalars(statement)).all())


def _parsed_refs(tools: Sequence[str | InlineTool]) -> list[tuple[int, str, str, uuid.UUID]]:
    """`(index, reference, prefix, id)` of every `TL-`/`KB-` reference (inline tools are skipped)."""
    parsed: list[tuple[int, str, str, uuid.UUID]] = []
    for index, tool in enumerate(tools):
        if isinstance(tool, str):
            prefix, _, raw_id = tool.partition("-")
            parsed.append((index, tool, prefix, uuid.UUID(raw_id)))
    return parsed


async def check_tools(session: AsyncSession, org_id: uuid.UUID, tools: Sequence[str | InlineTool]) -> None:
    """Every `TL-` tool and `KB-` knowledge base reference exists; the first missing one (in order) fails."""
    parsed = _parsed_refs(tools)
    found: dict[str, set[uuid.UUID]] = {}
    for prefix, (model, _, _) in _REFERENCE_KINDS.items():
        found[prefix] = await _existing(
            session, org_id, model, [ref[3] for ref in parsed if ref[2] == prefix]
        )
    for index, tool, prefix, ident in parsed:
        if ident not in found[prefix]:
            _, code, label = _REFERENCE_KINDS[prefix]
            raise AppError(code, f"{label} {tool} not found.", field=f"tools[{index}]")


async def _check_citation_schemas(session: AsyncSession, org_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> None:
    found = await _existing(session, org_id, CitationSchema, ids)
    for index, ident in enumerate(ids):
        if ident not in found:
            message = f"Citation schema {ident} not found."
            raise AppError("NOT_FOUND", message, field=f"citation_schema_ids[{index}]")


async def _check_evals(session: AsyncSession, org_id: uuid.UUID, evals: PostCallEvals) -> None:
    references: tuple[tuple[str, Any, uuid.UUID | None], ...] = (
        ("workbench_setup_id", EvalSetup, evals.workbench_setup_id),
        ("workbench_setup_version_id", EvalSetupVersion, evals.workbench_setup_version_id),
    )
    for key, model, ident in references:
        if ident is not None and not await _existing(session, org_id, model, [ident]):
            raise AppError("NOT_FOUND", f"Eval setup {ident} not found.", field=f"post_call_evals.{key}")


async def check_references(session: AsyncSession, org_id: uuid.UUID, merged: MergedParameters) -> None:
    """Citation schemas, post-call eval setup, memory, and number pool of the merged parameters exist."""
    values = merged.values
    await _check_citation_schemas(session, org_id, list(values.get("citation_schema_ids") or []))
    evals = values.get("post_call_evals")
    if isinstance(evals, PostCallEvals):
        await _check_evals(session, org_id, evals)
    memory_id = values.get("memory_id")
    if isinstance(memory_id, uuid.UUID) and not await _existing(session, org_id, Memory, [memory_id]):
        raise AppError("NOT_FOUND", f"Memory {memory_id} not found.", field="memory_id")
    strategy = merged.dialing_strategy
    if strategy.pool_id is not None and not await _existing(session, org_id, NumberPool, [strategy.pool_id]):
        message = f"Number pool {strategy.pool_id} not found."
        raise AppError("NOT_FOUND", message, field="dialing_strategy.pool_id")


def _require_feature(features: set[str], feature: str, parameter: str) -> None:
    if feature not in features:
        message = f"{parameter} is not available on your plan."
        raise AppError("FEATURE_NOT_IN_PLAN", message, field=parameter)


async def check_plan_features(session: AsyncSession, plan_id: str, merged: MergedParameters) -> None:
    """§4.1 step 6 on the merged values: citations, custom guard rails (at most 5), local presence, pools."""
    features = set(await session.scalar(select(Plan.features).where(Plan.id == plan_id)) or [])
    values = merged.values
    if values.get("citation_schema_ids"):
        _require_feature(features, "citations", "citation_schema_ids")
    rails: list[GuardRail] = list(values.get("guard_rails") or [])
    custom = sum(1 for rail in rails if rail.type == "custom")
    if custom:
        _require_feature(features, "custom_guard_rails", "guard_rails")
    if custom > MAX_CUSTOM_GUARD_RAILS:
        message = f"At most {MAX_CUSTOM_GUARD_RAILS} custom guard rails are allowed per call."
        raise AppError("GUARD_RAIL_LIMIT", message, field="guard_rails")
    strategy_feature = _STRATEGY_FEATURES.get(merged.dialing_strategy.type)
    if strategy_feature is not None:
        _require_feature(features, strategy_feature, "dialing_strategy")


async def load_byo_account(
    session: AsyncSession, org_id: uuid.UUID, encrypted_key: str | None
) -> TelnyxAccount | None:
    """§4.1 step 7: the org's BYO Telnyx account for the `encrypted_key` header (`None` without one)."""
    if encrypted_key is None:
        return None
    account = await session.scalar(
        select(TelnyxAccount).where(
            TelnyxAccount.encrypted_key_handle == encrypted_key, TelnyxAccount.org_id == org_id
        )
    )
    if account is None or account.status == "deleted":
        raise AppError("INVALID_ENCRYPTED_KEY", _INVALID_KEY_MESSAGE)
    if account.status in _UNAVAILABLE_ACCOUNTS:
        raise AppError("CARRIER_CONNECTION_ERROR", _CARRIER_MESSAGE)
    return account
