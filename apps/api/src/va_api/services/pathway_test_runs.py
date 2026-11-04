"""Starting a pathway unit-test run (``POST /v1/pathway/{pathway_id}/tests/run``; specs/12-pathways-api.md
§8, Decision P07-T11c) or a simulation run (``POST /v1/pathway/{pathway_id}/simulations``; §9, Decision
P07-T11d).

A unit run resolves the version to a number, selects the tests (every test of the pathway oldest first, or
the given ids in order without duplicates); a simulation run resolves the version and expands the scenarios
into conversations (:func:`va_domain.pathway_tests.simulation_jobs`). Both insert a ``queued``
``pathway_test_runs`` row, commit, and then start ``PathwayTestWorkflow`` on the post-call queue. When the
start fails the run is marked ``error`` and the request answers ``SERVICE_UNAVAILABLE``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final, Protocol

from va_api.auth.context import RequestContext
from va_api.services.pathways import VersionSelector, load_version
from va_common.checks import TemporalConnector, WorkflowStartError, start_workflow
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.models import PathwayTestRun
from va_db.repositories import PathwayRunRepo, PathwayTestRepo
from va_db.repositories.base import NotFoundError
from va_db.repositories.pathway_tests import TEST_RESOURCE
from va_db.session import Database
from va_domain.pathway_tests import (
    MAX_RUN_TESTS,
    PATHWAY_TEST_WORKFLOW_NAME,
    PathwayTestWorkflowInput,
    Scenario,
    simulation_jobs,
)

__all__ = [
    "NO_TESTS_MESSAGE",
    "RUNS_UNAVAILABLE_MESSAGE",
    "TOO_MANY_TESTS_MESSAGE",
    "PathwayTestStarter",
    "RunStartError",
    "TemporalPathwayTestStarter",
    "start_simulation_run",
    "start_unit_run",
]

NO_TESTS_MESSAGE: Final = "There are no unit tests to run."
TOO_MANY_TESTS_MESSAGE: Final = f"Select at most {MAX_RUN_TESTS} tests."
RUNS_UNAVAILABLE_MESSAGE: Final = "Test runs are unavailable; try again."
START_TIMEOUT_S: Final = 10.0
START_RPC_TIMEOUT_S: Final = 5.0
_FIELD: Final = "test_ids"

_log = get_logger(__name__)


class RunStartError(Exception):
    """``PathwayTestWorkflow`` could not be started (Temporal unreachable, refused, or too slow)."""


class PathwayTestStarter(Protocol):
    """Starts the workflow of one committed run; raises :class:`RunStartError` on failure."""

    async def start(self, run: PathwayTestWorkflowInput) -> None: ...


class TemporalPathwayTestStarter:
    """Starts ``PathwayTestWorkflow`` on ``task_queue`` (the post-call queue) through ``connector``."""

    def __init__(self, connector: TemporalConnector, task_queue: str) -> None:
        self._connector = connector
        self._task_queue = task_queue

    async def start(self, run: PathwayTestWorkflowInput) -> None:
        try:
            started = await start_workflow(
                self._connector,
                PATHWAY_TEST_WORKFLOW_NAME,
                run.model_dump(mode="json"),
                workflow_id=run.workflow_id(),
                task_queue=self._task_queue,
                timeout_s=START_TIMEOUT_S,
                rpc_timeout_s=START_RPC_TIMEOUT_S,
            )
        except WorkflowStartError as exc:
            msg = f"PathwayTestWorkflow for run {run.run_id} could not be started"
            raise RunStartError(msg) from exc.__cause__
        if not started:
            _log.info("pathway_test_run.workflow_already_started", run_id=str(run.run_id))


def _unique(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    return list(dict.fromkeys(ids))


async def _selected_tests(
    repo: PathwayTestRepo, pathway_id: uuid.UUID, test_ids: Sequence[uuid.UUID] | None
) -> list[uuid.UUID]:
    if test_ids is None:
        ids = await repo.ordered_ids(pathway_id, limit=MAX_RUN_TESTS + 1)
        if len(ids) > MAX_RUN_TESTS:
            raise AppError("INVALID_INPUT", TOO_MANY_TESTS_MESSAGE, field=_FIELD)
    else:
        ids = _unique(test_ids)
        found = await repo.find(pathway_id, ids)
        if any(test_id not in found for test_id in ids):
            raise NotFoundError(TEST_RESOURCE)
    if not ids:
        raise AppError("INVALID_INPUT", NO_TESTS_MESSAGE, field=_FIELD)
    return ids


async def _launch(
    database: Database,
    starter: PathwayTestStarter,
    clock: Clock,
    run: PathwayTestRun,
    start: PathwayTestWorkflowInput,
) -> None:
    """Start the committed ``run``'s workflow; a failed start marks the run ``error`` and answers 503."""
    try:
        await starter.start(start)
    except RunStartError as exc:
        _log.warning("pathway_test_run.start_failed", run_id=str(run.id), error=type(exc.__cause__).__name__)
        async with database.tenant_session(start.org_id) as session:
            await PathwayRunRepo(session, start.org_id).fail(run.id, clock.now())
        raise AppError("SERVICE_UNAVAILABLE", RUNS_UNAVAILABLE_MESSAGE) from exc
    _log.info(
        "pathway_test_run.started",
        run_id=str(run.id),
        kind=start.kind,
        items=len(start.test_ids) + len(start.simulations),
        version=run.version_number,
    )


def _start_input(run: PathwayTestRun, ctx: RequestContext, **work: object) -> PathwayTestWorkflowInput:
    """The workflow input of ``run`` with its ``kind`` and work (``test_ids`` or ``simulations``)."""
    return PathwayTestWorkflowInput.model_validate(
        {
            "run_id": run.id,
            "org_id": ctx.org_id,
            "pathway_id": run.pathway_id,
            "version_number": run.version_number,
            **work,
        }
    )


async def start_unit_run(  # noqa: PLR0913 - the request's run, its version and tests, and the collaborators
    database: Database,
    starter: PathwayTestStarter,
    clock: Clock,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    *,
    version: VersionSelector,
    test_ids: Sequence[uuid.UUID] | None,
) -> PathwayTestRun:
    """Insert and start a unit run; returns the ``queued`` run row."""
    async with database.tenant_session(ctx.org_id) as session:
        pathway, stored = await load_version(session, ctx, pathway_id, version)
        ids = await _selected_tests(PathwayTestRepo(session, ctx.org_id), pathway.id, test_ids)
        run = await PathwayRunRepo(session, ctx.org_id).add(
            pathway.id, version_number=stored.version_number, kind="unit"
        )
    await _launch(database, starter, clock, run, _start_input(run, ctx, kind="unit", test_ids=tuple(ids)))
    return run


async def start_simulation_run(  # noqa: PLR0913 - the request's run, its version and scenarios, and the collaborators
    database: Database,
    starter: PathwayTestStarter,
    clock: Clock,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    *,
    version: VersionSelector,
    scenarios: Sequence[Scenario],
    runs_per_scenario: int,
) -> PathwayTestRun:
    """Insert and start a simulation run (``runs_per_scenario`` conversations of every scenario); returns
    the ``queued`` run row."""
    async with database.tenant_session(ctx.org_id) as session:
        pathway, stored = await load_version(session, ctx, pathway_id, version)
        run = await PathwayRunRepo(session, ctx.org_id).add(
            pathway.id, version_number=stored.version_number, kind="simulation"
        )
    jobs = simulation_jobs(scenarios, runs_per_scenario)
    await _launch(database, starter, clock, run, _start_input(run, ctx, kind="simulation", simulations=jobs))
    return run
