"""va_telnyx test fixtures: log reset, isolated environment, a FakeTelnyx media WebSocket, a respx Telnyx
API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import respx
from media_agent import MediaAgent
from service_helpers import reset_logging
from sim_helpers import isolated_env
from telnyx_client_helpers import BASE_URL

__all__ = ["isolated_env", "reset_logging"]


@pytest.fixture
async def media_agent() -> AsyncIterator[MediaAgent]:
    """A media WebSocket FakeTelnyx can stream to (``media_agent.url``)."""
    async with MediaAgent() as agent:
        yield agent


@pytest.fixture
def telnyx_api() -> Iterator[respx.MockRouter]:
    """A mocked Telnyx API at :data:`BASE_URL`; an unmatched request fails the test."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as router:
        yield router
