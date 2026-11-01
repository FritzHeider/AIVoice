"""va_elevenlabs test fixtures: log reset, an isolated environment, and the REST client rig with its respx
router."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import respx
from rest_client_helpers import BASE_URL, RestRig, rest_rig
from service_helpers import reset_logging
from sim_helpers import isolated_env

__all__ = ["api", "isolated_env", "reset_logging", "rig"]


@pytest.fixture
async def rig() -> AsyncIterator[RestRig]:
    """An :class:`ElevenLabsRest` of ``BASE_URL`` on a recording clock."""
    async with rest_rig() as client_rig:
        yield client_rig


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    """respx routes for ``BASE_URL`` (other hosts pass through)."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router
