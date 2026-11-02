"""Media worker test helpers: fake container, app builder with fresh keys, served app, signed WS connects."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlencode

import fakeredis
import uvicorn
from clock_helpers import ManualClock
from nacl.signing import SigningKey
from redis.asyncio import Redis
from resolved_configs import resolved_config
from service_helpers import FakeContainer, RecordingServer, ok_check, settings_of
from starlette.applications import Starlette
from websockets.asyncio.client import ClientConnection, connect

from va_common.health import CheckFn
from va_common.service import StopController, run_server
from va_common.stream_tokens import mint_stream_token, stream_token_expiry
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_domain.call_workflow import PipelineFinished
from va_domain.calls.config import SendCallConfig
from va_media.context import CallContextLoader, ContextRefusedError
from va_media.control import ControlHandler
from va_media.loop_lag import LoopLagMonitor
from va_media.main import MediaDependencies, MediaState, create_app
from va_media.server import MediaAttach
from va_media.session import CallSession, SessionBody, SessionEnd
from va_media.settings import MediaSettings
from va_media.socket import SocketClosedError
from va_media.stream import read_until_stop

PUBLIC_HOST: Final = "media.test:8200"
TELNYX_PATH: Final = "/ws/telnyx"
CONTEXT_CREATED_AT: Final = datetime.fromisoformat("2026-11-02T12:00:00+00:00")


def call_context(
    call_id: uuid.UUID,
    *,
    org_id: uuid.UUID | None = None,
    opener_text: str | None = None,
    maintenance_message: str | None = None,
    direction: Literal["outbound", "inbound", "web"] = "outbound",
    **config: Any,
) -> CallContext:
    """A valid context for ``call_id`` built from the basic task-call snapshot (``config`` overrides)."""
    return CallContext(
        call_id=call_id,
        org_id=org_id if org_id is not None else uuid.uuid4(),
        direction=direction,
        to_number="+14155550100",
        from_number="+14155550199",
        config=SendCallConfig.model_validate({**resolved_config(), **config}),
        created_at=CONTEXT_CREATED_AT,
        opener_text=opener_text,
        maintenance_message=maintenance_message,
    )


class FakeContextLoader:
    """A context for any call (``contexts`` overrides; ``config`` overrides the snapshot of the others, which
    carry ``opener_text``, ``maintenance_message`` and ``direction``), or :class:`ContextRefusedError` for
    ``refusals``."""

    def __init__(self, **config: Any) -> None:
        self.config = config
        self.opener_text: str | None = None
        self.maintenance_message: str | None = None
        self.direction: Literal["outbound", "inbound", "web"] = "outbound"
        self.contexts: dict[uuid.UUID, CallContext] = {}
        self.refusals: dict[uuid.UUID, ContextRefusedError] = {}
        self.loads: list[tuple[uuid.UUID, str]] = []

    async def load(self, call_id: uuid.UUID, call_control_id: str) -> CallContext:
        self.loads.append((call_id, call_control_id))
        if call_id in self.refusals:
            raise self.refusals[call_id]
        return self.contexts.setdefault(
            call_id,
            call_context(
                call_id,
                opener_text=self.opener_text,
                maintenance_message=self.maintenance_message,
                direction=self.direction,
                **self.config,
            ),
        )

    def factory(self, _container: MediaDependencies, _clock: Clock) -> CallContextLoader:
        return self


def write_models_dir(
    root: Path, model: bytes, *, sha256: str | None = None, name: str = "silero_vad"
) -> Path:
    """A models directory with ``silero_vad.onnx`` and a ``MANIFEST.json`` entry (``sha256`` overrides)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "silero_vad.onnx").write_bytes(model)
    entry = {
        "name": name,
        "path": "models/silero_vad.onnx",
        "sha256": sha256 if sha256 is not None else hashlib.sha256(model).hexdigest(),
    }
    (root / "MANIFEST.json").write_text(json.dumps({"version": 1, "models": [entry]}), encoding="utf-8")
    return root


@dataclass
class RecordingSignaller:
    """A :class:`~va_media.workflow_signal.PipelineSignaller` that records ``(call id, summary)``."""

    signals: list[tuple[uuid.UUID, PipelineFinished]] = field(default_factory=list)
    signalled: asyncio.Event = field(default_factory=asyncio.Event)

    async def pipeline_finished(self, call_id: uuid.UUID, finished: PipelineFinished) -> None:
        self.signals.append((call_id, finished))
        self.signalled.set()


@dataclass
class FakeMediaContainer(FakeContainer):
    """A media container with an in-memory Redis and a passing ``redis`` check by default."""

    checks: Mapping[str, CheckFn] = field(default_factory=lambda: {"redis": ok_check})
    redis: Redis = field(default_factory=fakeredis.FakeAsyncRedis)


@dataclass
class MediaKeys:
    """A fresh Telnyx signing key and stream token secret (generated at test time)."""

    signing: SigningKey = field(default_factory=SigningKey.generate)
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    @property
    def public_b64(self) -> str:
        return base64.b64encode(bytes(self.signing.verify_key)).decode("ascii")

    def token(self, call_id: uuid.UUID, now: datetime, *, max_duration_min: float = 30) -> str:
        return mint_stream_token(call_id, stream_token_expiry(now, max_duration_min), self.secret)

    def headers(self, url: str, now: datetime) -> dict[str, str]:
        timestamp = int(now.timestamp())
        signature = self.signing.sign(f"{timestamp}|{url}".encode()).signature
        return {
            "telnyx-signature-ed25519": base64.b64encode(signature).decode("ascii"),
            "telnyx-timestamp": str(timestamp),
        }


def auth_values(keys: MediaKeys | None = None) -> dict[str, Any]:
    """Settings values that make the media lifespan start: secret, public key, and :data:`PUBLIC_HOST`."""
    used = keys if keys is not None else MediaKeys()
    return {
        "stream_token_secret": used.secret.hex(),
        "telnyx_public_key": used.public_b64,
        "media_public_host": PUBLIC_HOST,
    }


def media_app(  # noqa: PLR0913 - keyword-only test knobs
    models_dir: Path,
    container: FakeMediaContainer | None = None,
    *,
    monitor: LoopLagMonitor | None = None,
    keys: MediaKeys | None = None,
    clock: Any = None,
    body: SessionBody = read_until_stop,
    loader: FakeContextLoader | None = None,
    control_handler: ControlHandler | None = None,
    **overrides: Any,
) -> Starlette:
    """The media app with fake dependencies, no observability, an idle lag monitor (1 h ticks), a
    :class:`FakeContextLoader` (``loader``), and authentication configured from ``keys`` (fresh ones by
    default) for :data:`PUBLIC_HOST`."""
    values: dict[str, Any] = {"pod_id": "pod-test", **auth_values(keys), **overrides}
    return create_app(
        settings_of(MediaSettings, **values),
        container_factory=container if container is not None else FakeMediaContainer(),
        init_observability=False,
        models_dir=models_dir,
        monitor=monitor if monitor is not None else LoopLagMonitor(interval_s=3600),
        clock=clock,
        body=body,
        context_loader=(loader if loader is not None else FakeContextLoader()).factory,
        control_handler=control_handler,
    )


@asynccontextmanager
async def served_media(app: Starlette, *, port: int = 0) -> AsyncIterator[str]:
    """Serve ``app`` with real uvicorn on 127.0.0.1; yields the ``ws://host:port`` base URL."""
    ready = asyncio.Event()
    servers: list[RecordingServer] = []

    def factory(config: uvicorn.Config) -> RecordingServer:
        servers.append(RecordingServer(config, ready=ready))
        return servers[-1]

    stop = StopController()
    task = asyncio.ensure_future(
        run_server(app, host="127.0.0.1", port=port, stop=stop, install_signals=False, server_factory=factory)
    )
    try:
        waiter = asyncio.ensure_future(ready.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            waiter.cancel()
            task.result()
        yield servers[0].url.replace("http://", "ws://")
    finally:
        stop.request("served_media.exit")
        await task


def stream_query(call_id: uuid.UUID, token: str, *, leg: str | None = None) -> str:
    params = {"call_id": str(call_id), "token": token}
    if leg is not None:
        params["leg"] = leg
    return urlencode(params)


@asynccontextmanager
async def signed_connect(  # noqa: PLR0913 - keyword-only overrides of each signed part
    base_url: str,
    keys: MediaKeys,
    now: datetime,
    call_id: uuid.UUID,
    *,
    token: str | None = None,
    leg: str | None = None,
    query: str | None = None,
    signed_host: str = PUBLIC_HOST,
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[ClientConnection]:
    """Connect to ``base_url`` with Telnyx headers signed for ``ws://{signed_host}/ws/telnyx?{query}``."""
    raw_query = (
        query if query is not None else stream_query(call_id, token or keys.token(call_id, now), leg=leg)
    )
    signed = f"ws://{signed_host}{TELNYX_PATH}?{raw_query}"
    extra = {**keys.headers(signed, now), **(headers or {})}
    async with connect(
        f"{base_url}{TELNYX_PATH}?{raw_query}", additional_headers=extra, open_timeout=5, ping_interval=None
    ) as ws:
        yield ws


async def close_code(ws: ClientConnection) -> int | None:
    """The server's close code once the connection is closed (bounded wait)."""
    async with asyncio.timeout(5):
        await ws.wait_closed()
    return ws.close_code


def connected_msg() -> str:
    return json.dumps({"event": "connected", "version": "1.0.0"})


def start_msg(call_control_id: str = "cc-1", *, encoding: str = "PCMU", rate: int = 8000) -> str:
    return json.dumps(
        {
            "event": "start",
            "sequence_number": "1",
            "stream_id": "stream-1",
            "start": {
                "call_control_id": call_control_id,
                "media_format": {"encoding": encoding, "sample_rate": rate, "channels": 1},
            },
        }
    )


def stop_msg(call_control_id: str = "cc-1") -> str:
    return json.dumps(
        {
            "event": "stop",
            "sequence_number": "9",
            "stream_id": "stream-1",
            "stop": {"call_control_id": call_control_id},
        }
    )


def media_msg(chunk: int, payload: bytes, *, track: str | None = "inbound") -> str:
    media: dict[str, Any] = {"chunk": str(chunk), "timestamp": str(chunk * 20), "payload": b64(payload)}
    if track is not None:
        media["track"] = track
    return json.dumps(
        {"event": "media", "sequence_number": str(chunk + 1), "stream_id": "stream-1", "media": media}
    )


def dtmf_msg(digit: str) -> str:
    return json.dumps(
        {
            "event": "dtmf",
            "stream_id": "stream-1",
            "occurred_at": "2026-11-02T12:00:00.000Z",
            "sequence_number": "5",
            "dtmf": {"digit": digit},
        }
    )


def mark_msg(name: str) -> str:
    return json.dumps(
        {"event": "mark", "stream_id": "stream-1", "sequence_number": "6", "mark": {"name": name}}
    )


def error_msg(code: int, title: str = "telnyx error") -> str:
    return json.dumps({"event": "error", "stream_id": "stream-1", "payload": {"code": code, "title": title}})


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


class RecordingBody:
    """A session body that records each session and reads the stream until ``stop``."""

    def __init__(self) -> None:
        self.sessions: list[CallSession] = []
        self.ends: list[SessionEnd] = []
        self.started = asyncio.Event()
        self.ended = asyncio.Event()

    async def __call__(self, session: CallSession) -> SessionEnd:
        self.sessions.append(session)
        self.started.set()
        try:
            end = await read_until_stop(session)
        finally:
            self.ended.set()
        self.ends.append(end)
        return end

    async def wait_started(self) -> CallSession:
        async with asyncio.timeout(5):
            await self.started.wait()
        return self.sessions[-1]


class FakeSocket:
    """An in-memory :class:`~va_media.socket.MediaSocket`: ``feed`` messages, ``hang_up`` to close; sent text
    messages are recorded in ``sent`` (a send after ``hang_up``/``close`` raises ``SocketClosedError``)."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.closed_with: int | None = None
        self.sent: list[str] = []
        self.hung_up = False
        self.send_gate: asyncio.Event | None = None

    def feed(self, message: str | bytes) -> None:
        self.inbox.put_nowait(message)

    def hang_up(self) -> None:
        self.hung_up = True
        self.inbox.put_nowait(None)

    async def send_text(self, data: str) -> None:
        if self.hung_up or self.closed_with is not None:
            raise SocketClosedError(1000)
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append(data)

    async def receive(self) -> str | bytes:
        item = await self.inbox.get()
        if item is None:
            raise SocketClosedError(1000)
        return item

    async def close(self, code: int) -> None:
        if self.closed_with is None:
            self.closed_with = code


@dataclass
class Served:
    app: Starlette
    url: str
    keys: MediaKeys
    clock: ManualClock
    container: FakeMediaContainer
    body: RecordingBody
    loader: FakeContextLoader

    @property
    def media(self) -> MediaState:
        state: MediaState = self.app.state.media
        return state

    def metric(self, name: str, **labels: str) -> float | None:
        attach: MediaAttach = self.app.state.media_attach
        return attach.metrics.registry.get_sample_value(name, labels)

    def rejected(self, reason: str) -> float | None:
        return self.metric("va_media_admission_rejected_total", reason=reason)

    @asynccontextmanager
    async def connect(self, call_id: uuid.UUID, **kwargs: Any) -> AsyncIterator[ClientConnection]:
        async with signed_connect(self.url, self.keys, self.clock.now(), call_id, **kwargs) as ws:
            yield ws

    async def attach(self, ws: ClientConnection) -> CallSession:
        await ws.send(connected_msg())
        await ws.send(start_msg())
        return await self.body.wait_started()


@asynccontextmanager
async def served(
    models_dir: Path,
    *,
    body: Any = None,
    container: FakeMediaContainer | None = None,
    loader: FakeContextLoader | None = None,
    **overrides: Any,
) -> AsyncIterator[Served]:
    """The media app served with a :class:`ManualClock`, a :class:`RecordingBody` (unless ``body``), and a
    :class:`FakeContextLoader` (unless ``loader``)."""
    keys, clock = MediaKeys(), ManualClock()
    recording = RecordingBody()
    used = container if container is not None else FakeMediaContainer()
    contexts = loader if loader is not None else FakeContextLoader()
    app = media_app(
        models_dir,
        used,
        keys=keys,
        clock=clock,
        body=body if body is not None else recording,
        loader=contexts,
        **overrides,
    )
    async with served_media(app) as url:
        yield Served(app, url, keys, clock, used, recording, contexts)
