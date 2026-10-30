"""FakeTelnyx test fixtures: the platform-side media WebSocket."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from media_agent import MediaAgent


@pytest.fixture
async def media_agent() -> AsyncIterator[MediaAgent]:
    """A media WebSocket the simulator can stream to (``media_agent.url``)."""
    async with MediaAgent() as agent:
        yield agent
