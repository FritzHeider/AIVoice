"""The synchronous regression gate of a publish (specs/12-pathways-api.md §4 step 2; Decision P07-T11e3).

A blocking suite of 1-20 unit tests and no simulations runs inside the publish request: the run row is already
``running`` (inserted by the publish transaction), :func:`run_sync_suite` runs the tests at most
:data:`SYNC_CONCURRENCY` at a time, each through :func:`va_pathways.runs.run_stored_test` with no database
session held while it runs, stores every result item, and closes the run; the whole suite is capped at
:data:`SYNC_TIMEOUT_S` seconds of the injected clock (unfinished tests become ``regression_timeout`` items).
The closing step (timeout items, ``finish``, and the settle through
:func:`va_db.pathway_publish.settle_regression_publish`) runs shielded, so a request cancelled mid-run never
leaves the run ``running`` or its publish ``pending``. :func:`conflict_for` turns any outcome other than
``published`` into the 409 ``CONFLICT`` the request answers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_db.org_settings import OrgSettingsReader
from va_db.pathway_publish import SettleResult, settle_regression_publish
from va_db.repositories import PathwayRunRepo
from va_db.session import Database
from va_domain.pathway_tests import PublishOutcome, RegressionPublish
from va_llm.tokens import default_counter
from va_pathways.conversation import TextDeps
from va_pathways.recording import Item
from va_pathways.runs import RunError, StoredTest, error_item, run_stored_test

__all__ = [
    "CONFLICT_MESSAGES",
    "SYNC_CONCURRENCY",
    "SYNC_MAX_TESTS",
    "SYNC_TIMEOUT_S",
    "RegressionTestRunner",
    "StoredTestRunner",
    "SyncSuite",
    "conflict_for",
    "run_sync_suite",
]

SYNC_MAX_TESTS: Final = 20
SYNC_TIMEOUT_S: Final = 120.0
SYNC_CONCURRENCY: Final = 8

CONFLICT_MESSAGES: Final[Mapping[PublishOutcome, str]] = {
    "blocked": "The regression run did not pass; the version was not published.",
    "stale": "The pathway changed during the regression run; publish again.",
    "version_unavailable": "The pathway version is no longer available.",
    "pending": "The regression run did not finish; the version was not published.",
}

_log = get_logger(__name__)


class RegressionTestRunner(Protocol):
    """Runs one stored unit test of a run and returns its result item."""

    async def run(self, test: StoredTest) -> Item: ...


class StoredTestRunner:
    """The production runner: :func:`run_stored_test` over the api's LLM gateway and tools HTTP client."""

    def __init__(self, *, text: TextDeps, database: Database, org_settings: OrgSettingsReader) -> None:
        self._text = text
        self._database = database
        self._org_settings = org_settings

    async def run(self, test: StoredTest) -> Item:
        # ``default_counter`` is cached; the first call reads the vocabulary (CPU work: off the event loop).
        counter = await asyncio.to_thread(default_counter)
        return await run_stored_test(self._text, self._database, self._org_settings, test, counter=counter)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncSuite:
    """A ``running`` regression run to execute in the request and the publish it settles."""

    org_id: uuid.UUID
    pathway_id: uuid.UUID
    run_id: uuid.UUID
    test_ids: tuple[uuid.UUID, ...]
    intent: RegressionPublish


class _Execution:
    """One suite's tests and the ids whose result items are stored."""

    def __init__(self, database: Database, runner: RegressionTestRunner, suite: SyncSuite) -> None:
        self._database = database
        self._runner = runner
        self._suite = suite
        self._limit = asyncio.Semaphore(SYNC_CONCURRENCY)
        self.stored: set[uuid.UUID] = set()

    async def store(self, item: Item, *, replace: bool = True) -> None:
        suite = self._suite
        async with self._database.tenant_session(suite.org_id) as session:
            await PathwayRunRepo(session, suite.org_id).put_result(suite.run_id, item, replace=replace)

    async def _one(self, test_id: uuid.UUID) -> None:
        suite = self._suite
        async with self._limit:
            test = StoredTest(
                org_id=suite.org_id,
                pathway_id=suite.pathway_id,
                version_number=suite.intent.version_number,
                test_id=test_id,
            )
            try:
                item = await self._runner.run(test)
            except Exception as exc:
                _log.warning(
                    "pathway_publish.sync_test_failed", test_id=str(test_id), error=type(exc).__name__
                )
                item = error_item(test_id, None, "runner_error")
            await self.store(item)
            self.stored.add(test_id)

    async def run_all(self) -> None:
        async with asyncio.TaskGroup() as group:
            for test_id in self._suite.test_ids:
                group.create_task(self._one(test_id))

    def missing(self) -> Sequence[uuid.UUID]:
        return [test_id for test_id in self._suite.test_ids if test_id not in self.stored]


async def _conclude(
    database: Database, clock: Clock, execution: _Execution, suite: SyncSuite, missing_error: RunError | None
) -> SettleResult:
    """Store an item for every unfinished test (none when ``missing_error`` is ``None``: ``finish`` counts it
    as an error), close the run, and settle its publish."""
    if missing_error is not None:
        for test_id in execution.missing():
            await execution.store(error_item(test_id, None, missing_error), replace=False)
    order = [str(test_id) for test_id in suite.test_ids]
    async with database.tenant_session(suite.org_id) as session:
        status = await PathwayRunRepo(session, suite.org_id).finish(suite.run_id, order, clock.now())
    async with database.tenant_session(suite.org_id) as session:
        settled = await settle_regression_publish(
            session, suite.org_id, suite.run_id, suite.intent, now=clock.now()
        )
    result = SettleResult("version_unavailable") if settled is None else settled
    _log.info(
        "pathway_publish.sync_settled",
        run_id=str(suite.run_id),
        status=status,
        outcome=result.outcome,
        tests=len(suite.test_ids),
    )
    return result


async def run_sync_suite(
    database: Database,
    runner: RegressionTestRunner,
    clock: Clock,
    suite: SyncSuite,
    *,
    timeout_s: float = SYNC_TIMEOUT_S,
) -> SettleResult:
    """Run the suite in the request (at most :data:`SYNC_CONCURRENCY` tests at once, ``timeout_s`` in all),
    close the run, and settle its publish; a cancelled caller still gets the run closed (unfinished tests
    count as errors, so the publish is ``blocked``) before the cancellation propagates."""
    execution = _Execution(database, runner, suite)
    missing_error: RunError | None = None
    try:
        finished = await within(execution.run_all(), clock, timeout_s)
        if not finished:
            _log.warning("pathway_publish.sync_timeout", run_id=str(suite.run_id), timeout_s=timeout_s)
            missing_error = "regression_timeout"
    finally:
        closing = asyncio.ensure_future(_conclude(database, clock, execution, suite, missing_error))
        result = await asyncio.shield(closing)
    return result


def conflict_for(result: SettleResult, run_id: uuid.UUID) -> AppError:
    """The 409 a publish answers when its synchronous run did not publish the version."""
    return AppError(
        "CONFLICT",
        CONFLICT_MESSAGES[result.outcome],
        details={"run_id": str(run_id), "outcome": result.outcome},
    )
