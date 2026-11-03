"""Pathway versions: list, save, publish, promote, revert (specs/12-pathways-api.md §4).

Rules: specs/12 §4 Decision P07-T09b.

Every function runs in the caller's ``tenant_session`` and, when it changes anything, takes the pathway row
lock first, so exactly one ``draft`` row exists and ``draft_version == latest_version`` after every operation.
Publishing validates the version in publish mode (one ``INVALID_INPUT`` item per error, as for drafts). Cache
invalidation is the router's job: it runs after the transaction commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit
from va_api.auth.api_keys import creating_user_id
from va_api.auth.context import RequestContext
from va_api.services.pathways import (
    VERSION_NOT_FOUND_MESSAGE,
    GraphInput,
    check_graph,
    reject_errors,
)
from va_common.errors import AppError
from va_db.models import Pathway, PathwayVersion
from va_db.repositories import PathwayGraphJson, PathwayRepo, PinEnvironment
from va_engine.validation import ValidationReport

__all__ = [
    "NO_STAGING_MESSAGE",
    "PromoteOutcome",
    "PublishOutcome",
    "RevertOutcome",
    "SaveOutcome",
    "get_version",
    "list_versions",
    "pinned_number",
    "promote_staging",
    "publish_version",
    "revert_draft",
    "save_version",
]

NO_STAGING_MESSAGE: Final = "The pathway has no staging version."
_VERSION_FIELD: Final = "version_number"


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    """The saved version number and the new draft's."""

    saved_version: int
    draft_version: int


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """The published version, its environment, and the draft afterwards."""

    published_version: int
    environment: PinEnvironment
    draft_version: int


@dataclass(frozen=True, slots=True)
class PromoteOutcome:
    """The production version after a promote and whether the pin moved."""

    production_version: int
    moved: bool


@dataclass(frozen=True, slots=True)
class RevertOutcome:
    """The draft after a revert and the save-mode report of its graph."""

    draft: PathwayVersion
    report: ValidationReport


def pinned_number(pathway: Pathway, environment: PinEnvironment) -> int | None:
    """The version number pinned to ``environment``."""
    return pathway.production_version if environment == "production" else pathway.staging_version


async def _existing(repo: PathwayRepo, pathway: Pathway, number: int) -> PathwayVersion:
    version = await repo.version(pathway, number)
    if version is None:
        raise AppError("PATHWAY_VERSION_NOT_FOUND", VERSION_NOT_FOUND_MESSAGE, field=_VERSION_FIELD)
    return version


async def list_versions(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID
) -> tuple[Pathway, list[PathwayVersion]]:
    """The pathway and all its versions, newest first."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id)
    return pathway, await repo.versions(pathway)


async def get_version(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, number: int
) -> tuple[Pathway, PathwayVersion]:
    """The pathway and version ``number``; ``PATHWAY_VERSION_NOT_FOUND`` when it does not exist."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id)
    return pathway, await _existing(repo, pathway, number)


async def save_version(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, name: str | None
) -> SaveOutcome:
    """Freeze the draft as a saved (``archived``) version and open draft ``latest_version + 1``."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    draft = await repo.draft(pathway)
    await repo.freeze(draft, status="archived", name=name)
    new_draft = await repo.fork_draft(pathway, draft)
    after = {"version_number": draft.version_number, "name": draft.name, "graph_hash": draft.graph_hash}
    await audit(session, ctx, "pathway.version_save", "pathway", pathway.id, after=after)
    return SaveOutcome(saved_version=draft.version_number, draft_version=new_draft.version_number)


async def publish_version(  # noqa: PLR0913 - keyword-only body fields and URL policy
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    *,
    version_number: int | None,
    environment: PinEnvironment,
    allow_http: bool,
) -> PublishOutcome:
    """Validate in publish mode, mark ``published``, pin, and fork a new draft when publishing the draft."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    number = pathway.draft_version if version_number is None else version_number
    version = await _existing(repo, pathway, number)
    checked = await check_graph(
        repo, GraphInput.of(version), pathway_id=pathway.id, mode="publish", allow_http=allow_http
    )
    reject_errors(checked)
    previous = pinned_number(pathway, environment)
    was_draft = version.version_number == pathway.draft_version
    await repo.freeze(version, status="published", published_by=await creating_user_id(ctx, session))
    await repo.set_pin(pathway, environment, version.version_number)
    if was_draft:
        await repo.fork_draft(pathway, version)
    await audit(
        session,
        ctx,
        "pathway.publish",
        "pathway",
        pathway.id,
        before={"environment": environment, "version": previous},
        after={"environment": environment, "version": version.version_number},
    )
    return PublishOutcome(
        published_version=version.version_number,
        environment=environment,
        draft_version=pathway.draft_version,
    )


async def promote_staging(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID
) -> PromoteOutcome:
    """Pin the staging version to production; ``CONFLICT`` without one (a repeated promote is a no-op)."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    staging = pathway.staging_version
    if staging is None:
        raise AppError("CONFLICT", NO_STAGING_MESSAGE)
    previous = pathway.production_version
    if previous == staging:
        return PromoteOutcome(production_version=staging, moved=False)
    await repo.set_pin(pathway, "production", staging)
    await audit(
        session,
        ctx,
        "pathway.promote",
        "pathway",
        pathway.id,
        before={"production_version": previous},
        after={"production_version": staging},
    )
    return PromoteOutcome(production_version=staging, moved=True)


async def revert_draft(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, number: int, *, allow_http: bool
) -> RevertOutcome:
    """Replace the draft graph with a copy of version ``number`` (the draft keeps its number and name)."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    source = await _existing(repo, pathway, number)
    draft = await repo.draft(pathway)
    checked = await check_graph(
        repo, GraphInput.of(source), pathway_id=pathway.id, mode="save", allow_http=allow_http
    )
    before = draft.graph_hash
    if source.graph_hash != before:
        await repo.update_draft(pathway, draft, values={}, graph=PathwayGraphJson.of(source))
        await audit(
            session,
            ctx,
            "pathway.revert",
            "pathway",
            pathway.id,
            before={"graph_hash": before},
            after={"graph_hash": draft.graph_hash, "version_number": number},
        )
    return RevertOutcome(draft=draft, report=checked.report)
