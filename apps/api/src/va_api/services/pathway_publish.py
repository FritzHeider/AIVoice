"""Publishing a pathway version with the regression gate (specs/12-pathways-api.md §4; Decisions P07-T09b and
P07-T11e2).

Step 1 validates the version in publish mode (one ``INVALID_INPUT`` item per error). Step 2: regression runs
when the request sets ``run_regression`` or the draft's suite settings opt in with ``run_on_publish``; the
suite is every unit test of the pathway (oldest first, at most 500) plus, with ``include_simulations``, one
conversation per scenario. An empty suite publishes at once. With ``block_publish_on_failure`` the request
inserts a ``regression`` run whose publish record is ``pending``, starts ``PathwayTestWorkflow`` with the
publish intent, and answers ``pending_regression``: the worker publishes when the run passes. Otherwise the
version is published now and the regression run (publish record ``published``) only reports. Cache
invalidation of a completed publish is the router's job.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import actor_of
from va_api.auth.api_keys import creating_user_id
from va_api.auth.context import RequestContext
from va_api.services.pathway_test_runs import PathwayTestStarter, launch_run
from va_api.services.pathway_versions import existing_version
from va_api.services.pathways import GraphInput, check_graph, reject_errors
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.models import Pathway, PathwayTestRun, PathwayVersion
from va_db.pathway_publish import apply_publish, publish_record
from va_db.repositories import PathwayRepo, PathwayRunRepo, PathwayTestRepo, PinEnvironment
from va_db.session import Database
from va_domain.pathway_tests import (
    MAX_RUN_TESTS,
    PathwayTestWorkflowInput,
    PublishActor,
    RegressionPublish,
    SimulationJob,
    load_regression,
    regression_enabled,
    regression_jobs,
)

__all__ = ["SUITE_TOO_LARGE_MESSAGE", "PublishDeps", "PublishOutcome", "PublishRequest", "publish_pathway"]

SUITE_TOO_LARGE_MESSAGE: Final = f"The regression suite holds more than {MAX_RUN_TESTS} unit tests."
_REGRESSION_FIELD: Final = "run_regression"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PublishDeps:
    """The collaborators of a publish."""

    database: Database
    starter: PathwayTestStarter
    clock: Clock


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """``POST /v1/pathway/{id}/publish``: the version (``None`` = the draft), its environment, the regression
    flag, and whether publish-mode validation accepts http URLs."""

    version_number: int | None
    environment: PinEnvironment
    run_regression: bool | None
    allow_http: bool


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """``success`` (published now; ``published_version`` set) or ``pending_regression`` (the worker publishes
    when the run passes); ``run_id`` is the regression run, if any."""

    status: Literal["success", "pending_regression"]
    environment: PinEnvironment
    version_number: int
    draft_version: int
    published_version: int | None
    run_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _Suite:
    test_ids: tuple[uuid.UUID, ...]
    simulations: tuple[SimulationJob, ...]
    block: bool


@dataclass(frozen=True, slots=True)
class _Planned:
    """What the transaction decided: the outcome and, with a regression run, its workflow input."""

    outcome: PublishOutcome
    run: PathwayTestRun | None = None
    start: PathwayTestWorkflowInput | None = None


async def _suite(
    session: AsyncSession,
    ctx: RequestContext,
    pathway: Pathway,
    draft: PathwayVersion,
    run_regression: bool | None,
) -> _Suite | None:
    """The regression suite to run, or ``None`` when regression is off or the suite is empty."""
    settings = load_regression(draft.global_config)
    if not regression_enabled(run_regression, settings):
        return None
    test_ids = await PathwayTestRepo(session, ctx.org_id).ordered_ids(pathway.id, limit=MAX_RUN_TESTS + 1)
    if len(test_ids) > MAX_RUN_TESTS:
        raise AppError("INVALID_INPUT", SUITE_TOO_LARGE_MESSAGE, field=_REGRESSION_FIELD)
    simulations = regression_jobs(settings)
    if not test_ids and not simulations:
        _log.info("pathway_publish.regression_empty", pathway_id=str(pathway.id))
        return None
    return _Suite(tuple(test_ids), simulations, settings.block_publish_on_failure)


def _start(
    run: PathwayTestRun, ctx: RequestContext, suite: _Suite, intent: RegressionPublish | None
) -> PathwayTestWorkflowInput:
    return PathwayTestWorkflowInput(
        run_id=run.id,
        org_id=ctx.org_id,
        pathway_id=run.pathway_id,
        version_number=run.version_number,
        kind="regression",
        test_ids=suite.test_ids,
        simulations=suite.simulations,
        publish=intent,
    )


async def _plan(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, request: PublishRequest
) -> _Planned:
    """Steps 1-3 in one transaction: validate, then publish now and/or insert the regression run."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    number = pathway.draft_version if request.version_number is None else request.version_number
    version = await existing_version(repo, pathway, number)
    checked = await check_graph(
        repo, GraphInput.of(version), pathway_id=pathway.id, mode="publish", allow_http=request.allow_http
    )
    reject_errors(checked)
    suite = await _suite(session, ctx, pathway, await repo.draft(pathway), request.run_regression)
    published_by = await creating_user_id(ctx, session)
    actor = actor_of(ctx)
    runs = PathwayRunRepo(session, ctx.org_id)
    if suite is not None and suite.block:
        intent = RegressionPublish(
            environment=request.environment,
            version_number=number,
            graph_hash=version.graph_hash,
            published_by=published_by,
            actor=PublishActor(**dataclasses.asdict(actor)),
        )
        record = publish_record(
            block=True,
            environment=request.environment,
            version_number=number,
            graph_hash=version.graph_hash,
            outcome="pending",
        )
        run = await runs.add(pathway.id, version_number=number, kind="regression", publish=record)
        pending = PublishOutcome(
            status="pending_regression",
            environment=request.environment,
            version_number=number,
            draft_version=pathway.draft_version,
            published_version=None,
            run_id=run.id,
        )
        return _Planned(pending, run, _start(run, ctx, suite, intent))
    result = await apply_publish(
        session,
        repo,
        pathway,
        version,
        environment=request.environment,
        published_by=published_by,
        actor=actor,
    )
    published = PublishOutcome(
        status="success",
        environment=result.environment,
        version_number=result.published_version,
        draft_version=result.draft_version,
        published_version=result.published_version,
        run_id=None,
    )
    if suite is None:
        return _Planned(published)
    record = publish_record(
        block=False,
        environment=result.environment,
        version_number=result.published_version,
        graph_hash=version.graph_hash,
        outcome="published",
        published_version=result.published_version,
        draft_version=result.draft_version,
    )
    run = await runs.add(
        pathway.id, version_number=result.published_version, kind="regression", publish=record
    )
    return _Planned(dataclasses.replace(published, run_id=run.id), run, _start(run, ctx, suite, None))


async def publish_pathway(
    deps: PublishDeps, ctx: RequestContext, pathway_id: uuid.UUID, request: PublishRequest
) -> PublishOutcome:
    """Publish (or gate the publish of) a version; a blocking run whose workflow cannot start is marked
    ``error`` and answers ``SERVICE_UNAVAILABLE`` (nothing published), a reporting run's failed start is only
    logged (the publish already committed)."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        planned = await _plan(session, ctx, pathway_id, request)
    if planned.run is not None and planned.start is not None:
        blocking = planned.start.publish is not None
        await launch_run(
            deps.database, deps.starter, deps.clock, planned.run, planned.start, unavailable_raises=blocking
        )
    return planned.outcome
