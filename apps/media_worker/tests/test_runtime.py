"""The call runtime's lifespan wiring and per-process collaborators (specs/07 §16 Decision P05-T10a)."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import fakeredis
import pytest
from media_helpers import FakeContextLoader, FakeMediaContainer, FakeSocket, auth_values, call_context
from redis.asyncio import Redis
from runtime_rig import ASSETS_DIR
from service_helpers import json_log_stream, log_events, make_client, settings_of

from va_common.metrics import Service, ServiceMetrics, build_metrics, new_registry
from va_common.time import Clock, SystemClock
from va_db.session import Database
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_llm.tokens import default_counter
from va_media import runtime as runtime_module
from va_media.main import create_app
from va_media.runtime import CallRuntime, RuntimeDependencies, open_runtime
from va_media.runtime_config import ERROR_END
from va_media.session import CallSession, SessionBody, StreamStart
from va_media.settings import MediaSettings
from va_media.stream import read_until_stop
from va_media.vad_model import VadStatus
from va_telnyx import TelnyxClient, TelnyxClientFactory, UnknownTelnyxAccount
from va_telnyx.media import MediaFormat
from va_voice.background import BackgroundLibrary
from va_voice.vad import VadExecutor
from va_voice.vad_model import SileroModel

MODELS_DIR: Final = Path(__file__).resolve().parents[3] / "models"
REFUSED_LLM: Final = "http://127.0.0.1:9/v1"


@dataclass
class FactoryCall:
    container: RuntimeDependencies
    settings: MediaSettings
    clock: Clock
    metrics: ServiceMetrics
    vad: VadStatus
    models_dir: Path


@dataclass
class FakeRuntimeFactory:
    """A runtime factory yielding ``body``; records its calls and exits."""

    body: SessionBody = read_until_stop
    calls: list[FactoryCall] = field(default_factory=list)
    exited: int = 0

    @asynccontextmanager
    async def __call__(  # noqa: PLR0913 - the RuntimeFactory signature
        self,
        container: RuntimeDependencies,
        settings: MediaSettings,
        *,
        clock: Clock,
        metrics: ServiceMetrics,
        vad: VadStatus,
        models_dir: Path,
    ) -> AsyncIterator[SessionBody]:
        self.calls.append(FactoryCall(container, settings, clock, metrics, vad, models_dir))
        try:
            yield self.body
        finally:
            self.exited += 1


@dataclass
class FakeSessionView:
    """The parts of a session the runtime reads before it opens anything."""

    context: CallContext
    socket: FakeSocket = field(default_factory=FakeSocket)
    start: StreamStart = field(
        default_factory=lambda: StreamStart(
            "st-1", "cc-1", MediaFormat(encoding="PCMU", sample_rate=8000, channels=1)
        )
    )


class StaticOrgs:
    async def get(self, _org_id: uuid.UUID) -> OrgSettings | None:
        return OrgSettings()


@dataclass
class DbContainer:
    database: Database
    redis: Redis = field(default_factory=fakeredis.FakeAsyncRedis)


def _settings(**overrides: Any) -> MediaSettings:
    values: dict[str, Any] = {"local_master_key": secrets.token_hex(32), "llm_default_base_url": REFUSED_LLM}
    return settings_of(MediaSettings, **{**values, **overrides})


def _metrics() -> ServiceMetrics:
    return build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))


def _runtime(executor: VadExecutor, *, vad_model: Any = None, telnyx: Any = None) -> CallRuntime:
    return CallRuntime(
        settings=_settings(),
        gateway=cast("Any", None),
        counter=default_counter(),
        org_settings=StaticOrgs(),
        vad_model=vad_model,
        vad_executor=executor,
        clock=SystemClock(),
        metrics=_metrics(),
        background=BackgroundLibrary(ASSETS_DIR),
        telnyx=telnyx,
    )


def _session(view: FakeSessionView) -> CallSession:
    return cast("CallSession", view)


async def test_lifespan_enters_the_runtime_once_and_uses_its_body(models_dir: Path) -> None:
    factory = FakeRuntimeFactory()
    container = FakeMediaContainer()
    app = create_app(
        settings_of(MediaSettings, pod_id="pod-test", **auth_values()),
        container_factory=container,
        init_observability=False,
        models_dir=models_dir,
        runtime=factory,
        context_loader=FakeContextLoader().factory,
    )
    async with make_client(app):
        [call] = factory.calls
        assert call.container is container
        assert call.vad is app.state.media.vad
        assert call.vad.loaded
        assert call.models_dir == models_dir
        assert call.settings.pod_id == "pod-test"
        assert app.state.media_attach.body is factory.body
        assert factory.exited == 0
    assert factory.exited == 1


async def test_injected_body_skips_the_runtime(models_dir: Path) -> None:
    factory = FakeRuntimeFactory()
    app = create_app(
        settings_of(MediaSettings, **auth_values()),
        container_factory=FakeMediaContainer(),
        init_observability=False,
        models_dir=models_dir,
        body=read_until_stop,
        runtime=factory,
        context_loader=FakeContextLoader().factory,
    )
    async with make_client(app):
        assert app.state.media_attach.body is read_until_stop
    assert factory.calls == []


async def test_open_runtime_needs_a_database(models_dir: Path) -> None:
    with pytest.raises(TypeError, match="no database"):
        async with open_runtime(
            FakeMediaContainer(),
            _settings(),
            clock=SystemClock(),
            metrics=_metrics(),
            vad=VadStatus(loaded=True),
            models_dir=models_dir,
        ):
            pass


async def test_open_runtime_needs_the_local_master_key(models_dir: Path) -> None:
    settings = _settings(local_master_key="")
    database = Database.from_settings(settings)
    try:
        with pytest.raises(RuntimeError, match="LOCAL_MASTER_KEY"):
            async with open_runtime(
                DbContainer(database),
                settings,
                clock=SystemClock(),
                metrics=_metrics(),
                vad=VadStatus(loaded=True),
                models_dir=models_dir,
            ):
                pass
    finally:
        await database.dispose()


async def test_open_runtime_loads_the_vad_model_and_prewarms_the_llm() -> None:
    settings = _settings()
    database = Database.from_settings(settings)
    stream = json_log_stream()
    vad = VadStatus(loaded=True)
    try:
        async with open_runtime(
            DbContainer(database),
            settings,
            clock=SystemClock(),
            metrics=_metrics(),
            vad=vad,
            models_dir=MODELS_DIR,
        ) as runtime:
            assert isinstance(runtime, CallRuntime)
            assert isinstance(runtime.vad_model, SileroModel)
            assert isinstance(runtime._telnyx, TelnyxClientFactory)
    finally:
        await database.dispose()
    assert vad.loaded
    assert "llm.prewarm_failed" in [event["event"] for event in log_events(stream)]


async def test_open_runtime_skips_an_unverified_model(models_dir: Path) -> None:
    settings = _settings()
    database = Database.from_settings(settings)
    try:
        async with open_runtime(
            DbContainer(database),
            settings,
            clock=SystemClock(),
            metrics=_metrics(),
            vad=VadStatus(error="missing"),
            models_dir=models_dir,
        ) as runtime:
            assert runtime.vad_model is None
    finally:
        await database.dispose()


async def test_an_unloadable_model_fails_readiness(models_dir: Path) -> None:
    settings = _settings()
    database = Database.from_settings(settings)
    vad = VadStatus(loaded=True)  # the manifest matches the fixture bytes, onnxruntime rejects them
    try:
        async with open_runtime(
            DbContainer(database),
            settings,
            clock=SystemClock(),
            metrics=_metrics(),
            vad=vad,
            models_dir=models_dir,
        ) as runtime:
            assert runtime.vad_model is None
    finally:
        await database.dispose()
    assert not vad.loaded
    assert vad.error is not None
    assert "onnxruntime" in vad.error


async def test_a_runtime_without_vad_model_ends_with_error() -> None:
    stream = json_log_stream()
    async with VadExecutor(1) as executor:
        end = await _runtime(executor)(_session(FakeSessionView(call_context(uuid.uuid4()))))
    assert end == ERROR_END
    assert [event["event"] for event in log_events(stream)] == ["media.vad_unavailable"]


async def test_unsupported_leg_format_ends_with_error() -> None:
    stream = json_log_stream()
    pcma = StreamStart("st-1", "cc-1", MediaFormat(encoding="PCMA", sample_rate=8000, channels=1))
    view = FakeSessionView(call_context(uuid.uuid4()), start=pcma)
    async with VadExecutor(1) as executor:
        end = await _runtime(executor, vad_model=SileroModel.load(MODELS_DIR))(_session(view))
    assert end == ERROR_END
    assert [event["event"] for event in log_events(stream)] == ["media.pipeline_config_invalid"]


class FakeTelnyxFactory:
    """``for_account``: the platform sentinel for ``None``, :class:`UnknownTelnyxAccount` otherwise."""

    def __init__(self) -> None:
        self.platform = cast("TelnyxClient", object())
        self.accounts: list[uuid.UUID | None] = []

    async def for_account(self, account_id: uuid.UUID | None) -> TelnyxClient:
        self.accounts.append(account_id)
        if account_id is None:
            return self.platform
        raise UnknownTelnyxAccount(account_id)


async def test_the_call_telnyx_client_comes_from_the_factory() -> None:
    stream = json_log_stream()
    factory = FakeTelnyxFactory()
    byo = uuid.uuid4()
    async with VadExecutor(1) as executor:
        runtime = _runtime(executor, telnyx=factory)
        assert await runtime._telnyx_client(call_context(uuid.uuid4())) is factory.platform
        assert await runtime._telnyx_client(call_context(uuid.uuid4(), telnyx_account_id=str(byo))) is None
        assert await _runtime(executor)._telnyx_client(call_context(uuid.uuid4())) is None
    assert factory.accounts == [None, byo]
    [unavailable] = [e for e in log_events(stream) if e["event"] == "media.telnyx_unavailable"]
    assert unavailable["error_type"] == "UnknownTelnyxAccount"


async def test_a_failed_carrier_watch_is_logged_when_stopped() -> None:
    stream = json_log_stream()

    async def broken() -> None:
        message = "feed broke"
        raise RuntimeError(message)

    failed = asyncio.ensure_future(broken())
    await asyncio.wait({failed})
    await runtime_module._stop_task(failed, "media.carrier_watch_failed")

    async def forever() -> None:
        await asyncio.Event().wait()

    running = asyncio.ensure_future(forever())
    await runtime_module._stop_task(running, "media.carrier_watch_failed")
    assert running.cancelled()
    [event] = [e for e in log_events(stream) if e["event"] == "media.carrier_watch_failed"]
    assert event["error_type"] == "RuntimeError"
