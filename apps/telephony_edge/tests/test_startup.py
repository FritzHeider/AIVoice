"""The edge refuses to start without a valid platform webhook key (specs/06-telephony-telnyx.md §1, §4.1) or
a stream token secret, and wires the inbound accept by default (specs/06 §7 Decision P05-T12c)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from asgi_lifespan import LifespanManager
from edge_helpers import Keypair, ProbeContainer, edge_settings
from service_helpers import ok_check

from va_common.metrics import Service, build_metrics
from va_common.time import SystemClock
from va_edge.accept import InboundAccept
from va_edge.carrier import CarrierEventHandler
from va_edge.main import HANDLER_BUDGETS, NO_ACCEPTOR, carrier_handlers, create_app, platform_key
from va_edge.processor import EventHandler
from va_edge.routing import InboundRouter


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


async def test_missing_stream_token_secret_stops_startup() -> None:
    key = Keypair.generate()
    app = create_app(
        edge_settings(key.public_b64, va_env="test", stream_token_secret=""),
        container_factory=ProbeContainer({"postgres": ok_check}),
        init_observability=False,
    )
    with pytest.raises(RuntimeError, match="STREAM_TOKEN_SECRET is not configured"):
        async with LifespanManager(app):
            pass


def _router(handlers: Mapping[str, EventHandler]) -> InboundRouter:
    handler = handlers["call.initiated"]
    carrier = handler.__self__  # type: ignore[attr-defined]
    assert isinstance(carrier, CarrierEventHandler)
    router = carrier._inbound
    assert isinstance(router, InboundRouter)
    return router


def test_the_default_registry_accepts_inbound_calls_with_inbound_accept() -> None:
    settings = edge_settings(Keypair.generate().public_b64, va_env="test", media_public_host="media.test")
    container = ProbeContainer({"postgres": ok_check})
    metrics = build_metrics(Service.TELEPHONY_EDGE)
    handlers = carrier_handlers(container, clock=SystemClock(), metrics=metrics, settings=settings)
    accept = _router(handlers)._acceptor
    assert isinstance(accept, InboundAccept)
    assert accept._settings.media_public_host == "media.test"
    assert accept._settings.media_ws_scheme == "ws"
    assert "stream_secret" not in repr(accept._settings)
    assert HANDLER_BUDGETS == {"call.initiated": 5.0}


def test_no_acceptor_routes_without_one() -> None:
    settings = edge_settings(Keypair.generate().public_b64, va_env="test", stream_token_secret="")
    container = ProbeContainer({"postgres": ok_check})
    metrics = build_metrics(Service.TELEPHONY_EDGE)
    handlers = carrier_handlers(
        container, clock=SystemClock(), metrics=metrics, settings=settings, acceptor=NO_ACCEPTOR
    )
    assert _router(handlers)._acceptor is None
