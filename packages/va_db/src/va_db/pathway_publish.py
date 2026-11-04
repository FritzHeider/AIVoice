"""Publishing a pathway version (specs/12-pathways-api.md §4 step 3; Decisions P07-T09b and P07-T11e1).

:func:`apply_publish` is the part of a publish shared by the api (a direct publish) and the worker (a publish
completed after an asynchronous regression run): mark the version ``published``, pin it to the environment,
fork a new draft when the version was the draft, and write the ``pathway.publish`` audit row. Validation and
the resolved-graph cache invalidation stay with the caller. The caller holds the pathway row lock
(``PathwayRepo.get(..., lock=True)``) in the same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from va_db.audit import AuditActor, record
from va_db.models import Pathway, PathwayVersion
from va_db.repositories import PathwayRepo, PinEnvironment

__all__ = ["PublishResult", "apply_publish", "pinned_number"]


@dataclass(frozen=True, slots=True)
class PublishResult:
    """The published version, its environment, the draft afterwards, and the version pinned before."""

    published_version: int
    environment: PinEnvironment
    draft_version: int
    previous: int | None


def pinned_number(pathway: Pathway, environment: PinEnvironment) -> int | None:
    """The version number pinned to ``environment``."""
    return pathway.production_version if environment == "production" else pathway.staging_version


async def apply_publish(  # noqa: PLR0913 - the pathway, its version, and keyword-only publish facts
    session: AsyncSession,
    repo: PathwayRepo,
    pathway: Pathway,
    version: PathwayVersion,
    *,
    environment: PinEnvironment,
    published_by: uuid.UUID | None,
    actor: AuditActor,
) -> PublishResult:
    """Publish ``version`` of the locked ``pathway`` to ``environment`` in ``session``'s transaction."""
    previous = pinned_number(pathway, environment)
    was_draft = version.version_number == pathway.draft_version
    await repo.freeze(version, status="published", published_by=published_by)
    await repo.set_pin(pathway, environment, version.version_number)
    if was_draft:
        await repo.fork_draft(pathway, version)
    await record(
        session,
        actor,
        "pathway.publish",
        "pathway",
        pathway.id,
        before={"environment": environment, "version": previous},
        after={"environment": environment, "version": version.version_number},
    )
    return PublishResult(
        published_version=version.version_number,
        environment=environment,
        draft_version=pathway.draft_version,
        previous=previous,
    )
