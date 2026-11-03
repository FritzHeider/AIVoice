"""Fixtures of the docker e2e suite (specs/30 §5; Decision P05-T17b): the running stack, one harness per test.

`make e2e` brings the compose `e2e` stack up (rebuilding images) before pytest starts. When any service does
not answer, the session stops with exit code 3 instead of skipping: a silent skip would read as a green run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from e2e_harness import Harness, StackUrls
from e2e_receiver import ReceiverSettings, ServedReceiver, WebhookReceiver, served_receiver

STACK_PROBE_TIMEOUT_S = 5.0
STACK_DOWN_MESSAGE = "e2e stack is not running: run make e2e (or make up PROFILE=e2e)"


@pytest.fixture(scope="session")
def stack() -> StackUrls:
    """The stack's URLs once every probe answered 200; exits the session (code 3) otherwise."""
    urls = StackUrls.from_env()
    down: list[str] = []
    with httpx.Client(timeout=STACK_PROBE_TIMEOUT_S) as client:
        for url in urls.probes():
            try:
                status = client.get(url).status_code
            except httpx.HTTPError as exc:
                down.append(f"{url}: {type(exc).__name__}")
                continue
            if status != httpx.codes.OK:
                down.append(f"{url}: {status}")
    if down:
        pytest.exit(f"{STACK_DOWN_MESSAGE} ({'; '.join(down)})", returncode=3)
    return urls


@pytest.fixture
async def harness(stack: StackUrls) -> AsyncIterator[Harness]:
    async with Harness.open(stack) as opened:
        yield opened


@pytest.fixture
async def receiver() -> AsyncIterator[ServedReceiver]:
    """A webhook receiver served from the pytest process (specs/30 §5 Decision P06-T08)."""
    settings = ReceiverSettings.from_env()
    recorder = WebhookReceiver()
    async with served_receiver(recorder, bind=settings.bind, port=settings.port) as port:
        yield ServedReceiver(recorder, settings.url(port))
