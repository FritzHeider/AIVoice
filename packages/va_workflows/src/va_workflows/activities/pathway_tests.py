"""``PathwayTestWorkflow`` activities: run bookkeeping, one unit test or one simulated conversation per
activity, and the publish a blocking regression run settles (specs/12-pathways-api.md §4, §8, and §9,
Decisions P07-T11c, P07-T11d, and P07-T11e2).

Each test or simulation activity writes its own result item into the run row (locked ``FOR UPDATE``, an item
with the same test or simulation id replaced) and returns only the item's status, so large results never
enter the workflow history. The work runs with no database session open (the pool is small; a test or
conversation may take minutes).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, cast

from redis.asyncio import Redis
from temporalio import activity

from va_common.logging import bound_context, get_logger
from va_common.time import Clock
from va_db.org_settings import OrgSettingsReader
from va_db.pathway_cache import invalidate_pathway_cache
from va_db.pathway_publish import settle_regression_publish
from va_db.repositories import PathwayRunRepo
from va_db.repositories.pathway_tests import result_key
from va_db.session import Database
from va_llm.tokens import TokenCounter, default_counter
from va_pathways.conversation import TextDeps
from va_pathways.recording import Item
from va_pathways.runs import StoredSimulation, StoredTest, error_item, run_stored_simulation, run_stored_test
from va_pathways.simulations import simulation_error_item
from va_workflows.activities.pathway_tests_inputs import (
    FinishInput,
    FinishResult,
    RunRef,
    SettleInput,
    SettleResult,
    SimulationErrorInput,
    SimulationInput,
    UnitTestErrorInput,
    UnitTestInput,
    UnitTestOutcome,
)

__all__ = ["PathwayTestActivities"]

_log = get_logger(__name__)


class PathwayTestActivities:
    """The seven activities of ``PathwayTestWorkflow`` (registered on the post-call queue); ``redis`` holds
    the resolved pathway cache a settled publish invalidates (``None``: no cache to invalidate)."""

    def __init__(  # noqa: PLR0913 - keyword-only collaborators
        self,
        *,
        database: Database,
        org_settings: OrgSettingsReader,
        text: TextDeps,
        clock: Clock,
        redis: Redis | None = None,
        counter_factory: Callable[[], TokenCounter] = default_counter,
    ) -> None:
        self._database = database
        self._redis = redis
        self._org_settings = org_settings
        self._text = text
        self._clock = clock
        self._counter_factory = counter_factory
        self._counter: TokenCounter | None = None
        self._counter_lock = asyncio.Lock()

    async def _token_counter(self) -> TokenCounter:
        """The token counter, loaded once (reading the vocabulary is CPU work: off the event loop)."""
        async with self._counter_lock:
            if self._counter is None:
                self._counter = await asyncio.to_thread(self._counter_factory)
            return self._counter

    @activity.defn(name="begin_pathway_test_run")
    async def begin_pathway_test_run(self, request: RunRef) -> bool:
        """``queued`` → ``running``; ``False`` when the run row is gone or already finished."""
        async with self._database.tenant_session(request.org_id) as session:
            return await PathwayRunRepo(session, request.org_id).begin(request.run_id, self._clock.now())

    @activity.defn(name="run_pathway_unit_test")
    async def run_pathway_unit_test(self, request: UnitTestInput) -> UnitTestOutcome:
        """Run one test and store its result item; returns the item's status."""
        with bound_context(org_id=str(request.org_id), run_id=str(request.run_id)):
            item = await run_stored_test(
                self._text,
                self._database,
                self._org_settings,
                StoredTest(
                    org_id=request.org_id,
                    pathway_id=request.pathway_id,
                    version_number=request.version_number,
                    test_id=request.test_id,
                ),
                counter=await self._token_counter(),
            )
            return await self._store(request, item)

    @activity.defn(name="record_pathway_test_error")
    async def record_pathway_test_error(self, request: UnitTestErrorInput) -> UnitTestOutcome:
        """Store an ``error`` item for a test whose activity failed after its retries."""
        with bound_context(org_id=str(request.org_id), run_id=str(request.run_id)):
            # A result an earlier attempt stored before failing wins over the error.
            return await self._store(request, error_item(request.test_id, None, request.error), replace=False)

    @activity.defn(name="run_pathway_simulation")
    async def run_pathway_simulation(self, request: SimulationInput) -> UnitTestOutcome:
        """Run one simulated conversation and store its result item; returns the item's status."""
        with bound_context(org_id=str(request.org_id), run_id=str(request.run_id)):
            item = await run_stored_simulation(
                self._text,
                self._database,
                self._org_settings,
                StoredSimulation(
                    org_id=request.org_id,
                    pathway_id=request.pathway_id,
                    version_number=request.version_number,
                    job=request.job,
                ),
                counter=await self._token_counter(),
            )
            return await self._store(request, item)

    @activity.defn(name="record_pathway_simulation_error")
    async def record_pathway_simulation_error(self, request: SimulationErrorInput) -> UnitTestOutcome:
        """Store an ``error`` item for a conversation whose activity failed after its retries."""
        with bound_context(org_id=str(request.org_id), run_id=str(request.run_id)):
            item = simulation_error_item(request.job, request.error)
            return await self._store(request, item, replace=False)

    async def _store(self, request: RunRef, item: Item, *, replace: bool = True) -> UnitTestOutcome:
        async with self._database.tenant_session(request.org_id) as session:
            repo = PathwayRunRepo(session, request.org_id)
            stored = await repo.put_result(request.run_id, item, replace=replace)
        if not stored:
            _log.info("pathway_test_run.result_dropped", item_key=result_key(item))
            return "skipped"
        return cast("Literal['passed', 'failed', 'error']", item["status"])

    @activity.defn(name="finish_pathway_test_run")
    async def finish_pathway_test_run(self, request: FinishInput) -> FinishResult:
        """Order the results, set the final status and ``finished_at``."""
        order = [str(test_id) for test_id in request.test_ids] + list(request.simulation_ids)
        async with self._database.tenant_session(request.org_id) as session:
            status = await PathwayRunRepo(session, request.org_id).finish(
                request.run_id, order, self._clock.now()
            )
        _log.info("pathway_test_run.finished", run_id=str(request.run_id), status=status, items=len(order))
        return FinishResult(status=status)

    @activity.defn(name="settle_regression_publish")
    async def settle_regression_publish(self, request: SettleInput) -> SettleResult:
        """Publish the version when the finished run passed and its graph is unchanged; record the outcome on
        the run and invalidate the environment's resolved pathway cache after a publish."""
        intent = request.publish
        async with self._database.tenant_session(request.org_id) as session:
            settled = await settle_regression_publish(
                session, request.org_id, request.run_id, intent, now=self._clock.now()
            )
        if settled is None:
            _log.info("pathway_publish.settle_skipped", run_id=str(request.run_id))
            return SettleResult(outcome=None)
        if settled.outcome == "published" and self._redis is not None:
            await invalidate_pathway_cache(self._redis, request.pathway_id, [intent.environment])
        _log.info(
            "pathway_publish.settled",
            run_id=str(request.run_id),
            outcome=settled.outcome,
            environment=intent.environment,
            version=intent.version_number,
        )
        return SettleResult(outcome=settled.outcome)
