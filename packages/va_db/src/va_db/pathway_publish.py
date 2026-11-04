"""Publishing a pathway version (specs/12-pathways-api.md §4 steps 2-3; Decisions P07-T09b, P07-T11e1, and
P07-T11e2).

:func:`apply_publish` is the part of a publish shared by the api (a direct publish) and the worker (a publish
completed after an asynchronous regression run): mark the version ``published``, pin it to the environment,
fork a new draft when the version was the draft, and write the ``pathway.publish`` audit row. Validation and
the resolved-graph cache invalidation stay with the caller. The caller holds the pathway row lock
(``PathwayRepo.get(..., lock=True)``) in the same transaction.

:func:`settle_regression_publish` completes (or refuses) the publish a blocking regression run gates, once the
run finished; :func:`publish_record` builds the ``pathway_test_runs.publish`` value both sides store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy.ext.asyncio import AsyncSession

from va_db.audit import AuditActor, record
from va_db.models import Pathway, PathwayTestRun, PathwayVersion
from va_db.repositories import PathwayRepo, PathwayRunRepo, PinEnvironment
from va_db.repositories.base import NotFoundError
from va_domain.pathway_tests import PublishActor, PublishOutcome, RegressionPublish

__all__ = [
    "PublishResult",
    "SettleResult",
    "actor_from",
    "apply_publish",
    "pinned_number",
    "publish_record",
    "settle_regression_publish",
]

_UNSETTLED: Final = (None, "pending")
_LIVE_RUN: Final = ("queued", "running")


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


@dataclass(frozen=True, slots=True)
class SettleResult:
    """What became of a regression run's publish; the versions only when it was ``published``."""

    outcome: PublishOutcome
    published_version: int | None = None
    draft_version: int | None = None


def actor_from(actor: PublishActor) -> AuditActor:
    """The audit actor carried in a workflow input."""
    return AuditActor(
        org_id=actor.org_id,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        ip=actor.ip,
        user_agent=actor.user_agent,
        acting_parent_org_id=actor.acting_parent_org_id,
        platform_admin=actor.platform_admin,
    )


def publish_record(  # noqa: PLR0913 - keyword-only fields of the stored record
    *,
    block: bool,
    environment: PinEnvironment,
    version_number: int,
    graph_hash: str,
    outcome: PublishOutcome,
    published_version: int | None = None,
    draft_version: int | None = None,
    settled_at: datetime | None = None,
) -> dict[str, Any]:
    """The ``pathway_test_runs.publish`` value: the publish a regression run belongs to and its outcome
    (``published_version``/``draft_version`` once published, ``settled_at`` once a blocking run settled)."""
    stored: dict[str, Any] = {
        "block": block,
        "environment": environment,
        "version_number": version_number,
        "graph_hash": graph_hash,
        "outcome": outcome,
    }
    if published_version is not None:
        stored["published_version"] = published_version
    if draft_version is not None:
        stored["draft_version"] = draft_version
    if settled_at is not None:
        stored["settled_at"] = settled_at.isoformat()
    return stored


def _stored_result(stored: dict[str, Any]) -> SettleResult:
    return SettleResult(
        outcome=cast("PublishOutcome", stored["outcome"]),
        published_version=stored.get("published_version"),
        draft_version=stored.get("draft_version"),
    )


async def _decide(
    session: AsyncSession, org_id: uuid.UUID, run: PathwayTestRun, intent: RegressionPublish
) -> SettleResult:
    if run.status != "passed":
        return SettleResult("blocked")
    repo = PathwayRepo(session, org_id)
    try:
        pathway = await repo.get(run.pathway_id, lock=True)
    except NotFoundError:
        return SettleResult("version_unavailable")
    version = await repo.version(pathway, intent.version_number)
    if version is None:
        return SettleResult("version_unavailable")
    if version.graph_hash != intent.graph_hash:
        return SettleResult("stale")
    published = await apply_publish(
        session,
        repo,
        pathway,
        version,
        environment=intent.environment,
        published_by=intent.published_by,
        actor=actor_from(intent.actor),
    )
    return SettleResult("published", published.published_version, published.draft_version)


async def settle_regression_publish(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    intent: RegressionPublish,
    *,
    now: datetime,
) -> SettleResult | None:
    """Settle the publish of the finished regression run ``run_id`` in ``session``'s transaction: publish
    when the run passed and the version's graph still has the validated hash (``published``), else
    ``blocked`` (the run did not pass), ``stale`` (the graph changed), or ``version_unavailable`` (pathway or
    version gone). The run row is locked first; a run already settled returns its stored outcome (a retried
    activity never publishes twice) and a run still in progress stays ``pending``. ``None`` when the run row
    is gone."""
    runs = PathwayRunRepo(session, org_id)
    run = await runs.lock(run_id)
    if run is None:
        return None
    stored: dict[str, Any] = dict(run.publish) if isinstance(run.publish, dict) else {}
    if stored.get("outcome") not in _UNSETTLED:
        return _stored_result(stored)
    if run.status in _LIVE_RUN:
        return SettleResult("pending")
    result = await _decide(session, org_id, run, intent)
    await runs.set_publish(
        run,
        publish_record(
            block=True,
            environment=intent.environment,
            version_number=intent.version_number,
            graph_hash=intent.graph_hash,
            outcome=result.outcome,
            published_version=result.published_version,
            draft_version=result.draft_version,
            settled_at=now,
        ),
    )
    return result
