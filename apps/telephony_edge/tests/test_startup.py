"""The edge refuses to start without a valid platform webhook key (specs/06-telephony-telnyx.md §1, §4.1)."""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from edge_helpers import Keypair, ProbeContainer, edge_settings
from service_helpers import ok_check

from va_edge.main import create_app, platform_key


def _app(public_key: str) -> object:
    return create_app(
        edge_settings(public_key, va_env="test"),
        container_factory=ProbeContainer({"postgres": ok_check}),
        init_observability=False,
    )


async def test_missing_key_stops_startup_before_the_container_opens() -> None:
    container = ProbeContainer({"postgres": ok_check})
    app = create_app(edge_settings("", va_env="test"), container_factory=container, init_observability=False)
    with pytest.raises(RuntimeError, match="TELNYX_PUBLIC_KEY is not configured"):
        async with LifespanManager(app):
            pass
    assert container.opened == 0


async def test_malformed_key_stops_startup() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        async with LifespanManager(_app("c2hvcnQ=")):  # type: ignore[arg-type]
            pass


def test_platform_key_loads_the_configured_key() -> None:
    key = Keypair.generate()
    assert bytes(platform_key(edge_settings(key.public_b64))) == bytes(key.signing.verify_key)
