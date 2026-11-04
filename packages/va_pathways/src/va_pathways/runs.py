"""Runs one stored unit test or one simulation of a ``PathwayTestWorkflow`` run (specs/12-pathways-api.md
§8 and §9; Decisions P07-T11c and P07-T11d).

:func:`run_stored_test` reads the test row and the run's pathway version from the database (each in a short
tenant session, closed while the test runs) and runs it with :func:`va_pathways.unit_tests.run_unit_test`.
It never raises for data that disappeared meanwhile: a deleted test is an ``error`` item with
``test_not_found``, a pathway or version that no longer loads one with ``version_unavailable``, and a stored
row that no longer validates one with ``invalid_test``. :func:`run_stored_simulation` loads the version the
same way and runs one conversation with :func:`va_pathways.simulations.run_simulation`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

from va_common.logging import get_logger
from va_common.settings import Environment
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import PathwayTestRepo
from va_db.repositories.base import NotFoundError
from va_db.session import Database
from va_domain.org import OrgSettings
from va_domain.pathway_tests import SimulationJob
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_llm.tokens import TokenCounter
from va_pathways.conversation import TextDeps
from va_pathways.loader import DbPathwayLoader
from va_pathways.recording import Item
from va_pathways.simulations import run_simulation, simulation_error_item
from va_pathways.unit_tests import UnitTestSpec, run_unit_test

__all__ = [
    "RunError",
    "StoredSimulation",
    "StoredTest",
    "error_item",
    "insecure_urls_allowed",
    "run_stored_simulation",
    "run_stored_test",
]

type RunError = Literal[
    "test_not_found", "version_unavailable", "invalid_test", "runner_error", "regression_timeout"
]

_INSECURE_ENVS: Final = frozenset({Environment.LOCAL, Environment.TEST})

_log = get_logger(__name__)


def insecure_urls_allowed(va_env: Environment) -> bool:
    """``http`` and private webhook URLs pass validation (and run) only in ``local``/``test``."""
    return va_env in _INSECURE_ENVS


def error_item(test_id: uuid.UUID, name: str | None, error: RunError) -> Item:
    """The result item of a test that could not run."""
    return {
        "test_id": str(test_id),
        "name": name,
        "passed": False,
        "status": "error",
        "cases": [],
        "duration_ms": 0,
        "error": error,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredTest:
    """Where a run's test lives: the org, the pathway, the version every test of the run loads, the test."""

    org_id: uuid.UUID
    pathway_id: uuid.UUID
    version_number: int
    test_id: uuid.UUID


async def _load_spec(database: Database, test: StoredTest) -> tuple[str | None, UnitTestSpec | RunError]:
    async with database.tenant_session(test.org_id) as session:
        try:
            row = await PathwayTestRepo(session, test.org_id).get(test.pathway_id, test.test_id)
        except NotFoundError:
            return None, "test_not_found"
        name = row.name
        raw = {
            "name": row.name,
            "node_id": row.node_id,
            "history": list(row.history),
            "user_message": row.user_message,
            "expected": dict(row.expected),
            "simulated_variations": row.simulated_variations,
        }
    try:
        return name, UnitTestSpec.model_validate(raw)
    except ValidationError:
        return name, "invalid_test"


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSimulation:
    """One conversation of a simulation run: the org, the pathway, the version the run loads, the job."""

    org_id: uuid.UUID
    pathway_id: uuid.UUID
    version_number: int
    job: SimulationJob


@dataclass(frozen=True, slots=True)
class _Target:
    loader: DbPathwayLoader
    active: ActiveGraph
    org: OrgSettings


async def _load_target(
    database: Database,
    org_settings: OrgSettingsReader,
    org_id: uuid.UUID,
    pathway_id: uuid.UUID,
    version_number: int,
) -> _Target | None:
    """The run's version, its loader and the org's settings; ``None`` when the version no longer loads."""
    loader = DbPathwayLoader(database, org_id)
    try:
        active = await loader.load(str(pathway_id), version_number)
    except PathwayUnavailableError as exc:
        _log.info("pathway_test_run.version_unavailable", pathway_id=str(pathway_id), reason=str(exc))
        return None
    org = await org_settings.get_lenient(org_id)
    return _Target(loader, active, OrgSettings() if org is None else org)


async def run_stored_test(
    deps: TextDeps,
    database: Database,
    org_settings: OrgSettingsReader,
    test: StoredTest,
    *,
    counter: TokenCounter,
) -> Item:
    """The result item of ``test`` against the run's version (never raises for missing data or a failing
    pathway; database and model infrastructure errors propagate, so the activity retries)."""
    name, spec = await _load_spec(database, test)
    if not isinstance(spec, UnitTestSpec):
        _log.info("pathway_unit_test.skipped", test_id=str(test.test_id), error=spec)
        return error_item(test.test_id, name, spec)
    target = await _load_target(database, org_settings, test.org_id, test.pathway_id, test.version_number)
    if target is None:
        return error_item(test.test_id, name, "version_unavailable")
    result = await run_unit_test(
        deps,
        spec,
        test_id=str(test.test_id),
        org_id=test.org_id,
        active=target.active,
        org=target.org,
        loader=target.loader,
        counter=counter,
    )
    return result.item()


async def run_stored_simulation(
    deps: TextDeps,
    database: Database,
    org_settings: OrgSettingsReader,
    simulation: StoredSimulation,
    *,
    counter: TokenCounter,
) -> Item:
    """The result item of one conversation against the run's version (never raises for a pathway that no
    longer loads or fails; database and model infrastructure errors propagate, so the activity retries)."""
    target = await _load_target(
        database, org_settings, simulation.org_id, simulation.pathway_id, simulation.version_number
    )
    if target is None:
        return simulation_error_item(simulation.job, "version_unavailable")
    result = await run_simulation(
        deps,
        simulation.job,
        org_id=simulation.org_id,
        active=target.active,
        org=target.org,
        loader=target.loader,
        counter=counter,
    )
    return result.item()
