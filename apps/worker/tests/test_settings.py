"""Worker settings (specs/02-repo-layout-and-conventions.md §6) and task queue names (specs/01 §1)."""

from __future__ import annotations

import pytest
from service_helpers import settings_of

from va_worker.settings import WorkerSettings
from va_worker.task_queues import REGISTRATIONS, task_queue_names


def test_defaults_match_spec() -> None:
    settings = settings_of(WorkerSettings)
    assert settings.service_name == "worker"
    assert (settings.temporal_host, settings.temporal_namespace) == ("localhost:7233", "default")
    assert settings.temporal_task_queue == "va-main"
    assert settings.database_url == "postgresql+asyncpg://va:va@localhost:5432/va"


def test_task_queue_names_from_main_queue() -> None:
    assert task_queue_names("va-main") == ("va-main", "va-postcall", "va-ingest", "va-system")
    assert task_queue_names("staging-va-main")[1] == "staging-va-postcall"


@pytest.mark.parametrize("base", ["va", "va-postcall", "-main", ""])
def test_task_queue_names_require_main_suffix(base: str) -> None:
    with pytest.raises(ValueError, match="-main"):
        task_queue_names(base)


def test_no_registrations_before_workflows_exist() -> None:
    assert REGISTRATIONS == ()
