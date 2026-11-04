"""Runs one stored unit test of a ``PathwayTestWorkflow`` run (specs/12-pathways-api.md §8; Decision
P07-T11c).

:func:`run_stored_test` reads the test row and the run's pathway version from the database (each in a short
tenant session, closed while the test runs) and runs it with :func:`va_pathways.unit_tests.run_unit_test`.
It never raises for data that disappeared meanwhile: a deleted test is an ``error`` item with
``test_not_found``, a pathway or version that no longer loads one with ``version_unavailable``, and a stored
row that no longer validates one with ``invalid_test``.
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
from va_engine.navigation import PathwayUnavailableError
from va_llm.tokens import TokenCounter
from va_pathways.conversation import TextDeps
from va_pathways.loader import DbPathwayLoader
from va_pathways.recording import Item
from va_pathways.unit_tests import UnitTestSpec, run_unit_test

__all__ = ["RunError", "StoredTest", "error_item", "insecure_urls_allowed", "run_stored_test"]

type RunError = Literal["test_not_found", "version_unavailable", "invalid_test", "runner_error"]

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
    loader = DbPathwayLoader(database, test.org_id)
    try:
        active = await loader.load(str(test.pathway_id), test.version_number)
    except PathwayUnavailableError as exc:
        _log.info("pathway_unit_test.version_unavailable", test_id=str(test.test_id), reason=str(exc))
        return error_item(test.test_id, name, "version_unavailable")
    org = await org_settings.get_lenient(test.org_id)
    result = await run_unit_test(
        deps,
        spec,
        test_id=str(test.test_id),
        org_id=test.org_id,
        active=active,
        org=OrgSettings() if org is None else org,
        loader=loader,
        counter=counter,
    )
    return result.item()
