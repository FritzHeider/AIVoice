"""va_pathways test fixtures: a migrated Postgres per xdist worker (filter by your own ids)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from migration_helpers import migrated_postgres


@pytest.fixture(scope="session")
def pathways_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url
